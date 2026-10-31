#!/usr/bin/env python3
"""Shared logic for the .communications logger.

Zero model tokens are spent here: this module runs inside a PostToolUse hook
(a separate OS process) and inside the `comms` query CLI. Nothing it writes is
read by Claude unless the model explicitly runs `comms find`.

Python 3.9 compatible (macOS system python3). Stdlib only.
"""
import json
import os
import re
import shlex
import time
import datetime
import email.utils
import fcntl
import hashlib
import contextlib

import sys

# kitconfig sits beside this file. The whatsapp and slack CLIs load this module by path, so
# the folder goes on sys.path here, not in the caller.
_HERE = os.path.dirname(os.path.abspath(__file__))  # kit: code path
if _HERE not in sys.path:  # kit: code path
    sys.path.insert(0, _HERE)
import kitconfig as K  # noqa: E402

HOME = os.path.expanduser("~")


ROOT = K.archive_dir()
STORE = os.path.join(ROOT, "store")
ROUTES = os.path.join(ROOT, "routes.json")
THREADS = os.path.join(ROOT, "threads.json")
# abs client-repo path -> project, for cwd-scoped reads (`comms here`/`unread`)
REPOS = os.path.join(ROOT, "project_repos.json")
PREVIEWS = os.path.join(ROOT, "previews.json")
UNROUTED = os.path.join(ROOT, "UNROUTED.md")
DRAFTS = os.path.join(ROOT, "drafts.json")
# A draft older than this is not evidence for a later send: the variable behind
# `"$MSG"` could have been reassigned in between. Real draft-then-send is
# seconds apart.
DRAFT_TTL = 3600

BODY_CAP = 20000          # chars of transcript kept per row
DIR_NAME = ".communications"

# ---------------------------------------------------------------- channel programs

# channel -> (folder under ~/.claude/skills, folder under <repo>/channels, program file)
_CLI_HOMES = {
    "whatsapp": ("whatsapp", "whatsapp", "whatsapp_mautrix.py"),
    "slack": ("slack-bridge", "slack", "slack_bridge.py"),
    "imessage": ("imessage", "imessage", "imessage"),
}


def channel_cli(channel):
    """Path of a built-in channel's program, or the first place it should be when missing.

    Looks beside the kit (`<core>/../channels/<channel>/`), then in the skills folder the
    installed layout uses, then (iMessage) on PATH and in ~/.local/bin."""
    skill, repo, name = _CLI_HOMES[channel]
    found = [os.path.join(_HERE, "..", "channels", repo, name),  # kit: code path
             os.path.join(HOME, ".claude", "skills", skill, name)]
    if channel == "imessage":
        found += [os.path.join(HOME, ".local", "bin", name)]
        import shutil
        on_path = shutil.which(name)
        if on_path:
            found.insert(1, on_path)
    for path in found:
        if os.path.exists(path):
            return os.path.abspath(path)
    return os.path.abspath(found[1] if channel != "imessage" else found[-1])


# ---------------------------------------------------------------- channel plug-ins

BUILTIN_CHANNELS = ("imessage", "whatsapp", "slack", "gmail", "upwork")
MANIFEST_KINDS = ("python-cli", "node", "external")
_MANIFEST_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MANIFEST_CACHE = {"key": None, "found": [], "warnings": []}


def _manifest_folders():
    """Folders that may hold a channel.json: config `channel_dirs` (each IS a channel folder),
    then every folder under <core>/../channels/. Never raises."""
    folders, warnings = [], []
    try:
        listed = K.get_list("channel_dirs")
    except Exception as err:  # noqa: BLE001 - a bad config key is one warning, not a crash
        listed = []
        warnings.append("channel_dirs: %s" % err)
    for item in listed:
        if isinstance(item, str) and item.strip():
            folders.append(os.path.abspath(os.path.expanduser(item)))
        else:
            warnings.append("channel_dirs: ignored an entry that is not a folder path")
    base = os.path.join(_HERE, "..", "channels")  # kit: code path
    try:
        folders += [os.path.abspath(os.path.join(base, n)) for n in sorted(os.listdir(base))]
    except OSError:
        pass
    return folders, warnings


def _read_manifest(folder):
    """-> (manifest dict or None, warning or None) for one folder."""
    path = os.path.join(folder, "channel.json")
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as err:
        return None, "%s: not readable as JSON (%s)" % (path, err.__class__.__name__)
    if not isinstance(data, dict):
        return None, "%s: must hold a JSON object" % path
    name, verbs = data.get("name"), data.get("verbs")
    if not isinstance(name, str) or not _MANIFEST_NAME_RE.match(name):
        return None, "%s: name must be lowercase letters, digits, - or _" % path
    if data.get("kind") not in MANIFEST_KINDS:
        return None, "%s: kind must be one of %s" % (path, ", ".join(MANIFEST_KINDS))
    if not isinstance(data.get("entry"), str) or not data["entry"]:
        return None, "%s: entry must name the program file" % path
    if not isinstance(verbs, dict) or not all(
            isinstance(v, dict) and v.get("direction") in ("send", "read", "none")
            for v in verbs.values()):
        return None, "%s: verbs must map each verb to a direction (send, read or none)" % path
    for key in ("ignored",):
        if key in data and not (isinstance(data[key], list)
                                and all(isinstance(x, str) for x in data[key])):
            return None, "%s: %s must be a list of strings" % (path, key)
    blocked = data.get("blocked", {})
    if not isinstance(blocked, dict):
        return None, "%s: blocked must be an object" % path
    entry = os.path.abspath(os.path.join(folder, data["entry"]))
    return dict(data, dir=folder, entry_path=entry, blocked=blocked,
                ignored=list(data.get("ignored", []))), None


def channel_manifests():
    """The plug-in channels found in `channel_dirs` and <core>/../channels/, as a list of their
    channel.json contents plus `dir` and `entry_path`. A manifest named like a built-in channel is
    skipped; the first folder wins a duplicate name. Cheap: cached on each file's mtime and size.
    Never raises: a bad manifest is one line from channel_manifest_warnings()."""
    try:
        folders, warnings = _manifest_folders()
        stamps = []
        for folder in folders:
            try:
                st = os.stat(os.path.join(folder, "channel.json"))
                stamps.append((folder, st.st_mtime_ns, st.st_size))
            except OSError:
                stamps.append((folder, None, None))
        key = (tuple(stamps), tuple(warnings))
        if _MANIFEST_CACHE["key"] == key:
            return list(_MANIFEST_CACHE["found"])
        found, seen = [], set(BUILTIN_CHANNELS)
        for folder in folders:
            manifest, warning = _read_manifest(folder)
            if warning:
                warnings.append(warning)
            elif manifest and manifest["name"] not in seen:
                seen.add(manifest["name"])
                found.append(manifest)
        _MANIFEST_CACHE.update(key=key, found=found, warnings=warnings)
        return list(found)
    except Exception as err:  # noqa: BLE001 - discovery must never stop a tool call
        _MANIFEST_CACHE.update(key=None, found=[], warnings=["channel discovery failed: %s" % err])
        return []


def channel_manifest_warnings():
    """One line per manifest or config entry that could not be used (after channel_manifests())."""
    channel_manifests()
    return list(_MANIFEST_CACHE["warnings"])


# ---------------------------------------------------------------- channels

# marker in the command string -> channel name
CHANNEL_MARKERS = (
    ("whatsapp_mautrix.py", "whatsapp"),
    ("slack_bridge.py", "slack"),
    ("imessage", "imessage"),
    ("gws gmail", "gmail"),
    ("gws-as", "gmail"),      # `gws-as EMAIL gmail ...`: an added Gmail account (gmail_accounts.py)
)

# subcommand -> direction. Anything not listed is infrastructure noise
# (status, doctor, pair, login, workspaces) and is never logged.
VERBS = {
    "imessage": {
        "history": "read",
        "prepare-send": "draft", "send": "send", "send-preview": "send",
    },
    "whatsapp": {
        "history": "read", "transcribe": "read", "download": "read",
        "send": "send", "send-media": "send", "send-voice-confirm": "send",
        # an emoji reaction answers a message the way a text does (2026-10-28)
        "react": "send",
        # both put words in front of a person (2026-10-31)
        "reply": "send", "edit": "send",
    },
    "slack": {
        "history": "read", "files": "read", "transcribe": "read",
        "transcribe-channel": "read", "download": "read",
        # threads: the live read WITH thread replies (the buffer `history` has none)
        "threads": "read",
        "send": "send", "upload": "send",
        "react": "send",
        "edit": "send",      # puts new words in front of a person (2026-10-31)
    },
    # verified against `gws gmail --help` and `gws gmail users messages
    # --help` on 2026-09-09. gws has TWO paths: the `+verb` helpers and the
    # raw `users <resource> <method>` API. Both can send real mail.
    "gmail": {
        "+send": "send", "+reply": "send", "+reply-all": "send",
        "+forward": "send",
        "+read": "read", "+triage": "read", "+watch": "read",
        "messages.send": "send", "drafts.send": "send",
        # insert/import write a message INTO the mailbox without transmitting
        # it to anybody, so filing them as sends claimed mail was delivered
        "messages.import": "draft", "messages.insert": "draft",
        "drafts.create": "draft", "drafts.update": "draft",
        "messages.get": "read", "messages.list": "read",
        "threads.get": "read", "threads.list": "read",
        "history.list": "read", "drafts.get": "read", "drafts.list": "read",
    },
}


# Verbs deliberately NOT logged, listed so `comms doctor` can tell "we decided
# to skip this" apart from "this appeared and nobody classified it". A new send
# verb showing up unclassified means messages are silently never archived —
# that happened twice on 2026-09-09 (imessage `send`, gws `users messages
# send`), so this list is the tripwire. Add to it only on purpose.
IGNORED = {
    "imessage": {"doctor", "show-preview", "discard-preview",
                 "chats", "search", "whois", "stats", "resolve"},
    "whatsapp": {"login", "status", "sync", "send-voice",
                 "chats", "groups", "unread", "search",
                 # searches and admin actions: no words reach a person (2026-10-31)
                 "find", "delete", "mark-read", "block", "unblock",
                 "group-create", "group-add", "group-remove", "group-rename",
                 "group-leave", "group-members"},
    "slack": {"status", "workspaces", "sync", "pair",
              "channels", "contacts",
              # searches and admin actions: no words reach a person (2026-10-31)
              "open-dm", "find", "mentions", "delete", "channel-create",
              "channel-invite", "channel-remove", "channel-rename",
              "channel-leave", "channel-archive", "channel-members"},
    "gmail": set(),
}


# a WhatsApp/Slack `send` without --yes is only a dry run, not a real send
DRYRUN_UNLESS_YES = {("whatsapp", "send"), ("whatsapp", "send-media"),
                     ("whatsapp", "react"), ("whatsapp", "reply"), ("whatsapp", "edit"),
                     ("slack", "send"), ("slack", "upload"), ("slack", "react"),
                     ("slack", "edit")}


def _now():
    t = time.time()
    lt = datetime.datetime.fromtimestamp(t).astimezone()
    return t, lt.strftime("%Y-%m-%dT%H:%M:%S %Z"), lt.strftime("%Y-%m")


def _load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


# How long the hook's append will wait for a rewriter before giving up and
# writing unlocked. A rewrite holds the lock for milliseconds; two seconds is
# far beyond that and still far inside the hook's 10 second kill.
APPEND_LOCK_WAIT = 2.0


@contextlib.contextmanager
def filelock(path, timeout=None):
    """Advisory lock so concurrent sessions cannot interleave a read-modify-
    write. Every agent on this Mac writes these same files.

    `timeout` (seconds) polls instead of blocking and, if it expires, runs the
    body UNLOCKED. That is for the hook's append path: a rewrite that stalls
    must never stop a message being recorded, but the ordinary case still has
    to be serialised, because an append landing between a rewriter's tail read
    and its rename goes to the OLD inode and is discarded with it.
    """
    lock = path + ".lock"
    fh = None
    try:
        fh = open(lock, "w")
        try:
            os.chmod(lock, 0o600)
        except Exception:
            pass
        if timeout is None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        else:
            deadline = time.time() + timeout
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except Exception:
                    if time.time() >= deadline:
                        fh.close()
                        fh = None
                        break
                    time.sleep(0.005)
    except Exception:
        # FAIL OPEN on purpose: a lock we cannot take must never stop the hook
        # recording a message. The body runs unsynchronised, which is the same
        # risk this code had before locks existed.
        fh = None
    try:
        # exactly ONE yield. Yielding from both the try and the except turned
        # any error raised inside the block into
        # `RuntimeError: generator didn't stop after throw()`, hiding the real
        # exception in __context__.
        yield
    finally:
        if fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except Exception:
                pass
            fh.close()


def load_strict(path, default):
    """Distinguish ABSENT from UNPARSEABLE.

    `_load` returned `{}` for both, so a torn read mid-`os.replace` made the
    next writer treat a 400-entry index as empty and write its own single key
    back as the whole file. Measured: 400 of 406 entries destroyed.
    """
    if not os.path.exists(path):
        return default
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None          # corrupt — caller must NOT overwrite


def secure_dir(path):
    """Create a directory 0700 and tighten it if it already exists.

    The ROOT was only ever created implicitly by `makedirs`, so under a default
    umask of 022 it landed at 0755 — every file inside was 0600 but the
    directory listing of who the owner talks to was world-readable.
    """
    try:
        os.makedirs(path, exist_ok=True)
        os.chmod(path, 0o700)
    except Exception:
        pass
    return path


def _save(path, obj):
    tmp = "%s.tmp.%d" % (path, os.getpid())   # never a shared tmp name
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # 0600 AT CREATION, not after the rename: a writer killed mid-save (the
    # hook has a 10s budget) left an orphaned .tmp holding a full copy of the
    # index at the default 0644.
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


# ---------------------------------------------------------------- parsing

def _strip_heredocs_cheap(command):
    """Line-level heredoc strip. Quote-blind, but O(lines) with one regex per
    line — cheap enough to run on a multi-megabyte command, which the precise
    scanner below is not. Used only by the oversize guard in `_prefilter`."""
    out, lines = [], command.split("\n")
    i = 0
    while i < len(lines):
        out.append(lines[i])
        m = re.search(r"(?<!<)<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?",
                      lines[i])
        if m:
            delim = m.group(1)
            i += 1
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            if i < len(lines):
                out.append(lines[i])
        i += 1
    return "\n".join(out)


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _heredoc_delim(line, q):
    """Find an UNQUOTED heredoc opener in one line.

    Returns (delim or None, quote state at end of line). The quote state has to
    be threaded across lines by the caller, because a quoted string legally
    spans newlines.
    """
    delim, esc, j, n = None, False, 0, len(line)
    while j < n:
        ch = line[j]
        if q == "'":
            if ch == "'":
                q = None
            j += 1
            continue
        if esc:
            esc = False
            j += 1
            continue
        if ch == "\\":
            esc = True
            j += 1
            continue
        if q:
            if ch == q:
                q = None
            j += 1
            continue
        if ch in "'\"":
            q = ch
            j += 1
            continue
        if delim is None and line[j:j + 2] == "<<":
            if line[j:j + 3] == "<<<":
                j += 3               # here-STRING: no body follows
                continue
            m = HEREDOC_RE.match(line, j)
            if m:
                delim = m.group(2)
                j = m.end()
                continue
        j += 1
    return delim, q


def _strip_heredocs(command):
    """Remove heredoc BODIES. Their content is data being written, not commands
    being run — `tee f <<'EOF' ... imessage send ... EOF` sends nothing.

    QUOTE AWARE. A blind scan treated `--text "compare a << b"` as a heredoc
    opener and swallowed every following line, so the real sends after it were
    never archived.
    """
    lines = command.split("\n")
    out, q, i = [], None, 0
    while i < len(lines):
        out.append(lines[i])
        delim, q = _heredoc_delim(lines[i], q)
        i += 1
        if delim is not None:
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            if i < len(lines):
                out.append(lines[i])
                i += 1
            q = None      # a heredoc body cannot leave a quote open
    return "\n".join(out)


