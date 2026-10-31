"""Shared helpers for the release-gate tools. Python stdlib only.

The main piece is the stub harness. A real gws under a stripped environment deletes the real
login, and a real ssh can reach production, so every tool that runs repo code puts a stub
directory first on PATH. Each stub records its own name to a log and exits 99.
"""
import json
import os
import re
import signal
import subprocess
import sys
import tempfile

EXIT_OK, EXIT_DEFECT, EXIT_CANNOT = 0, 1, 2

# Names a test must never reach. The first group fails a suite when called; swiftc only records.
STUB_NAMES = ("gws", "gws-as", "ssh", "scp", "imessage", "imsg", "npm-publish", "swiftc")
FORBIDDEN_CALLS = ("gws", "gws-as", "ssh", "scp", "imessage", "imsg", "npm-publish")
STUB_MARK = "STUBCALL"

# Folders whose contents are not part of the published tree.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".unlazy"}

REPO_DEFAULT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def repo_arg(value):
    return os.path.realpath(value or REPO_DEFAULT)


def die(message, code=EXIT_CANNOT):
    print(message, file=sys.stderr)
    raise SystemExit(code)


def load_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        die(f"ERROR: {what} not found")
    except (OSError, ValueError) as err:
        die(f"ERROR: cannot read {what}: {type(err).__name__}")


# ----------------------------------------------------------------------------- files
def repo_files(repo):
    """Tracked files plus new files git would add; falls back to a walk outside a git repo.
    Paths are repo-relative with forward slashes."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, check=True).stdout
        names = {n for n in out.decode("utf-8", "surrogateescape").split("\0") if n}
        return sorted(n for n in names if os.path.lexists(os.path.join(repo, n)))
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for base, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            found.append(os.path.relpath(os.path.join(base, name), repo).replace(os.sep, "/"))
    return sorted(found)


def is_git_repo(repo):
    return subprocess.run(["git", "-C", repo, "rev-parse", "--git-dir"],
                          capture_output=True).returncode == 0


def skipped(rel):
    return any(part in SKIP_DIRS for part in rel.split("/"))


def is_test_path(rel):
    parts = rel.split("/")
    name = parts[-1]
    return ("tests" in parts[:-1] or name.startswith("test_") or name.endswith("_test.py")
            or name.endswith(".test.js"))


def has_python_shebang(path):
    try:
        with open(path, "rb") as fh:
            first = fh.read(256).split(b"\n", 1)[0]
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first


def python_sources(repo, roots=("core", "channels")):
    """Non-test python files under the given roots, including extensionless python scripts."""
    found = []
    for rel in repo_files(repo):
        if rel.split("/")[0] not in roots or skipped(rel) or is_test_path(rel):
            continue
        path = os.path.join(repo, rel)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        if rel.endswith(".py") or ("." not in os.path.basename(rel) and has_python_shebang(path)):
            found.append(rel)
    return found


# ----------------------------------------------------------------------------- processes
def run(argv, env, cwd, timeout):
    """Run to completion. Returns (exit code or None on timeout, combined text).
    A timeout kills the whole process group, so a hung child cannot outlive the tool."""
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except OSError as err:
        return None, f"cannot start: {err.strerror or type(err).__name__}"
    try:
        out, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, _ = proc.communicate()
        code = None
    return code, out.decode("utf-8", "replace")


# ----------------------------------------------------------------------------- stub harness
def make_stubs(stub_dir, log_path):
    """Create the stub executables. Returns the stub dir."""
    os.makedirs(stub_dir, exist_ok=True)
    open(log_path, "a").close()
    quoted = "'" + log_path.replace("'", "'\\''") + "'"
    body = ("#!/bin/sh\n"
            f"printf '{STUB_MARK}\\t%s\\n' \"$(basename \"$0\")\" >> {quoted}\n"
            "exit 99\n")
    for name in STUB_NAMES:
        path = os.path.join(stub_dir, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
    return stub_dir


def log_size(log_path):
    try:
        return os.path.getsize(log_path)
    except OSError:
        return 0


def stub_calls(log_path, since=0):
    """Names recorded after byte offset `since`, in order."""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(since)
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    prefix = STUB_MARK + "\t"
    return [line[len(prefix):] for line in text.split("\n") if line.startswith(prefix)]


def forbidden(calls):
    return sorted({c for c in calls if c in FORBIDDEN_CALLS})


def clean_env(home, stub_dir, config_path=None, tmpdir=None):
    """A whitelisted environment: empty HOME, stubs first on PATH, nothing personal."""
    real_path = os.environ.get("PATH", "/usr/bin:/bin")
    env = {"PATH": stub_dir + os.pathsep + real_path, "HOME": home, "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "TERM": "dumb"}
    if tmpdir:
        env["TMPDIR"] = tmpdir
    if config_path:
        env["COMMS_KIT_CONFIG"] = config_path
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
                "no_proxy", "SSL_CERT_FILE"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def temp_config(dest_dir, example_path, **overrides):
    """Copy config.example.json into dest_dir, with top-level overrides. Returns the path."""
    data = load_json(example_path, "config.example.json")
    if not isinstance(data, dict):
        die("ERROR: config.example.json is not an object")
    data.update(overrides)
    path = os.path.join(dest_dir, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return path


def copy_config(dest_dir, example_path):
    """Byte copy of config.example.json."""
    path = os.path.join(dest_dir, "config.json")
    try:
        with open(example_path, "rb") as src, open(path, "wb") as dst:
            dst.write(src.read())
    except OSError:
        die("ERROR: config.example.json not found")
    return path


def make_workdir(prefix):
    return os.path.realpath(tempfile.mkdtemp(prefix=prefix))


# ----------------------------------------------------------------------------- snapshots
def real_home_dirs(real_home):
    return [os.path.join(real_home, ".config", "comms-kit"),
            os.path.join(real_home, ".local", "share", "comms-kit")]


def snapshot(paths):
    """{path: {relpath: (size, mtime_ns)}}; a folder that does not exist is None."""
    snap = {}
    for root in paths:
        if not os.path.isdir(root):
            snap[root] = None
            continue
        entries = {}
        for base, dirs, files in os.walk(root):
            for name in dirs + files:
                full = os.path.join(base, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                entries[os.path.relpath(full, root)] = (st.st_size, st.st_mtime_ns)
        snap[root] = entries
    return snap


def snapshot_diff(before, after):
    """Names of the watched folders that changed."""
    return [root for root in before if before[root] != after.get(root)]


def real_home_default():
    return os.path.expanduser("~")


def tree_hash(repo, skip=("node_modules", ".git")):
    """{relpath: sha256 or 'dir'} for the whole code tree."""
    import hashlib
    result = {}
    for base, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in skip)
        for name in dirs:
            result[os.path.relpath(os.path.join(base, name), repo)] = "dir"
        for name in files:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, repo)
            if os.path.islink(full):
                result[rel] = "link:" + os.readlink(full)
                continue
            digest = hashlib.sha256()
            with open(full, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 16), b""):
                    digest.update(chunk)
            result[rel] = digest.hexdigest() + oct(os.stat(full).st_mode & 0o777)
    return result


SUBCOMMAND_GROUP = re.compile(r"\{([A-Za-z0-9_][\w.-]*(?:,[A-Za-z0-9_][\w.-]*)*)\}")
