#!/usr/bin/env python3
"""Clone the committed HEAD into a temp dir and run every test suite there, in a clean room.

The clean room: an empty HOME, COMMS_KIT_CONFIG pointing at a copy of config.example.json,
and a stub dir first on PATH. The stubs (gws, ssh, scp, imessage, imsg, npm-publish, swiftc)
record the call and exit 99. A suite that reaches gws, ssh, scp, imessage or imsg fails, even
when it hides the error. A real gws under a stripped environment deletes the real login.

Exit codes: 0 all suites pass, 1 a suite failed, 2 could not check (no suites, no commit).
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys

import kit_common as kc

TAIL_LINES = 15
RAN_ZERO = re.compile(r"^Ran 0 tests?\b", re.M)


def discover(clone):
    patterns = ("tests/test_*.py", "core/tests/test_*.py", "channels/*/test_*.py",
                "channels/*/tests/test_*.py")
    found = set()
    for pattern in patterns:
        found.update(os.path.relpath(p, clone).replace(os.sep, "/")
                     for p in glob.glob(os.path.join(clone, pattern)))
    locks = glob.glob(os.path.join(clone, "channels", "*", "package-lock.json"))
    locks += glob.glob(os.path.join(clone, "channels", "*", "*", "package-lock.json"))
    node = sorted(os.path.relpath(p, clone).replace(os.sep, "/")
                  for p in locks if "node_modules" not in p)
    return sorted(found), [os.path.dirname(p) for p in node]


def tail(text):
    lines = text.rstrip("\n").split("\n")
    return "\n".join(lines[-TAIL_LINES:])


def run_python_suite(clone, rel, env, timeout):
    code, out = kc.run([sys.executable, rel], env, clone, timeout)
    if code is None:
        return False, f"timed out after {timeout}s or could not start\n{out}"
    if code != 0:
        return False, f"exit {code}\n{out}"
    if RAN_ZERO.search(out):
        return False, f"ran 0 tests\n{out}"
    return True, out


def run_node_suite(clone, folder, env, timeout):
    cwd = os.path.join(clone, folder)
    for argv in (["npm", "ci", "--ignore-scripts"], ["node", "--test"]):
        code, out = kc.run(argv, env, cwd, timeout)
        if code != 0:
            why = "timed out or could not start" if code is None else f"exit {code}"
            return False, f"{' '.join(argv[:2])}: {why}\n{out}"
    return True, out


def clone_head(repo, dest, env):
    head = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "-q", "HEAD"],
                          capture_output=True)
    if head.returncode != 0:
        kc.die("ERROR: the repo has no commit; nothing to clone")
    done = subprocess.run(["git", "clone", "-q", "--no-hardlinks", repo, dest], env=env,
                          capture_output=True)
    if done.returncode != 0:
        kc.die("ERROR: git clone failed")


def copy_worktree(repo, dest):
    """Tracked files plus new files git would add, for a check before the first commit. The
    release gate never uses this: it clones HEAD, so an uncommitted file cannot make it pass."""
    out = subprocess.run(["git", "-C", repo, "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard"], capture_output=True, check=True).stdout
    for rel in out.decode("utf-8", "surrogateescape").split("\0"):
        source = os.path.join(repo, rel)
        if rel and os.path.isfile(source):
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo")
    ap.add_argument("--worktree", action="store_true",
                    help="test uncommitted files too; prints WORKTREE, not the release line")
    ap.add_argument("--timeout", type=int, default=300, help="seconds per suite")
    ap.add_argument("--real-home", default=kc.real_home_default(),
                    help="home whose comms-kit folders must stay untouched")
    args = ap.parse_args()
    repo = kc.repo_arg(args.repo)

    work = kc.make_workdir("comms-clean-")
    try:
        return run_all(repo, work, args)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_all(repo, work, args):
    home = os.path.join(work, "home")
    os.makedirs(home)
    log = os.path.join(work, "stub.log")
    stubs = kc.make_stubs(os.path.join(work, "stubs"), log)
    clone = os.path.join(work, "clone")
    git_env = kc.clean_env(home, stubs)
    if args.worktree:
        copy_worktree(repo, clone)
    else:
        clone_head(repo, clone, git_env)

    example = os.path.join(clone, "config.example.json")
    if not os.path.isfile(example):
        kc.die("ERROR: the committed tree has no config.example.json")
    confdir = os.path.join(work, "conf")
    os.makedirs(confdir)
    env = kc.clean_env(home, stubs, kc.copy_config(confdir, example),
                       tmpdir=os.path.join(work, "tmp"))
    os.makedirs(env["TMPDIR"])

    py_suites, node_dirs = discover(clone)
    suites = [(rel, "py") for rel in py_suites] + [(d, "node") for d in node_dirs]
    if not suites:
        kc.die("ERROR: the committed tree has no test suites")

    watched = kc.real_home_dirs(args.real_home)
    before = kc.snapshot(watched)
    failed = 0
    for name, kind in suites:
        offset = kc.log_size(log)
        if kind == "py":
            ok, out = run_python_suite(clone, name, env, args.timeout)
            label = name
        else:
            ok, out = run_node_suite(clone, name, env, args.timeout)
            label = f"{name} (node)"
        bad_calls = kc.forbidden(kc.stub_calls(log, offset))
        if bad_calls:
            ok = False
            out = f"reached a real tool: {', '.join(bad_calls)}\n{out}"
        if ok:
            print(f"PASS {label}")
        else:
            failed += 1
            reason = out.split("\n", 1)[0]
            print(f"FAIL {label}: {reason}")
            print(tail(out.split("\n", 1)[1] if "\n" in out else out))
    changed = kc.snapshot_diff(before, kc.snapshot(watched))
    for root in changed:
        failed += 1
        print(f"FAIL real state touched: {os.path.relpath(root, args.real_home)} changed")
    if failed:
        print(f"clean clone: FAILED ({failed} problems in {len(suites)} suites)")
        return kc.EXIT_DEFECT
    label = "WORKTREE check, not the release gate" if args.worktree else "clean clone"
    print(f"{label}: OK ({len(suites)} suites)")
    return kc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