def _strip_comments_rest(command, _unused=None):
    """Line-wise comment stripping, quote aware."""
    kept = []
    q, esc = None, False
    for line in command.split("\n"):
        buf = []
        for ch in line:
            # inside single quotes a backslash is LITERAL and only `'` closes
            if q == "'":
                buf.append(ch)
                if ch == "'":
                    q = None
                continue
            if esc:
                buf.append(ch); esc = False; continue
            if ch == "\\":
                buf.append(ch); esc = True; continue
            if q:
                buf.append(ch)
                if ch == q:
                    q = None
                continue
            if ch in "'\"":
                q = ch; buf.append(ch); continue
            if ch == "#":
                break
            buf.append(ch)
        # quote state carries to the next line: a quoted message legally spans
        # newlines, and resetting per line made a `#` on line 2 of a multi-line
        # message delete the rest of the text
        esc = False
        kept.append("".join(buf))
    return "\n".join(kept)


def normalize_command(command):
    """Heredocs out, comments out, backslash-continuations joined."""
    c = _strip_heredocs(command)
    c = _strip_comments_rest(c, None)
    return re.sub(r"\\\n\s*", " ", c)


def split_segments(command):
    """Break a shell line into runnable segments, RESPECTING QUOTES.

    A quote-blind split truncates any message containing `;`, `|` or `&&` and
    can strip a trailing `--yes` off a real send, filing it as a draft.
    """
    segs, buf, q, esc, i = [], [], None, False, 0
    n = len(command)
    while i < n:
        ch = command[i]
        # A backslash inside SINGLE quotes is a literal character, not an
        # escape. Treating it as one let `'C:\\'` consume the closing quote and
        # swallow every command after it, losing the real send that followed.
        if q == "'":
            buf.append(ch)
            if ch == "'":
                q = None
            i += 1
            continue
        if esc:
            buf.append(ch); esc = False; i += 1; continue
        if ch == "\\":
            buf.append(ch); esc = True; i += 1; continue
        if q:
            buf.append(ch)
            if ch == q:
                q = None
            i += 1
            continue
        if ch in "'\"":
            q = ch; buf.append(ch); i += 1; continue
        two = command[i:i + 2]
        if two in ("&&", "||"):
            segs.append("".join(buf)); buf = []; i += 2; continue
        # A LONE `&` backgrounds one command and starts another, so `send A &
        # send B` is two messages to two different people. Merging them lost
        # one message AND forged it into the other person's record. `2>&1` and
        # `&>log` are redirects, not separators.
        if ch == "&" and command[i - 1:i] != ">" and command[i + 1:i + 2] != ">":
            segs.append("".join(buf)); buf = []; i += 1; continue
        if ch in ";|\n":
            segs.append("".join(buf)); buf = []; i += 1; continue
        buf.append(ch); i += 1
    segs.append("".join(buf))
    return [x.strip() for x in segs if x.strip()]


INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "bash", "sh",
                "zsh", "osascript"}

# basename of the executable -> channel
EXEC_CHANNEL = {
    "imessage": "imessage",
    "whatsapp_mautrix.py": "whatsapp",
    "slack_bridge.py": "slack",
}


def exec_argv(segment):
    """argv with `$(`/backtick wrappers stripped, so the first token is
    whatever is actually being executed.

    `VAR=value` prefixes are deliberately LEFT ALONE here and dropped by
    `_skip_wrappers` instead. Stripping them twice turned `env FOO=bar
    imessage send` into a bare `bar` sitting in command position, and the send
    was never archived.
    """
    out = []
    for tok in argv_of(segment.rstrip(") ")):
        t = re.split(r"\$\(|`", tok)[-1]
        out.append(t)
    while out and not out[0]:
        out.pop(0)
    return out


HELP_FLAGS = {"--help", "-h", "help"}

# Anything that can legally sit in front of the real executable. Without this,
# `for p in ...; do imessage send`, `env X=1 imessage send`, `timeout 30
# imessage send` and `sudo -u me imessage send` were TOTAL false negatives —
# the message was sent and never archived. This is an explicit allowlist on
# purpose: `echo imessage` must still match nothing.
SHELL_KEYWORDS = {"if", "then", "else", "elif", "fi", "for", "while", "until",
                  "do", "done", "case", "esac", "in", "select", "function",
                  "{", "}", "(", ")", "!", "coproc"}
WRAPPERS = {"env", "sudo", "doas", "command", "builtin", "nohup", "time",
            "nice", "ionice", "timeout", "gtimeout", "caffeinate", "stdbuf",
            "xargs", "exec", "setsid", "script"}
