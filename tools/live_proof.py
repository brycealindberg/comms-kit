#!/usr/bin/env python3
"""Prove the live setup still behaves like it did before a change.

Spec (default ~/.config/comms-kit/live-proof.json):
  {"commands": [{"name": "...", "argv": [...], "timeout": 600, "expect": "regex"}]}

--baseline runs each command and stores, per command: the exit code, whether `expect` matched,
and the test total when the output has a `Ran N tests` or `# pass N` line. The baseline goes to
~/.local/share/comms-kit/live-baseline.json. --compare reruns and fails when an exit code
changed, an expect stopped matching, or a test total dropped. More tests are fine.

Only command names and verdicts print. Command output can hold personal messages, so it is
never printed and never stored.

Exit codes: 0 ok, 1 changed, 2 could not check.
"""
import argparse
import json
import os
import re
import sys

import kit_common as kc

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "comms-kit")
DEFAULT_SPEC = os.path.join(CONFIG_DIR, "live-proof.json")
DEFAULT_BASELINE = os.path.join(os.path.expanduser("~"), ".local", "share", "comms-kit",
                                "live-baseline.json")
COUNT_PATTERNS = (re.compile(r"Ran (\d+) tests?\b"), re.compile(r"^# pass (\d+)\b", re.M))


def load_spec(path):
    data = kc.load_json(path, "the proof spec")
    commands = data.get("commands") if isinstance(data, dict) else None
    if not commands:
        kc.die("ERROR: the proof spec has no commands")
    seen = set()
    for cmd in commands:
        ok = (isinstance(cmd, dict) and isinstance(cmd.get("name"), str) and cmd["name"]
              and isinstance(cmd.get("argv"), list) and cmd["argv"]
              and all(isinstance(a, str) for a in cmd["argv"]))
        if ok and cmd.get("expect") is not None:
            try:
                re.compile(cmd["expect"])
            except (re.error, TypeError):
                ok = False
        if not ok or cmd["name"] in seen:
            kc.die("ERROR: the proof spec has a malformed or duplicate command")
        seen.add(cmd["name"])
    return commands


def test_total(output):
    totals = [int(m.group(1)) for rx in COUNT_PATTERNS for m in rx.finditer(output)]
    return sum(totals) if totals else None


def measure(cmd, cwd):
    argv = [os.path.expanduser(a) if a.startswith("~/") else a for a in cmd["argv"]]
    code, output = kc.run(argv, dict(os.environ), cwd, int(cmd.get("timeout", 600)))
    expect = cmd.get("expect")
    return {"name": cmd["name"],
            "exit": "timeout-or-start-failure" if code is None else code,
            "expect_matched": True if expect is None else bool(re.search(expect, output, re.M)),
            "tests": test_total(output)}


def run_all(commands, cwd):
    results = []
    for cmd in commands:
        result = measure(cmd, cwd)
        results.append(result)
    return results


def differences(old, new):
    problems = []
    if old["exit"] != new["exit"]:
        problems.append(f"{new['name']}: exit code changed ({old['exit']} -> {new['exit']})")
    if old["expect_matched"] and not new["expect_matched"]:
        problems.append(f"{new['name']}: expect no longer matches")
    if old["tests"] is not None and (new["tests"] is None or new["tests"] < old["tests"]):
        now = "none" if new["tests"] is None else new["tests"]
        problems.append(f"{new['name']}: test count dropped ({old['tests']} -> {now})")
    return problems


def baseline(commands, path, cwd):
    results = run_all(commands, cwd)
    bad = [r["name"] for r in results if not r["expect_matched"]]
    for r in results:
        print(f"{r['name']}: exit {r['exit']}" + ("" if r["expect_matched"] else ", expect missed"))
    if bad:
        print(f"baseline NOT stored: expect did not match for {len(bad)} commands")
        return kc.EXIT_DEFECT
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "commands": results}, fh, indent=2)
    os.chmod(path, 0o600)
    print(f"baseline stored: {len(results)} commands")
    return kc.EXIT_OK


def compare(commands, path, cwd):
    stored = kc.load_json(path, "the baseline (run --baseline first)")
    old = {r["name"]: r for r in stored.get("commands", [])} if isinstance(stored, dict) else {}
    if not old:
        kc.die("ERROR: the baseline holds no commands")
    results = run_all(commands, cwd)
    problems = []
    for r in results:
        if r["name"] not in old:
            problems.append(f"{r['name']}: not in the baseline")
        else:
            problems += differences(old[r["name"]], r)
    now = {r["name"] for r in results}
    problems += [f"{name}: in the baseline but not in the spec" for name in old if name not in now]
    for line in problems:
        print(line)
    if problems:
        print(f"live DIFFERS from baseline: {len(problems)} problems")
        return kc.EXIT_DEFECT
    print(f"live same as baseline: {len(results)} commands")
    return kc.EXIT_OK


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--baseline", action="store_true")
    mode.add_argument("--compare", action="store_true")
    ap.add_argument("--spec", default=DEFAULT_SPEC)
    ap.add_argument("--baseline-file", default=DEFAULT_BASELINE)
    ap.add_argument("--repo")
    args = ap.parse_args()
    commands = load_spec(args.spec)
    cwd = kc.repo_arg(args.repo)
    if args.baseline:
        return baseline(commands, args.baseline_file, cwd)
    return compare(commands, args.baseline_file, cwd)


if __name__ == "__main__":
    sys.exit(main())
