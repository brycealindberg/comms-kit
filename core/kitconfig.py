#!/usr/bin/env python3
"""One place for every personal value: self numbers, hosts, workspace names, data paths.

Code reads them here, so the code itself holds no personal data. The file is JSON at
COMMS_KIT_CONFIG, else ~/.config/comms-kit/config.json. It holds no secrets: tokens stay
in each tool's own store.

A broken file raises ConfigError and never reads as empty. A guard that compares against
a self number must stop when the number is unknown, not pass with nothing to compare.
"""
import contextlib
import json
import os
import sys
import tempfile

try:
    import fcntl
except ImportError:  # Windows has no flock. Single-writer use stays safe.
    fcntl = None

CONFIG_ENV = "COMMS_KIT_CONFIG"
DEFAULT_CONFIG = "~/.config/comms-kit/config.json"
DEFAULT_DATA_DIR = "~/.local/share/comms-kit"
_MISSING = object()
_cache = {}


class ConfigError(RuntimeError):
    """The config file is unreadable, malformed, or lacks a value the caller must have."""


def config_path():
    return os.path.expanduser(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)


def _parse(path, raw):
    try:
        data = json.loads(raw)
    except ValueError as err:
        # The message keeps the position and drops the text: the file is personal data.
        raise ConfigError(f"comms-kit: {path} is not valid JSON (line {err.lineno})") from None
    if not isinstance(data, dict):
        raise ConfigError(f"comms-kit: {path} must hold a JSON object")
    return data


def load():
    path = config_path()
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise ConfigError(f"comms-kit: cannot read {path}: {err.strerror}") from None
    key = (path, stat.st_mtime_ns, stat.st_size)
    if _cache.get("key") != key:
        try:
            with open(path, encoding="utf-8") as fh:
                _cache["data"] = _parse(path, fh.read())
        except OSError as err:
            raise ConfigError(f"comms-kit: cannot read {path}: {err.strerror}") from None
        _cache["key"] = key
    return _cache["data"]


def _lookup(data, dotted):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def get(key, default=None):
    value = _lookup(load(), key)
    return default if value is _MISSING else value


def require(key):
    value = _lookup(load(), key)
    if value is _MISSING or value in ("", None, [], {}):
        raise ConfigError(f"comms-kit: set {key} in {config_path()} "
                          "(see config.example.json in the kit)")
    return value


def get_list(key):
    value = get(key, [])
    if not isinstance(value, list):
        raise ConfigError(f"comms-kit: {key} in {config_path()} must be a list")
    return value


def path(key, default=None):
    value = get(key, default)
    return None if value is None else os.path.expanduser(str(value))


def data_dir():
    return path("data_dir", DEFAULT_DATA_DIR)


def archive_dir():
    """Where the archive (store, routes, indexes, gate log) lives.

    data_dir when set. A machine that never set it and already keeps its archive in
    ~/.claude/communications (routes.json there) keeps that folder. Otherwise the kit default.
    A broken config raises ConfigError: there is no fallback to a folder nobody chose."""
    if get("data_dir"):
        return data_dir()
    legacy = os.path.expanduser("~/.claude/communications")
    if os.path.exists(os.path.join(legacy, "routes.json")):
        return legacy
    return data_dir()


@contextlib.contextmanager
def _locked(target):
    folder = os.path.dirname(target) or "."
    os.makedirs(folder, mode=0o700, exist_ok=True)
    if fcntl is None:
        yield
        return
    with open(target + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def set_value(key, value):
    """Set one dotted key atomically. A broken file is left as it is, never replaced."""
    target = config_path()
    with _locked(target):
        try:
            with open(target, encoding="utf-8") as fh:
                data = _parse(target, fh.read())
        except FileNotFoundError:
            data = {}
        node = data
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ConfigError(f"comms-kit: {part} in {key} already holds a plain value")
        node[parts[-1]] = value
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target) or ".", prefix=".config-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


def _keys(node, prefix=""):
    for name, value in sorted(node.items()):
        dotted = f"{prefix}{name}"
        if isinstance(value, dict) and value:
            yield from _keys(value, dotted + ".")
        else:
            yield dotted


def main(argv):
    """`set KEY VALUE` and `keys`. Neither prints a value, so a log cannot leak one."""
    try:
        if len(argv) == 3 and argv[0] == "set":
            try:
                value = json.loads(argv[2])
            except ValueError:
                value = argv[2]
            set_value(argv[1], value)
            print(f"set {argv[1]}")
            return 0
        if argv == ["keys"]:
            print("\n".join(_keys(load())))
            return 0
    except ConfigError as err:
        print(err, file=sys.stderr)
        return 1
    print("usage: kitconfig.py set KEY VALUE | keys", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