# runners whose second token is the literal `run`: `uv run whatsapp_mautrix.py`
RUNNERS = {"uv", "uvx", "poetry", "pipenv", "pdm", "rye", "hatch"}
# wrapper flags that consume the NEXT token. `-n` is deliberately absent:
# `sudo -n` is a boolean while `nice -n 5` is not, and the numeric rule below
# already covers `nice`. `-i` is absent because `env -i` is a boolean and
# eating the next token would swallow the executable itself.
WRAPPER_VALUE_FLAGS = {"-u", "-g", "-I", "-s", "-k", "--user", "--group",
                       "--signal", "--kill-after", "--chdir"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
NUMERIC_RE = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
# `python3.11`, `python3`, `node22` -> the interpreter family
VERSION_SUFFIX_RE = re.compile(r"[0-9.]+$")
INLINE_FLAGS = {"-c", "-e", "--eval", "--command"}
SHELL_INTERPRETERS = {"bash", "sh", "zsh", "dash", "ksh"}


def _skip_wrappers(argv):
    """Drop shell keywords, `VAR=` prefixes and wrapper commands so argv[0] is
    the thing that actually runs."""
    i, n = 0, len(argv)
    while i < n:
        tok = argv[i].strip("'\"")
        if not tok:
            i += 1
            continue
        if tok in SHELL_KEYWORDS:
            i += 1
            continue
        if ASSIGN_RE.match(tok):
            i += 1
            continue
        base = os.path.basename(tok)
        if base in WRAPPERS:
            i += 1
            numeric_ok = base in ("timeout", "gtimeout", "nice", "ionice")
            while i < n:
                t = argv[i].strip("'\"")
                if ASSIGN_RE.match(t):
                    i += 1
                    continue
                if t in WRAPPER_VALUE_FLAGS:
                    i += 2
                    continue
                if t.startswith("-") and t != "-":
                    i += 1
                    continue
                if numeric_ok and NUMERIC_RE.match(t):
                    i += 1
                    numeric_ok = False
                    continue
                break
            continue
        if base in RUNNERS and i + 1 < n and argv[i + 1].strip("'\"") == "run":
            i += 2
            continue
        break
    return argv[i:]


def _interp_family(base):
    return VERSION_SUFFIX_RE.sub("", base) or base


def _script_index(argv):
    """Index of the script an interpreter is running, skipping the
    interpreter's OWN flags. `python3 -u whatsapp_mautrix.py send ...` really
    sends; reading argv[1] blindly saw `-u` and archived nothing."""
    for j in range(1, len(argv)):
        a = argv[j].strip("'\"")
        if a in INLINE_FLAGS:
            return None          # inline code, not a script path
        if a.startswith("-"):
            continue
        return j
    return None


def _is_help(argv, ei):
    """Help flags count only right after the executable or right after its
    subcommand.

    Scanning the WHOLE argv meant a message bubble whose text was the word
    "help" cancelled the entire send — a three-bubble thread vanished because
    one bubble said "help".
    """
    nxt = argv[ei + 1:ei + 3]
    for j, tok in enumerate(nxt):
        if tok.strip("'\"") in HELP_FLAGS:
            if j == 0 or not nxt[0].startswith("-"):
                return True
    return False


def channel_of(argv):
    """Identify the channel from COMMAND POSITION only.

    Scanning a segment for the marker anywhere matches the CLI's name inside a
    message body, a URL, a `ls` argument or a heredoc — none of which run it.
    """
    argv = _skip_wrappers(argv)
    if not argv:
        return None
    ei, chan = None, None
    a0 = os.path.basename(argv[0].strip("'\""))
    if a0 in EXEC_CHANNEL:
        ei, chan = 0, EXEC_CHANNEL[a0]
    elif a0 == "gws" and len(argv) > 1 and argv[1] == "gmail":
        ei, chan = 1, "gmail"
    elif a0 == "gws-as" and len(argv) > 2 and argv[2] == "gmail":
        ei, chan = 2, "gmail"      # gws-as EMAIL gmail ...
    elif _interp_family(a0) in INTERPRETERS:
        si = _script_index(argv)
        if si is not None:
            sb = os.path.basename(argv[si].strip("'\""))
            if sb in EXEC_CHANNEL:
                ei, chan = si, EXEC_CHANNEL[sb]
    if chan is None:
        return None
    if _is_help(argv, ei):
        return None            # printing usage is not a conversation
    return chan


def shell_c_payload(seg):
    """`bash -c 'imessage send ...'` genuinely runs the CLI, so return the
    inner command string for a second pass. Language interpreters are excluded
    on purpose — see `is_inline_code`."""
    argv = _skip_wrappers(exec_argv(seg))
    if not argv:
        return None
    if os.path.basename(argv[0].strip("'\"")) not in SHELL_INTERPRETERS:
        return None
    for j in range(1, len(argv)):
        a = argv[j].strip("'\"")
        if a in ("-c", "-lc", "-ic", "-cl"):
            return argv[j + 1] if j + 1 < len(argv) else None
        if not a.startswith("-"):
            return None
    return None


def invoked(argv, marker):
    """True if `marker` is in EXECUTABLE position, not merely inside a string.

    `P='{"command":"imessage history --chat-id 5"}'` mentions the CLI inside a
    quoted JSON blob and runs nothing; `LAST=$(/path/imessage history ...)`
    genuinely runs it. Both survive shlex as a single token, so the test is what
    the token looks like once a `VAR=` prefix and a `$(`/backtick wrapper are
    stripped: a real invocation ends at the executable's own name.
    """
    if marker == "gws gmail":
        for i, tok in enumerate(argv[:-1]):
            if os.path.basename(tok) == "gws" and argv[i + 1] == "gmail":
                return True
            if (os.path.basename(tok) == "gws-as" and i + 2 < len(argv)
                    and argv[i + 2] == "gmail"):
                return True
        return False
    for tok in argv:
        cand = re.sub(r"^[A-Za-z_][A-Za-z0-9_]*=", "", tok)
        cand = re.split(r"\$\(|`", cand)[-1]
        if os.path.basename(cand.strip("\"'")) == marker:
            return True
    return False


# The hook is registered on matcher "Bash", so this runs on every shell command
# in every session. A per-character Python scan of a 1 MB command took 10.25s
# and blew the hook's 10s timeout — the message is then lost silently. These two
# guards keep the expensive path off anything that is not a comms command.
FAST_MARKERS = ("imessage", "whatsapp_mautrix.py", "slack_bridge.py", "gws")
# Re-measured 2026-09-09 after the hook's writes were batched: the 10.25s that
# forced a 64k bound was the per-row disk work, NOT this scan. Parsing 1.46 MB
# of chained sends costs 1.63s. The old bound was itself losing real messages —
# 2,000 chained sends truncated at 64k and dropped 1,097 of them.
MAX_SCAN = 1_000_000


def _prefilter(command):
    """Cheap C-level reject, then bound the work for huge commands."""
    if not any(m in command for m in FAST_MARKERS):
        return None
    if len(command) > MAX_SCAN:
        # Heredoc bodies FIRST. Dropping the `<<'EOF'` opener while keeping a
        # body line archived that line as a real sent message — forgery, not
        # just loss.
        command = _strip_heredocs_cheap(command)
        # Then join continuations: the marker sits on the first line and the
        # `--text` that holds the words sits on the next, so a marker-only
        # line filter deleted the message body.
        command = re.sub(r"\\\n\s*", " ", command)
        keep = [ln for ln in command.split("\n")
                if any(m in ln for m in FAST_MARKERS)]
        command = "\n".join(keep)
        if len(command) > MAX_SCAN:
            # Cut on a COMMAND boundary. A mid-line cut turned a real send into
            # a null row with no target and no text — silent corruption is
            # worse than the honest loss of everything past the bound. A
            # `;`-joined batch is all one line, so a newline is not enough.
            cut = max(command.rfind("\n", 0, MAX_SCAN),
                      command.rfind(";", 0, MAX_SCAN),
                      command.rfind("&&", 0, MAX_SCAN))
            command = command[:cut] if cut > 0 else ""
    return command


INLINE_CODE_FLAGS = {"-c", "-e", "--eval", "--command"}


def is_inline_code(command):
    """True if the whole command is an interpreter running a code string.

    Must be judged BEFORE splitting: a `;` or newline inside the code string
    produces fragments that look exactly like real invocations.
    """
    argv = argv_of(command)
    if not argv:
        return False
    base = _interp_family(os.path.basename(argv[0].strip("'\"")))
    # a SHELL running `-c` really does run the CLI inside it; only LANGUAGE
    # interpreters carry comms commands as inert string data
    if base not in INTERPRETERS or base in SHELL_INTERPRETERS:
        return False
    for a in argv[1:]:
        if a in INLINE_CODE_FLAGS:
            return True
        if not a.startswith("-"):
            return False        # a script path, not inline code
    return False


def detect_all(command, _depth=0):
    """Every runnable comms invocation in one command line.

    `send A ; send B` is two messages. Returning only the first loses the
    second forever, which is worse than not logging either.
    """
    command = _prefilter(command)
    if not command:
        return []
    if is_inline_code(command):
        return []
    hits = []
    for seg in split_segments(normalize_command(command)):
        chan = channel_of(exec_argv(seg))
        if chan:
            hits.append((chan, seg.rstrip(") ")))
            continue
        # `bash -c "imessage send ..."` sends for real. Depth is bounded so a
        # pathological nest cannot spin the hook past its 10s budget.
        if _depth < 2:
            inner = shell_c_payload(seg)
            if inner:
                hits.extend(detect_all(inner, _depth + 1))
    return hits


def detect(command):
    """First invocation only - kept for callers that want a single answer."""
    hits = detect_all(command)
    return hits[0] if hits else (None, None)


# Characters a backslash may escape INSIDE double quotes. Everywhere else a
# backslash escapes whatever follows it; inside single quotes nothing escapes.
_DQ_ESCAPES = '"\\$`\n'


def _tokenize(s):
    """Linear shell tokenizer, POSIX-compatible with `shlex.split`.

    `shlex.split` is QUADRATIC in token length: a 600 KB `--text` body took
    5.17s and, called twice per segment, blew the hook's 10s budget and lost
    the message. This does the same job in one pass.
    """
    toks, buf, q, esc, quoted = [], [], None, False, False
    for ch in s:
        if q == "'":
            if ch == "'":
                q = None
            else:
                buf.append(ch)
            continue
        if esc:
            if q == '"' and ch not in _DQ_ESCAPES:
                buf.append("\\")     # not an escape here; the backslash is data
            buf.append(ch)
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if q == '"':
            if ch == '"':
                q = None
            else:
                buf.append(ch)
            continue
        if ch in "'\"":
            q = ch
            quoted = True
            continue
        if ch.isspace():
            if buf or quoted:
                toks.append("".join(buf))
                buf, quoted = [], False
            continue
        buf.append(ch)
    if esc:
        buf.append("\\")
    if buf or quoted:
        toks.append("".join(buf))
    return toks


def argv_of(segment):
    try:
        return _tokenize(segment)
    except Exception:
        return segment.split()


def parse(channel, segment):
    """-> dict(verb, target, text, dry_run) with best-effort extraction."""
    argv = argv_of(segment)
    verb = None
    target = None
    text = None
    dry_run = False

    if channel == "gmail":
        # bare tokens after `gmail`, stopping at the first flag:
        #   `+send`                      -> "+send"
        #   `users messages send --json` -> "users.messages.send"
        bare = []
        seen_gmail = False
        for a in argv:
            if not seen_gmail:
                seen_gmail = (a == "gmail")
                continue
            if a.startswith("-") and not a.startswith("+"):
                break
            bare.append(a)
            if len(bare) >= 3:
                break
        # `users` is the API resource root and is optional in the shapes
        # actually used, so normalise it away rather than mapping both
        if bare and bare[0] == "users":
            bare = bare[1:]
        verb = ".".join(bare) if bare else None
        # A message/thread id is a valid key but a poor one — it fragments a
        # person's history across ids. Prefer an address; the hook recovers one
        # from the output headers, and only falls back to the id if there is
        # none.
        # a cc'd or bcc'd client received the mail and belongs in their own
        # history; only --to was ever read
        recips = []
        for fl, keys in (("--to", ("to", "To")), ("--cc", ("cc", "Cc")),
                         ("--bcc", ("bcc", "Bcc"))):
            recips += _flag_all(argv, fl)
            jf = _json_field(segment, keys)
            if jf:
                recips += [x.strip() for x in jf.split(",") if x.strip()]
        seen, recipients = set(), []
        for r in recips:
            k = norm(r)
            if k and k not in seen:
                seen.add(k)
                recipients.append(r)
        target = recipients[0] if recipients else None
        fallback = (_flag(argv, "--thread-id") or _flag(argv, "--message-id")
                    or _flag(argv, "--id")
                    or _json_field(segment, ("id", "threadId")))
        text = (_flag(argv, "--body") or
                _json_field(segment, ("body", "raw", "text")))
        # gws validates locally instead of sending when --dry-run is passed
        return {"verb": verb, "target": target, "text": text,
                "fallback": fallback, "recipients": recipients,
                "dry_run": "--dry-run" in argv}

    # positional subcommand = first bare token after the script/binary
    skip_next = False
    for i, a in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if a.startswith("-"):
            if "=" not in a and i + 1 < len(argv) and \
                    not argv[i + 1].startswith("-"):
                skip_next = True
            continue
        # the subcommand ends the search: `find send` and `group-rename Team send` send nothing
        if a in IGNORED.get(channel, ()):
            break
        if a in VERBS[channel]:
            verb = a
            rest = argv[i + 1:]
            target, text = _positional(channel, verb, rest)
            dry_run = ("--yes" not in rest and
                       (channel, verb) in DRYRUN_UNLESS_YES)
            break
    return {"verb": verb, "target": target, "text": text, "dry_run": dry_run}


def _flag_all(argv, name):
    """Every value of a repeatable flag, in order."""
    out = []
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            out.append(argv[i + 1])
        elif a.startswith(name + "="):
            out.append(a.split("=", 1)[1])
    return out


def _flag(argv, name):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def _json_field(segment, keys):
    """Pull a field out of a --json/--params payload.

    Handles `--json '{...}'`, `--json "{...}"`, `--params='{...}'` and a
    double-quoted payload with escaped inner quotes — all four shapes appear in
    the gws docs and only the first used to work.
    """
    blobs = []
    for m in re.finditer(r"--(?:json|params)(?:\s+|=)(['\"])(.*?)(?<!\\)\1",
                         segment, re.S):
        blobs.append(m.group(2))
    for m in re.finditer(r"--(?:json|params)(?:\s+|=)(\{.*?\})(?:\s|$)",
                         segment, re.S):
        blobs.append(m.group(1))
    for raw in blobs:
        for candidate in (raw, raw.replace('\\"', '"')):
            try:
                blob = json.loads(candidate)
            except Exception:
                continue
            if not isinstance(blob, dict):
                continue
            for k in keys:
                v = blob.get(k)
                if isinstance(v, list):
                    v = ", ".join(str(x) for x in v)
                if v:
                    return str(v)
    return None


def _positional(channel, verb, rest):
    """Pull (target, text) out of the args following the subcommand."""
    if channel == "imessage":
        argv = [verb] + rest
        # `--to HANDLE` is the ONE form that texts a number with no existing
        # chat. It had no extraction at all, so every first message to a new
        # contact was archived with no target and then dropped.
        tgt = (_flag(argv, "--chat-id") or _flag(argv, "--to")
               or _flag(argv, "--participant"))
        txt = None
        if verb in ("prepare-send", "send"):
            # both flags are repeatable and interleavable, one per bubble,
            # in send order — walk argv rather than taking the first match
            bubbles = []
            i = 0
            while i < len(argv):
                a = argv[i]
                # `--text=VALUE` is as valid as `--text VALUE`. Only the space
                # form was read, so the row looked healthy (--chat-id=104 DID
                # parse) while the words were silently missing.
                if a == "--text-file" and i + 1 < len(argv):
                    bubbles.append(_read_small(argv[i + 1]))
                    i += 2
                    continue
                if a.startswith("--text-file="):
                    bubbles.append(_read_small(a.split("=", 1)[1]))
                    i += 1
                    continue
                if a == "--text" and i + 1 < len(argv):
                    bubbles.append(argv[i + 1])
                    i += 2
                    continue
                if a.startswith("--text="):
                    bubbles.append(a.split("=", 1)[1])
                    i += 1
                    continue
                i += 1
            txt = "\n\n".join(b for b in bubbles if b) or None
        if verb == "send-preview":
            tgt = rest[0] if rest else None      # preview_id, resolved later
        return tgt, txt

    bare = [a for a in _drop_flags(rest)]
    if verb == "react" and channel in ("whatsapp", "slack"):
        # whatsapp: CHAT [EMOJI]; slack: CHANNEL TS [EMOJI]. The row's text says
        # what the owner did, so the thread reads as answered.
        emoji = bare[1 if channel == "whatsapp" else 2:][:1]
        if "--remove" in rest:
            return (bare[0] if bare else None), "[reaction removed]"
        return (bare[0] if bare else None), (f"[reaction {emoji[0]}]" if emoji else "[reaction]")
    if channel == "whatsapp":
        tgt = bare[0] if bare else None
        txt = None
        if verb == "send" and len(bare) > 1:
            txt = " ".join(bare[1:])
        elif verb in ("reply", "edit") and len(bare) > 2:   # CHAT EVENT WORDS
            txt = " ".join(bare[2:])
        elif verb == "send-media":
            txt = _flag([verb] + rest, "--caption")
        return tgt, txt
    if channel == "slack":
        tgt = bare[0] if bare else None
        txt = bare[1] if verb == "send" and len(bare) > 1 else None
        if verb == "edit":                                  # CHANNEL TS WORDS
            txt = bare[2] if len(bare) > 2 else None
        return tgt, txt
    return None, None


# Flags that take NO value. Everything else that looks like a flag is assumed
# to consume the next token, so an unknown `--reply-to ABC123` stops leaking
# ABC123 into the message text.
BOOLEAN_FLAGS = {"--yes", "-y", "--no-transcribe", "--dry-run", "--attachments",
                 "--page-all", "--json", "--verbose", "--quiet", "--force",
                 # whatsapp/slack `react` and `history --ids` (2026-10-28)
                 "--remove", "--money-ok", "--group-ok", "--ids"}
FLAG_RE = re.compile(r"^-{1,2}[A-Za-z][A-Za-z0-9-]*$")


REDIRECT_RE = re.compile(r"^(?:\d?>>?&?\d?|<|&>>?)(?:[^\s]*)$")


def _drop_flags(rest):
    """Bare (non-flag) tokens, in order.

    A token only counts as a flag if it LOOKS like one — `- item one, - item
    two` is a bulleted message, not an option, and dropping it archived a sent
    message with no content.
    """
    out, skip = [], False
    for tok in rest:
        if skip:
            skip = False
            continue
        if REDIRECT_RE.match(tok):     # 2>&1, >out, 2>/dev/null, <in
            continue
        if FLAG_RE.match(tok):
            if tok not in BOOLEAN_FLAGS:
                skip = True
            continue
        out.append(tok)
    return out


def _read_small(path, cap=8000):
    try:
        with open(os.path.expanduser(path)) as fh:
            return fh.read(cap)
    except Exception:
        return None


# ---------------------------------------------------------------- routing

def norm(target):
    """Normalise an identifier so the same person keys the same way.

    A jid or an email is already a stable unique key and is kept verbatim.
    Phone numbers keep their COUNTRY CODE — collapsing to the last 10 digits
    merged a UK number and a US number with the same last ten digits into one contact. Only the
    US `1` trunk prefix is dropped, so +1 206-555-0123 and 2065550123 still
    agree.
    """
    if not target:
        return None
    t = str(target).strip().strip("'\"")
    t = re.sub(r"\s+", " ", t)
    if not t:
        return None
    low = t.lower()
    if "@" in low or low.startswith("!"):
        return low
    if not re.fullmatch(r"[0-9+\-() .]+", t):
        return low
    digits = re.sub(r"[^0-9]", "", t)
    if not digits:
        return low
    if len(digits) == 11 and digits.startswith("1"):
        # The NANP trunk `1`. Guarded, because 11 digits starting with 1 is
        # ALSO the shape of a Chinese mobile (1[3-9]xxxxxxxxx). A real NANP
        # number can never have 0 or 1 as the first digit of its area code or
        # its exchange, so that rejects the malformed cases.
        # RESIDUAL, accepted: a Chinese mobile written bare (no `+86`) whose
        # digits happen to form a valid NANP number is still indistinguishable
        # from a US number. Written properly as `+86...` it is 13 digits and
        # never reaches here, and WhatsApp delivers it as a jid, which is kept
        # verbatim.
        if digits[1] in "23456789" and digits[4] in "23456789":
            return digits[1:]
        return digits
    if len(digits) < 10:
        return digits            # short numeric chat rowid (104, 280)
    return digits


def route(target, routes):
    key = norm(target)
    if not key:
        return None
    r = routes.get(key)
    if isinstance(r, dict):
        return r
    for v in routes.values():                    # alias list support
        if not isinstance(v, dict):
            continue
        if key in [norm(a) for a in (v.get("aliases") or [])]:
            return v
    return None


# ---------------------------------------------------------------- writing

def git_root(start):
    p = os.path.abspath(start or ".")
    for _ in range(24):
        if os.path.isdir(os.path.join(p, ".git")):
            return p
        nxt = os.path.dirname(p)
        if nxt == p:
            return None
        p = nxt
    return None


def append_jsonl(path, row):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    try:
        os.chmod(folder, 0o700)          # the filename listing is metadata too
    except Exception:
        pass
    with open(path, "a", errors="replace") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def append_jsonl_many(path, rows):
    """Append many rows in ONE open. Per-row open/chmod cost 14ms each, which
    is what blew the hook's 10s budget on a command chaining ~700 sends."""
    if not rows:
        return
    secure_dir(os.path.dirname(path))
    # Serialised against `comms`'s rewriters, which replace this file by
    # rename. Without it an append that lands after the rewriter reads the tail
    # and before it renames is written to the old inode and thrown away --
    # measured on a 26 MB store, a contiguous block of 423 rows vanished. The
    # hook writes ALL rows of one tool call in ONE append, so one lost append
    # is every message of that call. The wait is bounded and fails open, so a
    # stuck rewriter still cannot stop capture.
    with filelock(path, timeout=APPEND_LOCK_WAIT):
        with open(path, "a", errors="replace") as fh:
            fh.write("".join(json.dumps(r, ensure_ascii=False) + "\n"
                             for r in rows))
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def resolve_project(cwd, repos):
    """Return the project whose repo path is `cwd` or its nearest ancestor.

    `repos` maps an absolute repo path -> project name. The match is by path
    BOUNDARY, never raw string prefix: a sibling folder like `<repo>-other`
    does not belong to `<repo>`. When two repos both contain `cwd` (one nested
    inside the other), the LONGEST match wins. Returns None when no repo owns
    this folder -- the caller then falls back to an unscoped, all-contacts read.
    """
    if not cwd or not repos:
        return None
    cwd = os.path.normpath(cwd)
    best, best_len = None, -1
    for repo, project in repos.items():
        r = os.path.normpath(repo)
        if (cwd == r or cwd.startswith(r + os.sep)) and len(r) > best_len:
            best, best_len = project, len(r)
    return best


_ROOM_IN_BODY = re.compile(r"\[room (![^\]\s]+)\]")


def _archived_rows(key, store):
    """Archived rows whose `target` is `key`, newest first. A corrupt line is
    skipped, never fatal."""
    needle = '"target": %s' % json.dumps(key)
    try:
        months = sorted((f for f in os.listdir(store) if f.endswith(".jsonl")),
                        reverse=True)
    except OSError:
        return
    for month in months:
        try:
            with open(os.path.join(store, month), errors="replace") as fh:
                lines = [ln for ln in fh if needle in ln]
        except OSError:
            continue
        for ln in reversed(lines):
            try:
                row = json.loads(ln)
            except ValueError:
                continue
            if row.get("target") == key:
                yield row


def reader_target(thread, store=None, routes=None):
    """The id a READER must pass to the channel tool for this thread record.

    norm() lowercases a Matrix `!room` id so it keys stably, but room ids are
    case-sensitive: the lowercased key makes the bridge answer M_NOT_FOUND and
    the room reads as empty, with only a note to say so. Every archived row
    keeps the id as typed in `raw_target`, so the original case comes back
    from the newest row that has it.

    WhatsApp chats can also be keyed by a NAME or a `<lid>@lid` alias. A name
    row's body holds the room the tool opened (`[room !…]`), and the route
    that owns an alias names its room, so both are read as that room. That is
    what lets reader_rooms() read a chat once however it was saved. Anything
    else passes through unchanged.
    """
    key = thread.get("target") or ""
    if thread.get("raw_target"):
        return thread["raw_target"]
    store = store or STORE
    if key.startswith("!"):
        for row in _archived_rows(key, store):
            raw = row.get("raw_target")
            if isinstance(raw, str) and raw.lower() == key:
                return raw
        return key
    if thread.get("channel") != "whatsapp":
        return key
    if key.endswith("@lid"):
        for rkey, route in (routes or {}).items():
            if (rkey.startswith("!") and key in
                    [norm(a) for a in (route or {}).get("aliases") or []]):
                return reader_target({"channel": "whatsapp", "target": rkey},
                                     store=store)
        return key
    for row in _archived_rows(key, store):
        m = _ROOM_IN_BODY.search(row.get("body") or "")
        if m:
            return m.group(1)
    return key


def reader_rooms(rooms, store=None, routes=None):
    """[(room, reader_target)] with each chat read once.

    One chat can hold two thread records (its room id and an alias), and
    reading both prints every message twice. The first record wins; rooms
    arrive newest first.
    """
    out, seen = [], set()
    for r in rooms:
        t = reader_target(r, store=store, routes=routes)
        k = (r.get("channel"), str(t).lower())
        if k in seen:
            continue
        seen.add(k)
        out.append((r, t))
    return out


def catalog_for_project(project, threads, channel=None):
    """Rooms that belong to `project`, newest activity first.

    `threads` is the threads.json dict (target -> thread record). Returns the
    matching thread records as a list, optionally narrowed to one `channel`.
    project=None (an unscoped folder) yields [] so the caller reads everything.
    """
    if not project:
        return []
    out = [t for t in threads.values()
           if t.get("project") == project
           and (channel is None or t.get("channel") == channel)]
    out.sort(key=lambda t: t.get("last_ts") or 0, reverse=True)
    return out


def _under_prefix(path, prefixes):
    """True if `path` is one of `prefixes` or sits inside one (path-boundary)."""
    p = os.path.normpath(path)
    for pre in prefixes or ():
        pre = os.path.normpath(pre)
        if p == pre or p.startswith(pre + os.sep):
            return True
    return False


def suggest_repos(rows, exclude_prefixes=(), min_share=0.6):
    """Propose a client-repo -> project link from archived rows.

    Each row carries `project` (stamped at capture) and `cwd` (where the tool
    ran). A link is proposed only when one project OWNS a folder: its messages
    are >= `min_share` of that folder's traffic AND that folder's busiest
    project is itself. Anything short of that -- a folder two projects share, a
    project with no folder of its own -- is returned as a conflict for the owner,
    never auto-linked. Folders under `exclude_prefixes` (the hub, ~/.claude) are
    not client repos and are skipped.

    Returns (links, conflicts):
      links     = {abs_cwd: project}
      conflicts = [{project, cwd, share, owner}, ...]
    """
    from collections import Counter
    proj_cwds, cwd_projs = {}, {}
    for r in rows:
        proj, cwd = r.get("project"), r.get("cwd")
        if not proj or not cwd or _under_prefix(cwd, exclude_prefixes):
            continue
        proj_cwds.setdefault(proj, Counter())[cwd] += 1
        cwd_projs.setdefault(cwd, Counter())[proj] += 1
    links, conflicts = {}, []
    for proj, cwds in proj_cwds.items():
        top_cwd, n = cwds.most_common(1)[0]
        share = n / sum(cwds.values())
        owner = cwd_projs[top_cwd].most_common(1)[0][0]
        if share >= min_share and owner == proj:
            links[top_cwd] = proj
        else:
            conflicts.append({"project": proj, "cwd": top_cwd,
                              "share": round(share, 2), "owner": owner})
    return links, conflicts


def active_rooms(rooms, days, now_ts=None):
    """Rooms with activity within `days` (0/None = all), order preserved."""
    if not days:
        return list(rooms)
    if now_ts is None:
        now_ts = time.time()
    cutoff = now_ts - days * 86400
    # A room with no last_ts is not provably recent -> exclude from the window.
    return [r for r in rooms if r.get("last_ts") and r["last_ts"] >= cutoff]


def _iso_to_et(s):
    """A UTC ISO-8601 stamp (Z/offset) -> 'YYYY-MM-DD HH:MM PDT' in the machine's
    local zone, labeled with it (comms reads iMessage, so it runs on the Mac, whose
    zone follows the owner as he travels). Naive
    or unparseable input is returned unchanged rather than mislabeled."""
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return s
    if dt.tzinfo is None:
        return s
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _iso_to_epoch(s):
    """A UTC/offset ISO-8601 stamp (iMessage `created_at`) -> Unix epoch, or None
    for a naive or unparseable value. None (never a guessed epoch) is what lets
    the merged feed refuse to sort a message it cannot honestly time."""
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


def _im_fields(m):
    """(who, text, me) for one iMessage message dict. Shared by the grouped
    formatter and the record builder so the two cannot drift on who said what."""
    me = bool(m.get("is_from_me"))
    who = "Me" if me else (m.get("sender_name") or m.get("sender") or "?")
    text = (m.get("text") or "").strip()
    if not text and m.get("attachments"):
        text = "[attachment]"
    return who, text, me


def format_imessage(payload, limit):
    """iMessage history JSON -> compact '[time] who: text' lines, last `limit`."""
    msgs = (payload or {}).get("messages") or []
    if limit:
        msgs = msgs[-limit:]
    out = []
    for m in msgs:
        who, text, _ = _im_fields(m)
        out.append(("[%s] %s: %s"
                    % (_iso_to_et(m.get("created_at") or ""), who, text))
                   .rstrip())
    return out


def imessage_records(payload, limit):
    """iMessage history JSON -> merge-feed records, last `limit`. Each carries the
    real epoch from `created_at`; the grouped view keeps using format_imessage."""
    msgs = (payload or {}).get("messages") or []
    if limit:
        msgs = msgs[-limit:]
    out = []
    for m in msgs:
        who, text, me = _im_fields(m)
        out.append({"ts": _iso_to_epoch(m.get("created_at") or ""),
                    "channel": "imessage", "who": who, "text": text, "me": me})
    return out


def _epoch_to_et(ts):
    """A Unix epoch (Slack `ts`) -> 'YYYY-MM-DD HH:MM PDT' in local (Mac) time, labeled."""
    try:
        return datetime.datetime.fromtimestamp(float(ts)).astimezone().strftime(
            "%Y-%m-%d %H:%M %Z")
    except Exception:
        return ""


def _as_epoch(x):
    """A best-effort float epoch, or None. Keeps the merged feed from sorting a
    bad `ts` as if it were epoch 0 (1970, wrongly 'oldest')."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _slack_fields(m):
    """(who, text) for one Slack message dict. Shared by the grouped formatter
    and the record builder."""
    who = m.get("sender") or "?"
    text = " ".join((m.get("text") or "").split())
    if not text:
        text = ("[voice note]" if m.get("isVoiceNote")
                else "[attachment]" if m.get("attachments") else "")
    return who, text


def format_slack(messages, limit):
    """Slack buffered-message dicts -> '[time] sender: text' lines, last `limit`."""
    if limit:
        messages = messages[-limit:]
    out = []
    for m in messages:
        who, text = _slack_fields(m)
        out.append(("[%s] %s: %s" % (_epoch_to_et(m.get("ts")), who, text))
                   .rstrip())
    return out


def run_phased(tasks, first=frozenset(), workers=8):
    """Run [(channel, fn)] tasks `workers` at a time; return results in task order.

    Tasks whose channel is in `first` run on their own before the rest start. On
    2026-10-26, at load 183 on 18 cores, a client with 19 Slack rooms had every
    reader start at once and both iMessage reads passed their 45 s limit, while
    iMessage alone took 25 s. Reading the local channel first, then the network
    ones, costs a few seconds and keeps iMessage in the feed."""
    import concurrent.futures
    results = [None] * len(tasks)
    phases = [[i for i, (ch, _fn) in enumerate(tasks) if ch in first],
              [i for i, (ch, _fn) in enumerate(tasks) if ch not in first]]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for idx in phases:
            for i, res in zip(idx, ex.map(lambda i: tasks[i][1](), idx)):
                results[i] = res
    return results


SLACK_REPLY_MARK = "↳ "


def slack_feed_messages(stdout):
    """`slack_bridge.py threads CHANNEL --json` output -> buffer-shaped Slack dicts
    ({ts, sender, text}), oldest first, thread replies included and marked.

    The bridge buffer (`history`) holds top-level messages only, and on 2026-10-26 it
    held none for a client's DMs or a channel, so this live read is what
    `comms unread` uses. A reply sorts by its own time, so a new answer in an old
    thread still lands at the top of a newest-first feed. Returns None when the
    output is not a feed (ssh failed, bridge down), so the caller can fall back to
    the buffer and SAY so; a quiet channel is [] instead."""
    lines = [ln for ln in (stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return None
    out = []
    for m in payload["messages"]:
        if not isinstance(m, dict):
            continue
        text = m.get("text") or ""
        if m.get("thread_ts") and text:
            text = SLACK_REPLY_MARK + text
        msg = {"ts": m.get("ts"), "sender": m.get("name") or "?", "text": text}
        if m.get("files"):
            msg["attachments"] = [True] * int(m.get("files") or 1)
        out.append(msg)
    out.sort(key=lambda d: _as_epoch(d.get("ts")) or float("-inf"))
    return out


def slack_records(messages, limit):
    """Slack dicts -> merge-feed records, last `limit`. The Slack `ts` is already
    a Unix epoch, so it survives straight into the record."""
    if limit:
        messages = messages[-limit:]
    out = []
    for m in messages:
        who, text = _slack_fields(m)
        out.append({"ts": _as_epoch(m.get("ts")), "channel": "slack",
                    "who": who, "text": text, "me": False})
    return out


def project_emails(project, routes):
    """Every email address tied to `project` in routes.json -- its route keys
    and aliases that look like addresses, lowercased, sorted, deduped. Turns
    'any new email from this client?' into a real Gmail `from:(...)` search
    instead of re-reading known threads. project=None -> [] so an unowned
    folder searches the whole inbox."""
    if not project:
        return []
    emails = set()
    for key, v in (routes or {}).items():
        v = v or {}
        if v.get("project") != project:
            continue
        for cand in [key] + list(v.get("aliases") or []):
            if isinstance(cand, str) and "@" in cand:
                emails.add(cand.lower())
    return sorted(emails)


def _rfc2822_to_et(s):
    """A Gmail Date header ('Tue, 15 Sep 2026 13:57:32 -0300') -> 'YYYY-MM-DD
    HH:MM PDT' in the machine's local zone, labeled with it. The sender's own
    offset must never reach the owner; only the Mac's zone does. Naive or unparseable input is
    returned unchanged rather than mislabeled."""
    try:
        dt = email.utils.parsedate_to_datetime(s)
    except Exception:
        return s
    if dt is None or dt.tzinfo is None:
        return s
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _rfc2822_to_epoch(s):
    """A Gmail Date header -> Unix epoch, or None if naive/unparseable."""
    try:
        dt = email.utils.parsedate_to_datetime(s)
    except Exception:
        return None
    if dt is None or dt.tzinfo is None:
        return None
    return dt.timestamp()


def gmail_record(frm, subject, date_hdr, account=None):
    """One 'new mail from this client' hit -> a merge-feed record. The subject is
    the text so a gmail row reads like every other channel; the Date header is
    parsed to an epoch (never the sender's raw offset)."""
    rec = {"ts": _rfc2822_to_epoch(date_hdr or ""), "channel": "gmail",
           "who": frm or "?", "text": subject or "(no subject)", "me": False}
    if account:
        rec["account"] = account
    return rec


def _wall_et_to_epoch(s):
    """A naive local wall-clock 'YYYY-MM-DD HH:MM' -> Unix epoch, interpreted in
    the machine's own zone (the Mac's current zone). This is exactly what WhatsApp's
    history prints (see whatsapp_mautrix.py `_format_ts`), which has no
    structured mode. None for an unparseable value, so a line the merge cannot
    time is surfaced rather than sorted at a faked instant."""
    try:
        return datetime.datetime.strptime((s or "").strip(),
                                          "%Y-%m-%d %H:%M").timestamp()
    except Exception:
        return None


# WhatsApp's history line: '[YYYY-MM-DD HH:MM] Sender: body'. Sender is
# non-greedy up to the FIRST ':' so a colon inside the body stays in the body.
# The body is OPTIONAL: a media-only / empty message prints as '[ts] Sender:'
# with nothing after the colon (the trailing space is stripped upstream), and it
# is still a real, timestamped message -- not a note, and not a continuation of
# the message above it.
WA_LINE_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d)\] (.*?):(?: (.*))?$")


def parse_whatsapp_records(lines):
    """WhatsApp `history` text lines -> (records, notes).

    WhatsApp is the one channel with no JSON/structured mode, so its printed
    '[time] who: body' lines are parsed back into records. A message body that
    contained newlines arrives as extra lines with NO timestamp of their own --
    they belong to the message above, so they are appended to it and inherit its
    epoch. A line that matches nothing and has no message above it (an error, or
    an empty-state sentinel such as '(whatsapp unavailable: ...)' / '(nothing
    recent)') is returned as a NOTE, never a record: giving it a faked timestamp
    would sort a non-message into the feed and misstate when things happened.
    """
    records, notes = [], []
    cur = None
    for ln in lines:
        m = WA_LINE_RE.match(ln)
        if m:
            cur = {"ts": _wall_et_to_epoch(m.group(1)), "channel": "whatsapp",
                   "who": m.group(2), "text": m.group(3) or "[no text]",
                   "me": False}
            records.append(cur)
        elif cur is not None:
            cur["text"] += "\n" + ln          # continuation of the message above
        else:
            notes.append(ln)                  # sentinel / pre-message noise
    return records, notes


def merge_feed(records, limit=None):
    """Records from every channel -> ONE newest-first list of rendered lines.

    Input records are {ts: float|None, channel, who, text, me}. Sorted by `ts`
    descending; a record with no numeric `ts` sinks to the bottom and renders an
    empty time bracket rather than borrowing a plausible one. Every row is
    rendered through `_epoch_to_et` -- the same single local-zone path every other view
    uses -- and its text is whitespace-collapsed so one message is one row."""
    def _key(r):
        ts = r.get("ts")
        return ts if isinstance(ts, (int, float)) else float("-inf")
    ordered = sorted(records, key=_key, reverse=True)
    if limit:
        ordered = ordered[:limit]
    out = []
    for r in ordered:
        text = " ".join(str(r.get("text") or "").split())
        line = "[%s] %s/%s: %s" % (_epoch_to_et(r.get("ts")),
                                   r.get("channel") or "?",
                                   r.get("who") or "?", text)
        if r.get("account"):
            line += " (account: %s)" % r["account"]
        out.append(line)
    return out


def note_unrouted_many(items):
    """One lock for every new unrouted contact, instead of one lock each."""
    items = [(t, c, w) for (t, c, w) in items if t]
    if not items:
        return
    lock = None
    try:
        lock = filelock(UNROUTED)
        lock.__enter__()
        cur = open(UNROUTED).read() if os.path.exists(UNROUTED) else ""
        if not cur:
            cur = ("# Unrouted contacts\n\nNo project assigned yet. Assign "
                   "one with:\n\n    comms route <target> <project> "
                   "--path /abs/repo/path\n\n"
                   "| Target | Channel | Seen from |\n| --- | --- | --- |\n")
        added = []
        for target, channel, cwd in items:
            key = norm(target)
            if not key or "`%s`" % key in cur or "`%s`" % key in "".join(added):
                continue
            added.append("| `%s` | %s | %s |\n" % (key, channel or "",
                                                   cwd or ""))
        if added:
            with open(UNROUTED, "w") as fh:
                fh.write(cur + "".join(added))
            os.chmod(UNROUTED, 0o600)
    except Exception:
        pass
    finally:
        if lock:
            try:
                lock.__exit__(None, None, None)
            except Exception:
                pass


def ensure_gitignore(repo):
    gi = os.path.join(repo, ".gitignore")
    try:
        cur = open(gi).read() if os.path.exists(gi) else ""
        if DIR_NAME not in cur:
            with open(gi, "a") as fh:
                fh.write(("" if cur.endswith("\n") or not cur else "\n") +
                         DIR_NAME + "/\n")
    except Exception:
        pass


def write_index(folder, threads, project=None):
    rows = [t for t in threads.values()
            if project is None or t.get("project") == project]
    rows.sort(key=lambda r: r.get("last_ts", 0), reverse=True)
    lines = ["# Communications index",
             "",
             "Newest first. Query with `comms find <text>` or "
             "`comms thread <target>` — never `cat` the .jsonl.",
             "",
             "| Last | Channel | Target | Label | Msgs | Project |",
             "| --- | --- | --- | --- | --- | --- |"]
    for r in rows[:400]:
        lines.append("| {} | {} | `{}` | {} | {} | {} |".format(
            r.get("last_et", "")[:16], r.get("channel", ""),
            r.get("target", ""), r.get("label", "") or "—",
            r.get("count", 0), r.get("project", "") or "—"))
    try:
        os.makedirs(folder, exist_ok=True)
        idx = os.path.join(folder, "INDEX.md")
        with open(idx, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(idx, 0o600)
    except Exception:
        pass


def note_unrouted(target, channel, cwd):
    lock = None
    try:
        key = norm(target)
        lock = filelock(UNROUTED)
        lock.__enter__()
        cur = open(UNROUTED).read() if os.path.exists(UNROUTED) else ""
        if "`%s`" % key in cur:
            return
        if not cur:
            cur = ("# Unrouted contacts\n\nNo project assigned yet. Assign "
                   "one with:\n\n    comms route <target> <project> "
                   "--path /abs/repo/path\n\n"
                   "| Target | Channel | Seen from |\n| --- | --- | --- |\n")
        cur += "| `%s` | %s | %s |\n" % (key, channel, cwd or "")
        with open(UNROUTED, "w", errors="replace") as fh:
            fh.write(cur)
        os.chmod(UNROUTED, 0o600)
    except Exception:
        pass
    finally:
        if lock is not None:
            try:
                lock.__exit__(None, None, None)
            except Exception:
                pass


# ------------------------------------------------------------------ mcp

# MCP calls reach PostToolUse too (verified 2026-09-09), but the payload is a
# different shape: args arrive as a parsed dict in tool_input, and the response
# is a LIST of content blocks, not stdout/stderr. Adding another MCP comms
# source is one entry here plus its matcher in settings.json.
MCP_TOOLS = {
    "mcp__upwork__upwork__get_messages": {
        "channel": "upwork",
        # list_rooms / search_rooms / find_room are discovery, not conversation
        "dirs": {"list_messages": "read", "get_message": "read"},
    },
    "mcp__upwork__upwork__send_message": {
        "channel": "upwork",
        # create_room makes a room but carries no message
        "dirs": {"send": "send", "send_to_user": "send",
                 "message_proposal": "send", "edit": "send"},
    },
}

MCP_TARGET_KEYS = ("room_id", "user_id", "proposal_id", "job_posting_id")


def parse_mcp(tool_name, tool_input):
    """-> dict(channel, verb, direction, target, text) or None."""
    spec = MCP_TOOLS.get(tool_name)
    if not spec:
        return None
    ti = tool_input if isinstance(tool_input, dict) else {}
    action = ti.get("action")
    direction = spec["dirs"].get(action)
    if not direction:
        return None
    params = ti.get("params")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    if not isinstance(params, dict):
        params = {}
    target = None
    for k in MCP_TARGET_KEYS:
        v = params.get(k)
        if v:
            target = v if isinstance(v, str) else json.dumps(v, sort_keys=True)
            break
    text = params.get("message")
    if isinstance(text, (dict, list)):
        text = json.dumps(text)
    return {"channel": spec["channel"], "verb": action,
            "direction": direction, "target": target, "text": text}


def body_of(resp):
    """Flatten any tool_response shape into text. Bash gives stdout/stderr;
    MCP gives a list of content blocks."""
    if isinstance(resp, dict) and "content" not in resp:
        def _txt(v):
            if v is None:
                return ""
            return v if isinstance(v, str) else json.dumps(v, default=str)
        return (_txt(resp.get("stdout")) + _txt(resp.get("stderr")),
                bool(resp.get("interrupted")))
    if isinstance(resp, dict) and isinstance(resp.get("content"), list):
        resp = resp["content"]
    if isinstance(resp, list):
        out = []
        for b in resp:
            if isinstance(b, dict):
                out.append(str(b.get("text") or b.get("content") or
                               json.dumps(b)))
            else:
                out.append(str(b))
        return ("\n".join(out), False)
    return (str(resp or ""), False)


# ------------------------------------------------- unexpanded shell variables

# The hook reads the command's *source text*, so `send "$ROOM" "..."` logs the
# literal `$ROOM` — the shell expanded it, we never saw the result. When that
# happens, recover the real identifier from the command's own output.
ID_PATTERNS = (
    r"![A-Za-z0-9_\-]{8,}(?::[^\s\]]+)?",           # matrix room id
    r"\b\d{6,20}@(?:lid|s\.whatsapp\.net)\b",       # whatsapp dm jid
    r"\b[\w.\-]+@g\.us\b",                          # whatsapp group jid
    r"\b[CDG][A-Z0-9]{7,}\b",                        # slack channel / dm id
    r"\b[\w.+\-]+@[\w\-]+\.[a-z]{2,}\b",            # email address
)


# The iMessage engine's own record of every send, keyed by the `preview_id` it
# prints. This is more faithful than its stdout: `payload.texts` holds ONLY
# real text, while the output's flat `parts` array mixes an attachment's
# filename in among the message bubbles.
PI_PREVIEWS = os.path.join(HOME, ".pi-2", "agent", "state", "imessage",
                           "previews")
HEX_ID_RE = re.compile(r"^[0-9a-f]{16,64}$")
PREVIEW_ID_RE = re.compile(
    r'"preview_id"\s*:\s*"([0-9a-fA-F]{16,64})"(?![0-9a-fA-F])')
PREVIEW_ID_LOOSE = re.compile(
    r"preview[_ ]?id[\"':=\s]+([0-9a-fA-F]{16,64})(?![0-9a-fA-F])", re.I)


def preview_id_of(body):
    """First preview id in a blob. Callers holding several sends must scope the
    blob first — see `im_report`."""
    m = PREVIEW_ID_RE.search(body or "") or PREVIEW_ID_LOOSE.search(body or "")
    # the loose form is case-insensitive, so it matched an uppercase id that
    # the anchored lowercase lookup then rejected; and a 65-char id used to be
    # truncated to 64 and looked up as a DIFFERENT id
    return m.group(1).lower() if m else None


# ONLY `sent`. Measured 2026-09-09: all 3 files in `pending/` were abandoned or
# edited-away drafts that never went out — one shared a `baseline_rowid` with a
# sent message whose text was completely different — so reading them would
# archive words nobody received. `sending/` was empty, so its schema is
# unverified and it is treated as unknown rather than assumed safe.
PREVIEW_SENT = "sent"


def preview_payload(preview_id):
    """The engine's stored payload for one DELIVERED preview id, or None."""
    if not preview_id or not HEX_ID_RE.match(preview_id):
        return None
    p = os.path.join(PI_PREVIEWS, PREVIEW_SENT, preview_id + ".json")
    try:
        if not os.path.isfile(p):
            return None
        with open(p) as fh:
            d = json.load(fh)
        pl = d.get("payload")
        return pl if isinstance(pl, dict) else None
    except Exception:
        return None


def payload_texts(pl):
    """Bubbles out of a payload, tolerating both stored schemas.

    schema_version 1 wrote `text` as a single string; 2 and 3 write a `texts`
    list. Reading only `texts` returned nothing at all on a v1 record.
    """
    if not isinstance(pl, dict):
        return []
    raw = pl.get("texts")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        one = pl.get("text")
        raw = [one] if isinstance(one, str) else []
    return [t for t in raw if isinstance(t, str) and t.strip()]


# `About to send to NAME [room !ID]: "TEXT"` then `Sent. event_id=$...`, one
# pair per bubble. Verified against 7 real captures.
# group 1 is the contact as the bridge printed it, group 2 the room id. The
# name is captured because a row's target is usually the name the command line
# used, and pinning a bubble to ONE row is the whole point (see `_wa_verdict`).
WA_ECHO_RE = re.compile(
    r'^About to send to (?:GROUP )?"?([^\n]{0,200}?)"?[ \t]*'
    r'\[room ([^\]\n]+)\][ \t]*:[ \t]*"', re.M)
WA_SENT_RE = re.compile(r"Sent\. event_id=\S+")
# The first line the bridge prints AFTER an echoed message. Everything before
# the LAST of them is the message.
# Bounded and newline-free on purpose. The old `(.*?)` was compiled with re.S
# and unbounded, so every `About to send to ` that was NOT followed by
# `[room ...]` re-scanned to the end of the body before failing: 6,000 such
# lines in one 108 KB blob cost 18.7 SECONDS, against a 10-second hook budget,
# and a timeout loses every row in the call. The echo HEADER is always one
# line; only the message spans lines, and that is handled separately below.
WA_AFTER_RE = re.compile(
    r"^(?:Sent\. event_id="
    r"|Dry run "
    r"|Matrix API error: "
    r"|Voice memo error: "
    r"|Cannot reach "
    r"|No contact matches "
    r"|No management room"
    r"|Refusing to send an empty"
    r"|File not found: "
    r"|Upload returned no content_uri"
    r"|Disambiguate by "
    r"|--mention '"
    r"|Candidates \(id )"
    r"|NOT SENT: ", re.M)
# The end of an echoed message: the bridge closes the quote as the LAST
# character of that line. `rfind('"')` alone landed inside a quote in the
# client's own words, or inside the bridge's `content: {...}` debug dump.
WA_CLOSE_RE = re.compile(r'"(?=\n|\Z)')
# Every way the bridge says a bubble did NOT go out, each on its own line.
# Read against the source, not guessed: `whatsapp_mautrix.py` 283/954/1014
# (Cannot reach homeserver), 883 (`(N portal matches)` -- a DIFFERENT wording
# from 2449's `(N matches)`), 2005, 2446, 2449, 2492, 2636, 2752, 2756, 2811,
# 3237, 3240. Five of these were missing and a send refused for any of them was
# archived as delivered; `Cannot reach homeserver` needs no attacker at all,
# just a network blip. The bare phrase "refusing to send" stays out: the owner
# texting "the tool keeps refusing to send" must not delete his own message.
WA_FAIL_RE = re.compile(
    r"^Matrix API error: "
    r"|^Voice memo error: "
    r"|^Cannot reach (?:homeserver|whisper service)\b"
    r"|^No contact matches\b"
    r"|^No management room\b"
    r"|^Refusing to send an empty\b"
    r"|^File not found: "
    r"|^Upload returned no content_uri\b"
    r"|^--mention '[^']*' [^\n]*\brefusing to guess\."
    r"|^'[^']*' is ambiguous \(\d+(?: portal)? matches\)"
    # `send --since` (2026-09-23): the live re-read saw a newer message, could not
    # read the chat, or --yes came without --since. Nothing went out.
    r"|^NOT SENT: ", re.M)
# ...and the ones that NAME the recipient they refused, so a batch's refusal
# can be pinned to one row instead of applied to every row sharing the blob.
WA_FAIL_NAMED_RE = re.compile(
    r"^No contact matches '([^']*)'"
    r"|^'([^']*)' is ambiguous \(\d+(?: portal)? matches\)", re.M)


def _wa_regions(body):
    """-> (bubbles, tool_text) for one WhatsApp output blob.

    A bubble is (room, name, text, delivered, after), where `after` is the
    bridge's own words between this echo and the next one -- the only place
    this bubble's confirmation or refusal can be.

    `tool_text` is the body with every echoed MESSAGE cut out, i.e. everything
    the tools said and nothing the client did. Scanning the whole body instead
    let a message that quoted a CLI error line delete itself.

    Scanned rather than matched line by line: a message containing a newline
    never closes its quote on the opening line, and a line-anchored pattern
    recovered NOTHING from it. The owner's WhatsApp messages are routinely
    multi-line.
    """
    body = body or ""
    bubbles, quoted = [], []
    starts = list(WA_ECHO_RE.finditer(body))
    for i, m in enumerate(starts):
        begin = m.end()                       # just past the opening quote
        stop = starts[i + 1].start() if i + 1 < len(starts) else len(body)
        chunk = body[begin:stop]
        # The LAST marker, not the first. A message whose own text contains a
        # line that looks like a bridge marker cut the message there: it was
        # truncated AND its own words stayed in `tool_text`, where a quoted
        # `No contact matches 'Alex'` then deleted Alex's delivered
        # message. The bridge's output is one block at the END of the chunk.
        edges = list(WA_AFTER_RE.finditer(chunk))
        head = chunk[:edges[-1].start()] if edges else chunk
        close = -1
        if edges:
            last = None
            for last in WA_CLOSE_RE.finditer(head):
                pass
            close = last.start() if last is not None else head.rfind('"')
        # `exact` means the bridge's own next line was found, so the closing
        # quote really is the end of the message. Without it the message is
        # unterminated as far as this parser can tell -- the bridge printed
        # nothing after the echo, or a DIFFERENT tool's output follows in a
        # chained command -- and cutting there would hide that other tool's
        # error report from its own row.
        exact = close >= 0
        if not exact:
            close = chunk.rfind('"')
        if close < 0:
            continue
        after = chunk[close:]
        if exact:
            quoted.append((begin, begin + close))
        bubbles.append((m.group(2), m.group(1) or "", chunk[:close],
                        bool(WA_SENT_RE.search(after)), after))
    keep, at = [], 0
    for lo, hi in quoted:
        keep.append(body[at:lo])
        at = hi
    keep.append(body[at:])
    return bubbles, "\n".join(keep) if quoted else body


def wa_events(body):
    """(room, text, delivered) per bubble, in output order."""
    return [(b[0], b[2], b[3]) for b in _wa_regions(body)[0]]


def _wa_ids(bubble):
    """Every identifier that could name this bubble's recipient.

    The bridge labels a DM `12065551234 (Dan)` (whatsapp_mautrix.py:891), so a
    row addressed by the bare number matched neither the room id nor the whole
    label, and every number-addressed send in a batch was unjudgeable.
    """
    name = bubble[1] or ""
    ids = [bubble[0], name]
    m = re.match(r"^\s*(.+?)\s*\((.+)\)\s*$", name)
    if m:
        ids.extend([m.group(1), m.group(2)])
    return [norm(x) for x in ids if norm(x)]


def _wa_mine(bubbles, tool_text, target, lone_ok=True):
    """The bubbles that belong to THIS row -> list, possibly empty.

    Pinned by room id first, then by the name the bridge echoed -- a row whose
    target came off the command line is usually the NAME, and matching only the
    room id lost the text of every send addressed that way.

    A target that is a phone number or an unexpanded `$VAR` matches neither, so
    a LONE send in the output is taken to be this row's. But not when something
    in the output names a recipient it refused: that is exactly the case where
    the one echo present belongs to somebody else.
    """
    if not target:
        return bubbles
    key = norm(target)
    hit = [b for b in bubbles if key in _wa_ids(b)]
    if hit:
        return hit
    # `lone_ok` is the keep-or-drop side only. Recovering TEXT off an
    # unidentified bubble archived a mangled string built out of a hostile
    # WhatsApp pushname; refusing to guess leaves the row with no text, which
    # is visible in a way a wrong value is not.
    if lone_ok and len(bubbles) == 1 and not WA_FAIL_NAMED_RE.search(
            tool_text or ""):
        return bubbles
    return []


def _wa_verdict(bubbles, tool_text, target=None):
    """Did THIS row's WhatsApp send fail? True / False / None (no opinion).

    One Bash call can hold several sends and the hook gives every row it
    produced the SAME merged output, so one blob carries several verdicts.
    Reading the blob whole handed every row the first verdict it found: a
    refusal for Sam deleted the message that reached Alex, and Alex's
    `Sent.` line vouched for a bubble the bridge never put on the wire.
    """
    key = norm(target) if target else None
    # A refusal that NAMES a recipient answers for that row and no other.
    if key:
        for m in WA_FAIL_NAMED_RE.finditer(tool_text):
            who = m.group(1) if m.group(1) is not None else m.group(2)
            if norm(who) == key:
                return True
    mine = _wa_mine(bubbles, tool_text, target)
    if bubbles and not mine:
        return None                     # several sends, none identifiable
    if any(b[3] for b in mine):
        return False                    # a bubble of ours reached the wire
    if mine:
        # echoed but never confirmed. The reason sits after the echo; a DRY RUN
        # prints no reason at all and is a draft, which is kept.
        return bool(WA_FAIL_RE.search("\n".join(b[4] for b in mine)))
    return True if WA_FAIL_RE.search(tool_text) else None


def _wa_delivered(body, direction, room=None):
    """Bubbles WhatsApp actually put on the wire for ONE room, in order.

    Each echo counts only if its own `Sent.` line follows it before the next
    echo. A half-failed send prints 3 echoes and 2 confirmations, and taking
    all 3 would archive a bubble that never arrived. A dry run prints echoes
    and no confirmations at all, which is exactly the draft text.
    """
    bubbles, tool_text = _wa_regions(body)
    if not bubbles:
        return []
    if room:
        # the SAME pin the keep-or-drop verdict uses, by room id or by the name
        # the bridge echoed. Room-id only lost the text of `send "Alex" "$MSG"`
        # entirely: the target is the name, the echo carries both.
        bubbles = _wa_mine(bubbles, tool_text, room, lone_ok=False)
    elif len(set(b[0] for b in bubbles)) > 1:
        # two rooms in one command and nothing says which one this row is:
        # concatenating them puts one contact's words in another's archive
        return []
    if direction == "draft":
        return [b[2] for b in bubbles]
    return [b[2] for b in bubbles if b[3]]


# Send-report statuses. `sent_and_verified` is the only one meaning every
# bubble landed. `partial` means we know exactly which did and that the rest
# never will. `failed` means none did. `uncertain` means the engine cannot
# tell — the row is kept (a send was attempted) but no text is claimed for it.
# Source: imessage.py:61 and :2073-2097.
IM_OK = "sent_and_verified"
IM_UNCERTAIN = "uncertain"
IM_NOTHING_LANDED = ("failed", "cancelled", "invalid", "refused")


def report_verdict(d):
    """One send-report dict -> True (nothing landed) / False / None.

    None means "this is not a send report", which is the answer that matters:
    a history read is a list of MESSAGES, and treating one as a report would
    delete a real conversation.
    """
    if not isinstance(d, dict):
        return None
    status = d.get("status")
    if isinstance(status, str):
        return status in IM_NOTHING_LANDED
    if d.get("error"):
        return True
    return None


def json_objects(text, cap=8, scan=MAX_SCAN):
    """Every top-level JSON object in a blob, brace-matched.

    stdout and stderr arrive merged, so a partial send yields TWO documents.
    A greedy `{.*}` spans both and parses as neither, which silently turned a
    partial send into "no status known".
    """
    text = (text or "")[:scan]
    out, i, n = [], 0, len(text)
    while i < n and len(out) < cap:
        if text[i] != "{":
            i += 1
            continue
        depth, j, q, esc = 0, i, None, False
        while j < n:
            c = text[j]
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif q:
                if c == q:
                    q = None
            elif c == '"':
                q = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        try:
            d = json.loads(text[i:j + 1])
            if isinstance(d, dict):
                out.append(d)
        except Exception:
            pass
        i = j + 1 if j > i else i + 1
    return out


def report_chat(report):
    """The chat a send report belongs to, as a string."""
    v = report.get("chat_id")
    if v is None and isinstance(report.get("target"), dict):
        v = report["target"].get("chat_id")
    return None if v is None else str(v)


def im_reports(body):
    """Every iMessage send report in a blob, best-ranked first, plus whether
    any of them carried an error."""
    cands, saw_error = [], False
    for d in json_objects(body):
        if d.get("error"):
            saw_error = True
        inner = d.get("send_report")
        if isinstance(inner, dict):
            cands.append((3, inner))          # the engine's own failure report
        if "parts" in d and "sent" in d:
            cands.append((2, d))              # a full success report
        elif "status" in d:
            cands.append((1, d))              # a bare progress line
    # Rank, do not take the first: a partial send prints a `sending` progress
    # line BEFORE the real report, and first-match picked the progress line, so
    # the failure went unnoticed.
    cands.sort(key=lambda x: -x[0])
    return [c for _r, c in cands], saw_error


def im_report(body, want=None):
    """(send report, saw_error) for ONE row.

    A single Bash call can carry several sends and the hook sees one merged
    output for all of them, so an unscoped `re.search` handed every row the
    FIRST send's words. Match on the chat id the report names; when several
    sends are present and none is this row's, return nothing rather than guess
    — filing one person's message under another is worse than filing none.
    """
    reports, saw_error = im_reports(body)
    if not reports:
        return {}, saw_error
    if want is not None:
        want = str(want)
        for r in reports:
            if report_chat(r) == want:
                return r, saw_error
        chats = set(c for c in (report_chat(r) for r in reports) if c)
        if len(chats) > 1 or (chats and want not in chats):
            return {}, saw_error
    return reports[0], saw_error


def im_delivered(report):
    """(texts, files) for the bubbles the engine says it actually delivered.

    `parts` is one label per bubble in send order; `sent` holds one entry per
    DELIVERED bubble with its 1-based `bubble` index and `kind`. Reading
    `parts` alone archived every bubble of a partial send, including the ones
    that never went out.
    """
    parts = report.get("parts")
    sent = report.get("sent")
    if not isinstance(parts, list) or not isinstance(sent, list):
        return [], []
    texts, files = [], []
    for entry in sent:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("bubble")
        if not isinstance(idx, bool) and isinstance(idx, int) \
                and 1 <= idx <= len(parts):
            label = parts[idx - 1]
        else:
            continue
        if not isinstance(label, str) or not label.strip():
            continue
        (files if entry.get("kind") == "file" else texts).append(label)
    return texts, files


# Flags that differ between a dry run and the real send, and nothing else.
# Stripping them makes the two commands compare equal.
RUN_FLAGS = {"--yes", "-y", "--dry-run", "--no-dry-run", "--confirm"}


def draft_key(channel, target, command):
    """Identity of a message as COMPOSED, independent of whether it was a dry
    run or the real send."""
    toks = [t for t in argv_of(command or "") if t not in RUN_FLAGS]
    digest = hashlib.sha1(" ".join(toks).encode("utf-8", "replace"))
    return "%s|%s|%s" % (channel, norm(target) or "", digest.hexdigest()[:16])


_DRAFT_SNAPSHOT = None


def drafts_snapshot():
    """Read the draft cache ONCE per process.

    Re-reading and re-parsing it per row cost seconds across a batched command
    and blew the hook's budget outright at 2,000 sends.
    """
    global _DRAFT_SNAPSHOT
    if _DRAFT_SNAPSHOT is None:
        _DRAFT_SNAPSHOT = _load(DRAFTS, {}) or {}
    return _DRAFT_SNAPSHOT


def remember_drafts(pairs, now=None):
    """Store many dry-run texts under ONE lock, at flush time."""
    pairs = [(k, t) for (k, t) in (pairs or []) if k and t]
    if not pairs:
        return
    try:
        with filelock(DRAFTS):
            cur = load_strict(DRAFTS, {})
            if cur is None:
                return              # corrupt: never clobber
            stamp = now or time.time()
            for key, text in pairs:
                cur[key] = {"text": text[:BODY_CAP], "ts": stamp}
            if len(cur) > 200:
                keep = sorted(cur.items(), key=lambda kv: kv[1].get("ts", 0))
                cur = dict(keep[-200:])
            _save(DRAFTS, cur)
            global _DRAFT_SNAPSHOT
            _DRAFT_SNAPSHOT = cur
    except Exception:
        pass


def remember_draft(key, text, now=None):
    """Single-entry form, for callers outside the hook."""
    remember_drafts([(key, text)], now=now)


def recall_draft(key, now=None):
    """The text of a recent dry run of this exact command, or None."""
    if not key:
        return None
    got = drafts_snapshot().get(key)
    if not isinstance(got, dict):
        return None
    if (now or time.time()) - float(got.get("ts") or 0) > DRAFT_TTL:
        return None
    text = got.get("text")
    return text if isinstance(text, str) and text.strip() else None


def slack_echo(body, target=None):
    """The message text out of a Slack DRY RUN echo.

    A real send returns `{"ok":true,"ts":...,"channel":...}` with no text at
    all; only the dry run prints `{"channel":..., "text":...}`.
    """
    hits = []
    for d in json_objects(body):
        text, chan = d.get("text"), d.get("channel")
        if isinstance(text, str) and text.strip() and chan:
            hits.append((str(chan), text))
    if not hits:
        return None
    if target:
        key = norm(target)
        hits = [h for h in hits if norm(h[0]) == key]
    elif len(set(h[0] for h in hits)) > 1:
        return None            # several channels, nothing says which is ours
    return hits[0][1] if hits else None


def recover_sent(body, channel, verb=None, direction=None, target=None):
    """What the tool itself recorded sending -> dict(text, source, files).

    The command line is the primary source; this is the fallback for when it
    held a shell variable or a file that no longer exists.
    """
    out = {"text": None, "source": None, "files": None}
    body = body or ""
    if channel == "slack":
        got = slack_echo(body, target)
        if got:
            out["text"] = got
            out["source"] = "cli-output"
        return out
    if channel not in ("imessage", "whatsapp"):
        return out
    if channel == "imessage":
        report, _err = im_report(body, target)
        status = report.get("status")
        status = status if isinstance(status, str) else None
        # Per-bubble truth first: it is the only source that separates a
        # partial send from a complete one.
        texts, files = im_delivered(report)
        if texts or files:
            if texts:
                out["text"] = "\n\n".join(texts)
                out["source"] = "cli-output"
            out["files"] = files or None
            return out
        # Older builds print no `parts`. Fall back to the engine's stored
        # record, which holds the INTENDED text — so only when the engine says
        # every bubble landed.
        if status is not None and status != IM_OK:
            return out
        # the id must come from THIS row's report; scanning the whole body
        # again would hand every row of a batch the first send's preview
        pid = report.get("preview_id")
        if not isinstance(pid, str):
            reports, _e = im_reports(body)
            if len(reports) > 1:
                return out          # several sends, none identifiable as ours
            pid = preview_id_of(body)
        pl = preview_payload(pid)
        if pl:
            texts = payload_texts(pl)
            files = [p.get("name") for p in (pl.get("parts") or [])
                     if isinstance(p, dict) and p.get("kind") == "file"
                     and p.get("name")]
            if texts:
                out["text"] = "\n\n".join(texts)
                out["source"] = "preview-store"
            if files:
                out["files"] = files
            if out["text"] or out["files"]:
                return out
        return out
    if channel == "whatsapp":
        got = _wa_delivered(body, direction, target)
        if got:
            out["text"] = "\n\n".join(got)
            out["source"] = "cli-output"
        return out
    return out


# The WHOLE value is a substitution or a bare variable. Deliberately anchored:
# a message that merely contains a `$` ("that'll be $50", "50% off `prod`") is
# ordinary text and must survive.
UNEXPANDED_RE = re.compile(
    r"^\s*(?:\$\(.*\)|`[^`]*`|\$\{[A-Za-z_][A-Za-z0-9_]*\}"
    r"|\$[A-Za-z_][A-Za-z0-9_]*)\s*$", re.S)
# `$(cat /tmp/draft.txt)` names a real readable path; `$(cat "$D/x.txt")` does
# not. Only the first can be recovered. `$(gmail-html-body /tmp/reply.txt)` is
# the HTML Gmail send (gmail_html.py); its file holds the plain words, so the
# archive reads that file too.
CAT_SUBST_RE = re.compile(
    r"^\s*\$\(\s*(?:cat|gmail-html-body)\s+[\"']?([^\"'$`)\s]+)[\"']?\s*\)\s*$")


def unexpanded_text(value):
    """True if the captured 'message' is really the shell source of one."""
    return bool(value) and bool(UNEXPANDED_RE.match(str(value)))


def substitution_file(value):
    """The literal path inside `$(cat PATH)` or `$(gmail-html-body PATH)`."""
    m = CAT_SUBST_RE.match(str(value or ""))
    return m.group(1) if m else None


def unresolved(target):
    """True if the extracted target is a shell variable or substitution."""
    return bool(target) and bool(re.search(r"[$`]", str(target)))


# Recovery patterns are SCOPED PER CHANNEL. A single shared list matched
# ordinary uppercase words ("DELIVERED", "COMPLETED", "CONNECTION") as Slack
# ids and signature addresses ("noreply@example.com") as contacts, archiving
# people who do not exist.
RECOVERY = {
    "whatsapp": (r"![A-Za-z0-9_\-]{8,}(?::[^\s\]]+)?",
                 r"\b\d{6,20}@(?:lid|s\.whatsapp\.net)\b",
                 r"\b[\w.\-]+@g\.us\b"),
    # Slack ids must contain a digit; that is what separates C01ABCDEF from
    # the word CONNECTION.
    # A digit ANYWHERE was not enough: "CONNECTION2", "DEPLOYMENT1" and any
    # version-suffixed uppercase word were recovered as channel ids. A real
    # Slack id carries a digit within the first three characters after the
    # C/D/G prefix, which no English word does.
    "slack": (r"\b[CDG](?=[A-Z0-9]{0,2}\d)[A-Z0-9]{7,12}\b",),
    # same multi-label domain fix as ADDR_RE: this pattern stored
    # a domain cut at its first dot as a real contact.
    "gmail": (r"(?<![\w.+\-])[\w.+\-]+@[A-Za-z0-9\-]+"
              r"(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}\b",),
    "imessage": (),      # --chat-id is always present; guessing adds garbage
    "upwork": (),        # structured MCP input, nothing to recover from
}


# the owner's own addresses. A Gmail helper invoked with only `--message-id` names
# no person on the command line, so the row filed under the message ID and
# never joined that contact's thread -- 11 of 29 gmail rows on 2026-09-09.
# The message's own headers carry both sides; telling them apart needs to know
# which one is the owner's. The addresses come from the kit config key `self_emails` (a list). More
# can be added without editing the config by writing `<data dir>/self.json`:
# {"emails": ["other@address"]}.
# a Gmail message/thread id as `gws` prints it
GMAIL_ID_RE = re.compile(r"^[0-9a-f]{16}$")
# Real `gws` output is not one tidy shape. Measured against the archive on
# 2026-09-09 it prints `FROM:`, `From :`, `  Date:` indented, RFC-folded
# continuations, and several header blocks per message because the quoted
# chain keeps its own. The old pattern -- case-sensitive, hard-anchored, no
# space before the colon -- matched none of the first three.
HEADER_RE = re.compile(r"^[ \t]*(?:To|From|Cc)[ \t]*:[ \t]*(.{0,400})$",
                       re.M | re.I)
# ANY header-shaped line. Used only to prove a To:/From:/Cc: line has a
# NEIGHBOUR: 43 of 43 real header lines in the archive sat next to another
# header, but a message body quoting `From: attacker@example.net` in prose does
# not -- and that line alone used to re-key the row to whoever it named.
# A WHITELIST, not `[A-Za-z-]+:`. The loose form called `Note:`, `Sent:`,
# `Warning:`, `TODO:` and even `https://...` header-shaped, so one URL beside a
# forged `From:` in a message body satisfied the adjacency rule it exists to
# enforce.
ANY_HEADER_RE = re.compile(
    r"^[ \t]*(?:from|to|cc|bcc|reply-to|sender|return-path|delivered-to"
    # `subj` earns its place: a session that pretty-prints a Gmail read as
    # `TO: ... | CC: None` / `SUBJ: ...` produced a REAL two-line header block
    # whose second line was not in this vocabulary, so the prose guard below
    # ("a lone header-shaped line is prose") threw the To: line away and the
    # row stayed filed under its message id. Measured 2026-09-10: adding it
    # changed the parse of exactly 1 row in 289 -- that one.
    r"|date|subject|subj|message-id|message_id|references|in-reply-to"
    r"|in_reply_to|received|mime-version|content-type"
    r"|content-transfer-encoding|list-unsubscribe|x-[a-z0-9\-]{1,40})"
    r"[ \t]*:", re.I)
# A domain is any number of labels. `[\w\-]+\.[a-z]{2,}` stopped at the FIRST
# dot, so a `.co.za` address was stored as if it ended in `.co` --
# an address that does not exist. It also merged `.co.uk`, `.co.za` and
# `.co.jp` for one local part into a single thread, and dropped
# an address with a long chain of subdomains entirely.
# The leading lookbehind is not cosmetic, it is the whole cost model. Without
# it the engine may START a match at EVERY character of a long local-part run,
# and each attempt walks the run to its end looking for `@`. One `--format raw`
# base64 body measured 4.9s at 40 KB and 115s at 200 KB -- past the hook's 10
# second kill, which loses every row of that tool call. The lookbehind allows a
# start only where the previous character cannot be part of an address, so a
# 600 KB run has ONE start instead of 600,000.
ADDR_RE = re.compile(
    r"(?<![\w.+\-])[\w.+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*"
    r"\.[A-Za-z]{2,}")
# `Message-ID: <CALAmjs...@mail.example.com>` is address-SHAPED and is not an
# address. It was harmless only while the domain pattern truncated it to
# `@mail.gmail`; fixing that made it a plausible lone candidate.
NON_ADDR_HEADER_RE = re.compile(
    r"^[ \t]*(?:Message[\-_]?Id|References|In[\-_]?Reply[\-_]?To)[ \t]*:",
    re.M | re.I)
# `gws gmail users messages get --format metadata` and the `+read` pretty view
# print no colon at all -- a padded COLUMN table:
#     From       Sam Roe <sam@example.org>
#     To         Alex Doe <alex@example.com>
# Measured on the live archive 2026-09-10: this shape parsed to ZERO header
# values, so a whole client thread stayed keyed by its message id.
# TWO or more spaces is the column gutter. One space is prose ("From now on
# ..."), and the gutter is the only thing keeping this off English sentences.
COL_HEADER_RE = re.compile(r"^[ \t]*(?:To|From|Cc)[ \t]{2,}(\S.{0,400})$",
                           re.M | re.I)
# the neighbour vocabulary for the column shape -- same job as ANY_HEADER_RE,
# and a WHITELIST for the same reason
ANY_COL_HEADER_RE = re.compile(
    r"^[ \t]*(?:from|to|cc|bcc|reply-to|sender|return-path|delivered-to"
    r"|date|subject|subj|snippet|labels|label-ids|message-id|references"
    r"|in-reply-to)[ \t]{2,}\S", re.I)
NON_ADDR_COL_HEADER_RE = re.compile(
    r"^[ \t]*(?:Message[\-_]?Id|References|In[\-_]?Reply[\-_]?To)"
    r"[ \t]{2,}\S", re.M | re.I)


def email_key(value):
    """Compare-form of an address: lowercased, `<>` stripped, `+tag` removed.

    `owner+client@example.com` IS the owner. Compared raw it read as a
    correspondent, so a self-note filed under him as if he were someone else,
    and a client mail sent to a tagged alias produced two "others" and gave up.
    Only the COMPARISON is tagless; the address stored as the target keeps its
    tag, because that is what the account actually holds.
    """
    e = str(value or "").strip().strip("'\"").strip("<>").lower()
    if "@" not in e:
        return e
    local, _, domain = e.partition("@")
    return local.split("+", 1)[0] + "@" + domain


_SELF_CACHE = [None, None]        # (mtime, set) -- see self_emails()


def self_emails():
    """Every address that is OURS, so the other one is the correspondent.

    Cached on `self.json`'s mtime. This is called once per row and one chained
    Bash line can produce hundreds of rows, all inside a 10 second budget.
    """
    path = os.path.join(ROOT, "self.json")
    configured = [e for e in K.get_list("self_emails") if isinstance(e, str)]
    try:
        st = os.stat(path)
        stamp = (st.st_mtime, st.st_size, tuple(configured))
    except Exception:
        stamp = (0, 0, tuple(configured))
    # An added Gmail account's address is ours too (gmail_accounts.py). The accounts folder's own
    # mtime joins the stamp, so connecting or removing one is seen on the next call.
    accounts = os.path.join(HOME, ".config", "gws-accounts")
    try:
        stamp = stamp + (os.stat(accounts).st_mtime,)
    except Exception:
        pass
    # size as well as mtime: `rsync -t`, `touch -r` and a Time Machine restore
    # all put back an OLD mtime on NEW contents
    if _SELF_CACHE[0] == stamp and _SELF_CACHE[1] is not None:
        return _SELF_CACHE[1]
    got = set(email_key(e) for e in configured)
    try:
        extra = load_strict(path, {}) or {}
        emails = extra.get("emails") if isinstance(extra, dict) else None
        # a LIST of STRINGS or nothing. `{"emails": "one@example.com"}` iterated the
        # string and added sixteen single characters to the self set while the
        # alias itself was never added -- the documented escape hatch doing the
        # exact opposite of what it says, silently.
        if isinstance(emails, (list, tuple)):
            for e in emails:
                if isinstance(e, str) and "@" in e and email_key(e):
                    got.add(email_key(e))
    except Exception:
        pass
    try:
        for n in os.listdir(accounts):
            if "@" in n and os.path.isdir(os.path.join(accounts, n)) and email_key(n):
                got.add(email_key(n))
    except Exception:
        pass
    _SELF_CACHE[0], _SELF_CACHE[1] = stamp, got
    return got


def header_values(body, cap=2000):
    """To:/From:/Cc: values from real header BLOCKS, folded lines joined.

    Two rules make this trustworthy on output nobody controls:
    a line is only taken when another header-shaped line sits NEXT to it, and
    `Message-ID:`/`References:`/`In-Reply-To:` are skipped outright.
    """
    lines = (body or "").split("\n")
    hdr = [bool(ANY_HEADER_RE.match(ln)) or bool(ANY_COL_HEADER_RE.match(ln))
           for ln in lines]
    out = []
    for i, ln in enumerate(lines):
        # colon form first: the two are disjoint, `From:` has no gutter and
        # `From   x` has no colon, so neither can steal the other's line
        m = HEADER_RE.match(ln) or COL_HEADER_RE.match(ln)
        if (not m or NON_ADDR_HEADER_RE.match(ln)
                or NON_ADDR_COL_HEADER_RE.match(ln)):
            continue
        if not ((i and hdr[i - 1]) or (i + 1 < len(lines) and hdr[i + 1])):
            continue          # a lone header-shaped line is prose
        value = m.group(1)
        # RFC 5322 folding: a continuation is indented AND is not itself a
        # header, which is what separates a genuine `To: a,\n\tb@example.com` from
        # a block that is merely indented as a whole -- there the next line IS
        # a header, so nothing is joined.
        j = i + 1
        while (j < len(lines) and lines[j][:1] in (" ", "\t")
               and lines[j].strip() and not hdr[j]):
            value += " " + lines[j].strip()
            j += 1
        out.append(value)
        if len(out) >= cap:
            break
    return out


def leading_header_values(body):
    """Header values from the block the body OPENS with, and nothing below it.

    The capture writes one message's own headers at the top of the body and a
    blank line under them; everything after that blank line is the message
    text, which on a reply carries the quoted history -- and that history is
    full of header-shaped lines naming people who are not in THIS message.
    Measured 2026-09-12: a one-line reply from a client carried an Outlook quote
    block naming a colleague, so `gmail_party` saw two others and refused, and the
    row stayed filed under its 16-hex message id.

    Returns [] when the body does not open with a header block, which puts the
    caller back on the whole-body scan with nothing changed.
    """
    lines = (body or "").split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    end = start
    while end < len(lines) and lines[end].strip():
        end += 1
    if end - start < 2:
        return []          # the neighbour rule: one line alone is prose
    return header_values("\n".join(lines[start:end]))


def _headers_overflowed(values, cap=2000):
    """The cap was reached, so anything after it was never looked at."""
    return len(values) >= cap


def gmail_party(body, limit=2):
    """The one address in this output that is NOT ours, or None.

    Stops at `limit` distinct others. Once there are two the answer is fixed
    at None, and scanning on was quadratic: 3,000 addresses in one body cost
    11.9s against the hook's 10 second budget, and a killed hook loses every
    row of that call, not only this one.
    """
    mine = self_emails()
    # The opening block is THIS message's own headers. Trust it before the
    # whole-body scan, which cannot tell a quoted reply's headers from a
    # genuine second correspondent.
    lead = leading_header_values(body)
    if lead:
        picked, got = [], set()
        for value in lead:
            for e in ADDR_RE.findall(value):
                k = email_key(e)
                if not k or k in mine or k in got:
                    continue
                got.add(k)
                picked.append(e)
                if len(picked) > 1:
                    break
            if len(picked) > 1:
                break
        if len(picked) == 1:
            return picked[0]
    values = header_values(body)
    others, seen = [], set()
    for value in values:
        for e in ADDR_RE.findall(value):
            k = email_key(e)
            if not k or k in mine or k in seen:
                continue
            seen.add(k)
            others.append(e)
            if len(others) >= limit:
                return None
    if _headers_overflowed(values):
        # a digest or a 100-message thread dump: a third party could be sitting
        # past the cap, and "the first one we saw" is exactly the guess this
        # function refuses to make everywhere else
        return None
    return others[0] if len(others) == 1 else None


def gmail_self_note(body):
    """The one SELF address in this output when nobody else is named.

    A genuine note to self has a real header block whose every address is
    ours. Telling that apart from "no header block at all" is what lets a
    self-note keep his address without reintroducing a loose whole-body scan:
    a bare `Delivered-To:` line names him too, and is not a header block.
    """
    mine = self_emails()
    values = header_values(body)
    if not values or _headers_overflowed(values):
        return None
    found = None
    for value in values:
        for e in ADDR_RE.findall(value):
            k = email_key(e)
            if not k:
                continue
            if k not in mine:
                return None
            found = found or e
    return found


def gmail_correspondent(body, target, sole=True):
    """The PERSON a Gmail row is with, when the command named only an id.

    Returns None -- keep the id -- for anything ambiguous: a target that is
    already an address, a body with no header block, or a thread with two
    other participants. A message id is a poor key but a TRUE one; filing a
    row under the wrong person is not recoverable.

    `sole` is False when the Bash line called `gws gmail` more than once. Every
    one of those rows gets the SAME merged output blob, so a header in it
    cannot be attributed to any one of them -- unscoped,
    `+reply --message-id A ; +reply --message-id B` filed B's message in A's
    correspondent's thread. See [[batched-rows-share-one-payload]].
    """
    if not sole:
        return None
    if not GMAIL_ID_RE.match(str(target or "").strip().lower()):
        return None
    return gmail_party(body)


def recover_target(body, channel=None, sole=True):
    """Pull a real identifier out of command output, for the one case where
    the command line could not supply it (a shell variable, or a Gmail helper
    with no --to).

    `sole=False` says this Bash line called the channel's CLI more than once,
    so every row shares one merged blob and nothing in it can be attributed.
    `gws gmail +reply --message-id A ; ... --message-id B` reaches here with
    NO target on either row -- `--message-id` names no person -- so the gate
    has to be here as well as on `gmail_correspondent`, or both rows are filed
    under whichever correspondent the blob happens to name.
    """
    pats = RECOVERY.get(channel) if channel else None
    if pats is None:
        return None
    scan = body or ""
    if channel == "gmail":
        # STRICT, with no fallback sweep. The generic scan below enforces none
        # of the rules that make this safe -- no header block, no adjacency, no
        # self filter, no `Message-ID` exclusion -- so whenever `gmail_party`
        # declined, that sweep answered instead and every rule was moot:
        # `From: attacker@example.net` in a message BODY became the contact key,
        # a `Delivered-To:` line re-filed the row under the owner himself, and a
        # folded `References:` continuation invented an address outright.
        # Declining here keeps the message id, which is a poor key but a TRUE
        # one.
        return gmail_party(scan) if sole else None
    for pat in pats:
        found = []
        for m in re.finditer(pat, scan):
            k = norm(m.group(0))
            if k and k not in [norm(x) for x in found]:
                found.append(m.group(0))
            if len(found) > 1:
                break
        # Two different ids in one output means two different recipients, and
        # the command line no longer says which one this row is. Returning the
        # first filed one person's message under another; the row is dropped
        # instead.
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return None
    return None


# ------------------------------------------------------------- redaction

# The archive stores raw command output (20 KB) and the command line (2 KB)
# from CLIs that hold Slack/WhatsApp/Gmail/Upwork credentials. A CLI printing
# an auth error, or an inline `--api-key`, would otherwise sit on disk forever.
# 0600 stops other USERS, not other processes running as this user — and every
# agent on this Mac runs as this user. Redaction is lossy and deliberate: an
# archive is the wrong place to keep a live credential.
REDACTIONS = (
    # The value must look like a TOKEN, not like English. A length-only test
    # destroyed `Authorization: awaiting-client-signoff-until-friday`, so
    # require a digit somewhere in it — real bearer tokens always have one.
    (re.compile(r"(?i)\b((?:bearer|authorization)[:\s]+)"
                r"(?=[A-Za-z0-9\-_.]*\d)[A-Za-z0-9\-_.]{20,}"),
     r"\1[REDACTED]"),
    # sk- keys carry internal hyphens (`sk-proj-...`, `sk-ant-...`), so the
    # character class must allow them or the key survives redaction
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{10,}"
                r"|gh[pousr]_[A-Za-z0-9]{30,}|AIza[0-9A-Za-z_-]{35}"
                r"|glpat-[A-Za-z0-9_-]{16,}"
                r"|A(?:KIA|SIA|ROA|IDA|GPA|IPA|NPA|CCA)[0-9A-Z]{16})\b"),
     "[REDACTED-KEY]"),
    # a JWT is three base64url segments; `eyJ` is base64 for `{"`
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
                r"\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED-JWT]"),
    (re.compile(r"(?i)(--?(?:api[-_]?key|token|password|passwd|secret|auth)"
                r"[=\s])\S+"), r"\1[REDACTED]"),
    (re.compile(r"\b([A-Z][A-Z0-9_]*_(?:API_KEY|SECRET|TOKEN|PASSWORD)\s*[=:]"
                r"\s*)\S+"), r"\1[REDACTED]"),
    # The same names with NO leading dash. A pasted config block or CLI error
    # containing `password: hunter2goeshere` was archived in the clear.
    (re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token"
                r"|client[_-]?secret|private[_-]?key)(\s*[:=]\s*)(\S{6,})"),
     r"\1\2[REDACTED]"),
    # `?token=` is as common as `/reset/` — match any delimiter. `confirm` is
    # deliberately NOT here: it destroyed the owner's own Calendly booking links,
    # which are the entire point of the message. A confirmation page is not a
    # credential; a reset or magic link is.
    (re.compile(r"https?://\S*(?:reset|verify|token|magic)[=/?&]\S+"),
     "[REDACTED-LINK]"),
)


def was_redacted(value):
    """True if `redact()` rewrote this value.

    Every replacement above is shorter than what it replaced, so a stored
    string's length only proves the original's length when nothing was
    scrubbed out of it. `comms reindex` needs that distinction: it decides
    whether a stored command was CUT by comparing its length to the cap.
    """
    return "[REDACTED" in (value or "")


def redact(value):
    """Strip credential-shaped substrings before anything reaches disk."""
    if not value:
        return value
    out = str(value)
    for pat, repl in REDACTIONS:
        out = pat.sub(repl, out)
    return out


# Errors that are printed as prose rather than JSON. `'Sam' is ambiguous —
# refusing to guess` was archived as a real conversation under the key `alex`.
# Anchored at the start so a message that merely mentions an error is safe.
PLAINTEXT_ERROR_RE = re.compile(
    r"\s*(?:error\b|usage:|traceback\b|fatal:|refusing\b|no such\b"
    r"|[^\n]{0,90}\b(?:is ambiguous|refusing to guess|not found|"
    r"no matching chat)\b)", re.I)


# The `\s*` must live INSIDE the lookahead. Outside it, the regex engine
# backtracks it to zero width and the `(?!null)` guard then inspects the space
# instead of the word — so `{"error": null` (a SUCCESS) was read as a failure.
UNCLOSED_ERROR_RE = re.compile(r'^[ \t]*\{\s*"error"\s*:(?!\s*null\b)', re.M)
# Evidence in PLAIN TEXT that a send really went out. DELIVERY evidence only:
# the `About to send to ...` echo and the `DRY RUN` banner were here once and
# had to come out, because they prove a message BODY is present, not that
# anything left the machine — a real failure printed under the echo was
# archived as a delivery.
# iMessage only. `Sent. event_id=` used to be here and had to come out: it is
# the WhatsApp bridge's marker, and in a chained `whatsapp send && imessage
# send` it vouched for the iMessage refusal printed underneath it. WhatsApp is
# judged by `_wa_verdict` now, scoped to its own bubbles. `RESULT: SENT` also
# came out -- no tool on this machine prints it (grepped ~/.claude/skills,
# ~/.local/bin and ~/.pi-2), so it was pure false-positive surface.
PLAINTEXT_SENT_RE = re.compile(r"sent_and_verified")
# Kept as an alias: this was always the WhatsApp bridge's failure list, and it
# now lives beside the bridge's other markers as WA_FAIL_RE.
PLAINTEXT_SEND_FAIL_RE = WA_FAIL_RE
# an error document written by a CLI starts its own line; one pasted into a
# chat sits mid-sentence behind a timestamp and a speaker name
LINE_START_OBJ_RE = re.compile(r"^[ \t]*\{", re.M)
# The Slack bridge reports its own failures the same way the WhatsApp bridge
# does -- prose, not JSON. `slack_bridge.py` raises SystemExit at :63, :90,
# :92, :116 and :118, and the JSON detail inside `Slack bridge HTTP 401:
# {"detail": ...}` sits MID-LINE, so the line-start object scan below never saw
# it and a refused send was archived as a delivered message.
SLACK_FAIL_RE = re.compile(
    r"^Slack bridge (?:HTTP \d+|unreachable\b)"
    r"|^BRIDGE_API_KEY missing\b"
    r"|^NOT SENT: ", re.M)          # `send --since` refused: nothing went out


def _slack_failed(body, target=None):
    """Did THIS row's Slack send fail? True / False / None (no opinion).

    A blob can hold a refusal AND a delivery: one Bash call, two channels or
    two sends. Any `"ok": true` document naming this row's channel wins, for
    the same reason it wins everywhere else -- dropping a row is irreversible.
    """
    if not SLACK_FAIL_RE.search(body):
        return None
    key = norm(target) if target else None
    for d in json_objects(body, cap=40):
        if not isinstance(d, dict) or d.get("ok") is not True:
            continue
        if not key or norm(str(d.get("channel") or "")) == key:
            return False
    return True


def failed(body, empty_counts=True, channel=None, target=None):
    """True if the CLI reported an error instead of returning a conversation.

    A failed read carries no messages, but it does carry the id that was
    probed — so logging it invents a contact ("chat 0", "chat 999") that never
    existed. Deliberately narrow: only a top-level JSON `error` object counts,
    so a real message whose text mentions an error is untouched.

    `channel` and `target` scope the answer to ONE row. They are optional only
    so an old caller keeps working; every real caller passes them, because one
    Bash call can chain sends on two different tools and the hook hands every
    row it produced the same merged output.
    """
    b = (body or "").strip()
    if not b:
        return empty_counts
    # The WhatsApp bridge reports in sentences, not JSON, so its verdict is
    # settled first and separately. Three things went wrong when this blob was
    # judged as a whole (all measured 2026-09-09):
    #   - a WhatsApp echo vouched for an iMessage refusal chained after it;
    #   - one unresolvable recipient deleted the bubble that really landed;
    #   - a message quoting a CLI error line deleted itself.
    if not empty_counts:
        bubbles, tool_text = _wa_regions(b)
        if channel in (None, "whatsapp") and (bubbles
                                              or WA_FAIL_RE.search(tool_text)):
            verdict = _wa_verdict(bubbles, tool_text, target)
            if verdict is not None:
                return verdict
        # From here on, read only what the TOOLS said. `tool_text` is the body
        # with every echoed message cut out, so a client's own words can never
        # be parsed as a CLI's report.
        b = tool_text.strip() or b
    # A partial/failed send merges two json documents into one body, so the
    # single-document parse below saw malformed input and called it a success.
    if not empty_counts and b.startswith("["):
        # A top-level JSON ARRAY of send reports. The line-start scan further
        # down only considers lines that BEGIN with `{`, so a batch send whose
        # every recipient failed began with `[`, matched nothing, and was
        # archived as delivered. Only unanimous failure drops the row, and
        # only when every element really is a report -- a `read` never reaches
        # here, because a history list is exactly this shape.
        try:
            arr = json.loads(b)
        except Exception:
            arr = None
        if isinstance(arr, list) and arr:
            verdicts = [report_verdict(d) for d in arr]
            if None not in verdicts:
                return all(verdicts)
    if b.startswith("{") and b.count("{") > 1:
        reports, saw_error = im_reports(b)
        # This runs before the row's target is known, so it cannot be scoped to
        # one send. In a batch, drop only when EVERY report says nothing
        # landed — otherwise one failed send in the line would discard the
        # delivered ones alongside it.
        states = [r.get("status") for r in reports
                  if isinstance(r.get("status"), str)]
        if states:
            for r in reports:
                t, f = im_delivered(r)
                if t or f:
                    return False    # something really landed: keep the row
            # `uncertain` is deliberately NOT a failure: the engine cannot tell
            # whether it went out, so the row is kept as "a send happened" with
            # no text claimed for it.
            return all(st in IM_NOTHING_LANDED for st in states)
        if saw_error:
            return True
    if b.startswith("{"):
        try:
            d = json.loads(b)
        except Exception:
            d = None
        # `"error": null` means there was NO error. Testing `"error" in d`
        # called every successful send a failure and dropped it. The old
        # `len(d) <= 2` size test also made the check intermittent: the same
        # real failure was caught or missed depending on how many keys the CLI
        # happened to return.
        if isinstance(d, dict):
            # the engine also reports a refused send as a plain report with no
            # `error` key at all, so an error-only test called it a delivery
            return bool(d.get("error")) or d.get("status") in IM_NOTHING_LANDED
        if d is not None:
            return False
        # UNPARSEABLE. This used to return a bare `^{"error"` prefix test, so a
        # blob whose FIRST document was a Slack failure and whose second was
        # anything else read as a success -- and cutting an echoed message out
        # could drop the brace count from 2 to 1 and route a multi-document
        # blob in here. Fall through to the line-start scan, which brace-
        # matches each document instead of guessing from the first byte.
        if empty_counts or not LINE_START_OBJ_RE.search(b):
            return bool(re.match(r'\{\s*"error"\s*:\s*(?!null\b)', b))

    # Every branch above needs the body to START with `{`. A warning line, a
    # wrapper banner or a chained `echo` in front of the JSON hid the whole
    # check, and three refused sends were archived as deliveries. Measured
    # 2026-09-09 on the live store.
    #
    # Deliberately narrow, because a false positive here DELETES a real client
    # message and a false negative only keeps a dud:
    #   - sends only. A read prints conversation text, and a client who pastes
    #     `{"error": ...}` into a chat would otherwise erase their own message.
    #   - the object must begin a line. A CLI's error document does; one quoted
    #     inside a transcript line does not.
    #   - any evidence the message went out wins, in JSON or in plain text.
    # (the WhatsApp bridge's plain-text markers were read at the top of this
    # function, scoped to this row's own bubbles — see `_wa_verdict`)
    if not empty_counts and channel in (None, "slack"):
        verdict = _slack_failed(b, target)
        if verdict is not None:
            return verdict
    if (not empty_counts and "{" in b and LINE_START_OBJ_RE.search(b)):
        if PLAINTEXT_SENT_RE.search(b):
            return False
        # cap 40, not 8: a real refusal sitting past the 8th document in a
        # batched blob was invisible and the row was kept as delivered
        docs = [d for d in json_objects(b, cap=40) if isinstance(d, dict)]
        flat = []
        for d in docs:
            flat.append(d)
            inner = d.get("send_report")
            if isinstance(inner, dict):
                flat.append(inner)
        # anything that says a message really went out wins: dropping a row is
        # irreversible, so an ambiguous blob is kept
        # `uncertain` is kept for the same reason the byte-0 branch keeps it:
        # the engine cannot tell whether the message went out, so the row
        # stands as "a send happened" with no text claimed for it. Without this
        # carve-out one banner line flipped an uncertain send from kept to
        # dropped.
        if any(d.get("status") == IM_OK or d.get("sent") or d.get("ok") is True
               or d.get("event_id") or d.get("id") or d.get("ts")
               or d.get("status") == IM_UNCERTAIN
               for d in flat):
            return False
        if any(bool(d.get("error")) or d.get("status") in IM_NOTHING_LANDED
               for d in flat):
            return True
        # nothing parsed: the body was cut at BODY_CAP mid-object, or the CLI
        # was piped through `head`. An unterminated `{"error": ...` is still a
        # refusal, and one such row is in the live store.
        if not docs and UNCLOSED_ERROR_RE.search(b):
            return True
    return bool(PLAINTEXT_ERROR_RE.match(b))


# ------------------------------------------------------------ shared row writer
# Moved here from hooks/comms-log.py (2026-10-30, P0) so the PostToolUse hook AND the CLIs write
# rows the same way. Install commslib.py BEFORE comms-log.py: the hook calls write_rows.

def _stamp_accounts(items):
    """Give a Gmail row sent or read through an ADDED account an `account` field.

    A main-account row (plain `gws`) gets NO field, so it stays exactly as it was. The command is
    the only evidence: `gws-as EMAIL gmail ...` or GOOGLE_WORKSPACE_CLI_CONFIG_DIR=... gws gmail ...
    Never raises: a failure here must not cost a row."""
    try:
        GA = None
        for row, _meta in items:
            if row.get("channel") != "gmail" or "account" in row:
                continue
            cmd = row.get("command") or ""
            if "gws-as" not in cmd and "GOOGLE_WORKSPACE_CLI_CONFIG_DIR" not in cmd:
                # `export VAR=...; gws gmail ...`: the variable sits in another segment of the
                # same line, so the account is unknown. Say so rather than call it the main one.
                if "GOOGLE_WORKSPACE_CLI_CONFIG_DIR" in (row.get("full_command") or ""):
                    row["account"] = "unresolved"
                continue
            if GA is None:
                import gmail_accounts as GA
            acct = GA.account_of_command(cmd)
            if acct:
                row["account"] = acct
    except Exception:
        pass


def write_rows(items, cwd=None):
    """All the disk work for one batch of (row, meta) pairs, in one pass.

    meta: {month, key, channel, ts, et, project, label, hit, target, draft_put}."""
    if not items:
        return
    _stamp_accounts(items)
    secure_dir(ROOT)

    # 1. the durable record. Append-only, so this is the one write that must
    #    not be skipped; everything below is a rebuildable index.
    by_month = {}
    for row, meta in items:
        by_month.setdefault(meta["month"], []).append(row)
    for month, rows in by_month.items():
        append_jsonl_many(os.path.join(STORE, month + ".jsonl"), rows)

    # 2. derived index, under ONE lock for the whole batch. Unsynchronised, 40
    #    concurrent writers destroyed 400 of 406 entries: a torn read looked
    #    like an empty index and the next writer saved its own single key over
    #    the whole file.
    threads = None
    with filelock(THREADS):
        loaded = load_strict(THREADS, {})
        if loaded is not None:      # corrupt: leave it for `comms reindex`
            threads = loaded
            for _row, meta in items:
                key = meta["key"]
                t = threads.get(key, {"target": key,
                                      "channel": meta["channel"], "count": 0})
                t["count"] = t.get("count", 0) + 1
                t["last_ts"] = meta["ts"]
                t["last_et"] = meta["et"]
                t["channel"] = meta["channel"]
                t["project"] = meta["project"]
                t["label"] = meta["label"]
                t.setdefault("first_et", meta["et"])
                threads[key] = t
            _save(THREADS, threads)
            write_index(ROOT, threads)

    # 3. per-project mirrors, one append and one index rewrite per repo
    by_project = {}
    unrouted = []
    for row, meta in items:
        hit = meta["hit"]
        if meta["project"] and hit.get("path") and os.path.isdir(hit["path"]):
            slot = by_project.setdefault(
                (hit["path"], meta["project"], meta["month"]), [])
            slot.append(row)
        elif not meta["project"]:
            unrouted.append((meta["target"], meta["channel"], cwd))
    for (path, project, month), rows in by_project.items():
        folder = os.path.join(path, DIR_NAME)
        # gitignore FIRST: writing client messages into a repo and only then
        # protecting them leaves a window where a `git add -A` sweeps them in
        root = git_root(path)
        if root:
            ensure_gitignore(root)
        append_jsonl_many(os.path.join(folder, month + ".jsonl"), rows)
        if threads is not None:
            write_index(folder, threads, project=project)

    note_unrouted_many(unrouted)
    remember_drafts([m["draft_put"] for _r, m in items if m.get("draft_put")])


# ------------------------------------------------------------ receipts (CLI + hook dedupe)
# A send logged by the CLI itself (plain shell, launchd, pi-2, Codex) must not be logged a second
# time by the PostToolUse hook when the same send ran inside Claude Code. The key is the
# transport's own receipt for ONE delivered part. No receipt -> no key -> never deduped
# (a possible duplicate row is better than a dropped message).

RECEIPTS = os.path.join(ROOT, "receipts.json")
RECEIPT_KEEP_S = 7 * 24 * 3600
WA_EVENT_RE = re.compile(r"Sent\. event_id=(\$[A-Za-z0-9_\-+/=.:]+)")


def receipt_key(channel, message_id, scope=None):
    """whatsapp:$event | slack:<channel>:<ts> | imessage:local:<rowid>. None when unknown."""
    if message_id in (None, ""):
        return None
    if channel == "slack":
        return "slack:%s:%s" % (scope or "?", message_id) if scope else None
    if channel == "imessage":
        return "imessage:local:%s" % message_id
    return "%s:%s" % (channel, message_id)


@contextlib.contextmanager
def _receipt_lock(timeout=2.0):
    fd = os.open(RECEIPTS + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError("receipts lock")
                time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _load_receipts():
    data = _load(RECEIPTS, None)
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("keys"), dict):
        return {"version": 1, "keys": {}}
    return data


def remember_receipts(keys):
    keys = [k for k in keys or [] if k]
    if not keys:
        return
    secure_dir(ROOT)
    with _receipt_lock():
        data = _load_receipts()
        now = time.time()
        data["keys"] = {k: t for k, t in data["keys"].items() if now - float(t) <= RECEIPT_KEEP_S}
        for k in keys:
            data["keys"][k] = now
        _save(RECEIPTS, data)


def receipts_known(keys):
    """True only when every key is non-empty and already archived by a CLI."""
    keys = list(keys or [])
    if not keys or not all(keys):
        return False
    try:
        have = _load_receipts()["keys"]
    except Exception:
        return False
    return all(k in have for k in keys)


def receipts_in_body(channel, body, workspace=None):
    """The receipt keys a tool's own output carries, for the hook. [] when none or unparseable."""
    body = body or ""
    if channel == "whatsapp":
        return [receipt_key("whatsapp", m.group(1)) for m in WA_EVENT_RE.finditer(body)]
    if channel == "slack":
        out = []
        for d in json_objects(body, cap=40):
            if isinstance(d, dict) and d.get("ok") is True and d.get("ts") and d.get("channel"):
                out.append(receipt_key("slack", str(d["ts"]),
                                       scope="%s:%s" % (workspace, d["channel"]) if workspace else None))
        return out
    if channel == "imessage":
        out = []
        for d in json_objects(body, cap=40):
            if isinstance(d, dict) and isinstance(d.get("sent"), list):
                out += [receipt_key("imessage", s.get("message_id")) if isinstance(s, dict) else None
                        for s in d["sent"]]
        return out
    return []


# ------------------------------------------------------------ CLI logging

def hook_logs_here():
    """True where a PostToolUse comms-log hook already archives each tool call: Claude Code
    (CLAUDECODE) and pi's cswap-claude-hooks bridge, which runs the same hooks and exports
    CLAUDE_CODE_SESSION_ID but not CLAUDECODE (2026-10-31: pi reads were archived twice)."""
    return bool(os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_SESSION_ID"))


def log_event(channel, direction, target, text=None, message_id=None, source="cli", *,
              verb=None, scope=None, body=None, files=None, recipients=None, cwd=None,
              command=None, hook_covers=True, account=None):
    """Archive one row from a comms CLI. Never raises; returns True when a row was written.

    Sends always log (one row per delivered part, keyed by its receipt). Reads log only outside
    a hook runtime (hook_logs_here(): there the PostToolUse hook already logs every read) and
    outside comms/inbox's own background reads (COMMS_INTERNAL=1)."""
    try:
        if direction == "read" and (hook_logs_here() or os.environ.get("COMMS_INTERNAL")):
            return False
        if not target:
            return False
        ts, et, month = _now()
        routes = _load(ROUTES, {})
        key = norm(target)
        hit = route(target, routes) or {}
        rkey = receipt_key(channel, message_id, scope) if direction == "send" else None
        if direction == "send" and rkey is None and hook_covers and hook_logs_here():
            return False    # no receipt to dedupe on, and the hook logs this send in a hook runtime
        body = body or ""
        text = text or None
        full_command = command or ""
        row = {
            "ts": round(ts, 3), "et": et, "channel": channel, "verb": verb or direction,
            "dir": direction, "target": key, "raw_target": target,
            "project": hit.get("project"), "label": hit.get("label"),
            "text": redact((text or "")[:BODY_CAP]) or None,
            "body": redact(body[:BODY_CAP]),
            "truncated": len(body) > BODY_CAP or len(text or "") > BODY_CAP,
            "text_source": "cli" if text else None,
            "files": files or None,
            "recipients": [norm(r) for r in (recipients or [])] or None,
            "cwd": cwd or os.getcwd(),
            "session": "",
            "command": redact(full_command[:1000]),
            "full_command": redact(full_command[:2000]) or None,
            "fc_cut": len(full_command) > 2000,
            "message_id": message_id,
            "receipt_key": rkey,
            "source": source,
        }
        if account:                 # an added Gmail account; the main account adds no field
            row["account"] = account
        meta = {"month": month, "key": key, "channel": channel, "ts": ts, "et": et,
                "project": hit.get("project"), "label": hit.get("label"), "hit": hit,
                "target": target, "draft_put": None}
        write_rows([(row, meta)], cwd=row["cwd"])
        # archive FIRST, receipt second: a crash in between can only duplicate a row
        if rkey:
            remember_receipts([rkey])
        return True
    except Exception as e:  # noqa: BLE001 - logging never changes a send's outcome
        try:
            import sys as _sys
            _sys.stderr.write("comms log: could not archive this %s (%s)\n" % (direction, e.__class__.__name__))
        except Exception:
            pass
        return False


class StdoutTee(object):
    """Pass stdout through unchanged and keep the first `cap` characters for the read log."""

    def __init__(self, stream, cap=BODY_CAP + 1):
        self._s = stream
        self._cap = cap
        self._buf = []
        self._n = 0

    def write(self, data):
        if self._n < self._cap:
            piece = data[: self._cap - self._n]
            self._buf.append(piece)
            self._n += len(piece)
        return self._s.write(data)

    def flush(self):
        return self._s.flush()

    def getvalue(self):
        return "".join(self._buf)

    def __getattr__(self, name):
        return getattr(self._s, name)
