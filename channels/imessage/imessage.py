#!/usr/bin/env python3
"""Safe, on-demand wrapper around openclaw/imsg (MIT).

On-demand is the design: this wrapper deliberately excludes watch and live-channel
behaviour, so nothing here pushes an arriving message into a session. It is a tool
somebody calls, never one that calls them.

Sending is headless and reaches anything it can address -- direct iMessage chats,
SMS and RCS chats, iMessage groups, file attachments, and a phone number or email
with no chat yet. Every send still goes through an immutable, content-hashed,
routing-bound record whose routing is revalidated immediately before delivery, and
every part is verified against the chat afterwards. The record is an audit trail
rather than a consent gate; the consent is the owner's yes in the conversation that
produced the draft.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 3
UPSTREAM_VERSION = "0.14.1"
UPSTREAM_BINARY_SHA256 = "5377aa4e8ae89a8650ec664af96893aaf3d577a646bb3873871d999960b816c1"
PREVIEW_TTL_SECONDS = 10 * 60
APPROVAL_DIALOG_SECONDS = 5 * 60
UI_DIAGNOSTIC_SECONDS = 60
# None means "ask draft_checker()": config `imessage.draft_checker`, else the sibling
# WhatsApp channel's checker. A missing file refuses every send. Tests may set a path.
DRAFT_CHECKER: Path | None = None
DRAFT_CHECK_SECONDS = 30
MAX_PREVIEW_BYTES = 64 * 1024
MAX_TEXT_CHARS = 4_000
# One typed phrase now authorizes a whole message rather than one bubble, so it
# must authorize a BOUNDED one. Two caps, and neither widens what a single
# approval could already send: at most five bubbles, and the same 4,000-character
# ceiling a lone bubble always had, now applied to the sequence total.
MAX_PREVIEW_BUBBLES = 5
# Once bubble 1 is dispatched the message is committed. The preview TTL must not
# abort the rest mid-thread, because a half-delivered message is the one outcome
# worse than a late one -- so the remaining bubbles run against their own bounded
# deadline instead of the TTL, and instead of no deadline at all.
SEND_SEQUENCE_GRACE_SECONDS = 120
PREVIEW_ID_RE = re.compile(r"^[0-9a-f]{24}$")
NONCE_RE = re.compile(r"^[0-9a-f]{16}$")
# "partial" is new with multi-bubble previews and is not a synonym for either
# neighbour: "failed" means nothing left, "uncertain" means we cannot tell, and
# "partial" means we know exactly which bubbles landed and that the rest never will.
PREVIEW_STATUSES = frozenset(
    {"pending", "sending", "sent", "partial", "failed", "uncertain", "invalid"}
)

# Read-side bounds. Search is cross-chat by construction, so it is capped
# separately from the per-chat history limit. 100 is not a policy choice: the
# messages.search RPC method answers -32602 Invalid params above it (measured
# 2026-09-09 -- 100 ok, 101 refused), while the CLI form accepts more but would
# put the query text in the process table. Bounded reads are the existing
# contract everywhere else here, so the RPC cap is the one worth keeping.
MAX_SEARCH_LIMIT = 100
# Floor for the per-search chat-verdict budget; callers raise it to the row
# limit, so this only bounds a caller that asks for fewer rows than chats.
MAX_SEARCH_CHAT_LOOKUPS = 40
SEARCH_MATCH_MODES = ("contains", "exact")
STATS_TOP_N = 10
WHOIS_KINDS = ("phone", "email")
# The same JSON-RPC surface also exposes send, message.edit, message.unsend,
# message.delete and tapback. A generic passthrough would therefore be a send
# path with no preview and no typed approval, so methods are allowlisted.
READ_ONLY_RPC_METHODS = frozenset({"messages.search"})
ISO_INSTANT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
# Anchored on the FIRST character so a handle can never look like a flag:
# "--limit" is all-legal under a bare character class that includes "-".
HANDLE_RE = re.compile(r"^[+A-Za-z0-9][A-Za-z0-9._%+@-]{0,127}$")
REGION_RE = re.compile(r"^[A-Za-z]{2}$")
SEND_SERVICES = ("imessage", "sms", "auto")
# Below this many digits a "phone number" is a shortcode, and suffix
# matching against one would collide with almost every real number.
MIN_PHONE_DIGITS = 7
# A full national number. `--to` demands at least this many digits, because a
# 7-digit local number cannot be routed reliably OR matched back to a chat.
NATIONAL_NUMBER_DIGITS = 10


class ImsgError(RuntimeError):
    """Expected safety, validation, or upstream command failure."""


class SendSequenceError(ImsgError):
    """A multi-bubble send stopped part way; carries exactly how far it got."""

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


class RpcSendError(ImsgError):
    """A redacted, disposition-aware JSON-RPC send failure."""

    def __init__(self, message: str, disposition: str = "unknown") -> None:
        super().__init__(message)
        self.disposition = disposition


# ---- Paths and config ------------------------------------------------------------------
# Personal values come from the kit config (~/.config/comms-kit/config.json). The shared
# modules are found from THIS file, never from a caller-set variable: the outbound gate
# and the pinned-binary check must not be redirectable from the environment.
OWNER_LABEL = "me"  # how the owner's own rows are labelled in read output


def core_dirs() -> list[Path]:
    here = os.path.abspath(__file__)
    return [Path(here).parents[2] / "core", Path.home() / ".claude" / "communications"]  # kit: code path


def comms_dir() -> Path:
    """The folder that holds the shared modules: the first one that has any."""
    dirs = core_dirs()
    return next((d for d in dirs if (d / "outbound_gate.py").is_file()
                 or (d / "commslib.py").is_file()), dirs[-1])


_KITCONFIG: dict[str, Any] = {}


def kitconfig_module() -> Any:
    if "module" not in _KITCONFIG:
        import importlib.util  # noqa: PLC0415
        _KITCONFIG["module"] = None
        for folder in core_dirs():
            path = folder / "kitconfig.py"
            if path.is_file():
                spec = importlib.util.spec_from_file_location("kitconfig", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                _KITCONFIG["module"] = module
                break
    return _KITCONFIG["module"]


def read_kit(fetch: Any) -> Any:
    """Run `fetch(kitconfig)`; None when the kit config module is absent.

    A broken config file stops the tool: guessing a path for a send tool is worse than failing.
    """
    kitconfig = kitconfig_module()
    if kitconfig is None:
        return None
    try:
        return fetch(kitconfig)
    except Exception as error:  # noqa: BLE001 - ConfigError and friends
        raise ImsgError(f"Kit config is unreadable; nothing was run: {error}") from error


def data_root() -> Path:
    value = read_kit(lambda kitconfig: kitconfig.data_dir())
    return Path(value) if value else Path.home() / ".local" / "share" / "comms-kit"


def upstream_binary() -> Path:
    value = read_kit(lambda kitconfig: kitconfig.path("imessage.binary_path"))
    default = data_root() / "imessage" / "vendor" / "imsg" / f"v{UPSTREAM_VERSION}" / "imsg"
    return Path(value) if value else default


def state_root() -> Path:
    value = read_kit(lambda kitconfig: kitconfig.path("imessage.state_dir"))
    return Path(value) if value else data_root() / "imessage" / "state"


def draft_checker() -> Path:
    value = read_kit(lambda kitconfig: kitconfig.path("imessage.draft_checker"))
    sibling = Path(os.path.abspath(__file__)).parents[1] / "whatsapp" / "draft_check.py"
    return Path(value) if value else sibling  # kit: code path


def iso_time(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat().replace("+00:00", "Z")


class OrderedPart(argparse.Action):
    """Append (kind, value) to one shared list, preserving argv order.

    argparse's own `append` keeps a separate list per flag, which loses the
    order BETWEEN flags -- the reason mixing --text and --text-file used to be
    refused outright. A message is a sequence, so the sequence is what gets
    recorded.
    """

    def __init__(self, option_strings, dest, kind=None, **kwargs):
        self.kind = kind
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        parts = getattr(namespace, self.dest, None)
        if parts is None:
            parts = []
            setattr(namespace, self.dest, parts)
        parts.append((self.kind, values))


class SingleValue(argparse.Action):
    """Refuse a repeated flag rather than silently taking the last one.

    The shell wrapper can only inspect argv[2], so `send --chat-id 104 --chat-id 7`
    satisfied its guard and then routed to 7. Last-wins is the wrong default for
    anything that decides WHERE a message goes.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} may only be given once")
        setattr(namespace, self.dest, values)


def append_send_audit(entry: dict[str, Any]) -> None:
    """Record that a send happened and under what verdict. Never the message text.

    The override reason used to exist only on the caller's stdout, so an
    unattended sender that discarded stdout left no trace that a gate had been
    waived at all. The content-hashed preview cannot carry it without breaking
    its own digest, so the audit trail lives beside it instead.
    """
    path = state_root() / "send-audit.jsonl"
    ensure_private_dir(path.parent)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.chmod(path, 0o600)


def print_json(value: Any, *, stream: Any = sys.stdout) -> None:
    json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
    stream.write("\n")


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ImsgError(f"Unsafe state path (expected a real directory): {path}")
    if info.st_uid != os.getuid():
        raise ImsgError(f"State directory is not owned by the current user: {path}")
    os.chmod(path, 0o700)


def preview_dir(status: str) -> Path:
    if status not in PREVIEW_STATUSES:
        raise ImsgError("Invalid preview status")
    path = state_root() / "previews" / status
    ensure_private_dir(path)
    return path


def preview_path(preview_id: str, status: str = "pending") -> Path:
    if not PREVIEW_ID_RE.fullmatch(preview_id):
        raise ImsgError("Invalid preview ID")
    return preview_dir(status) / f"{preview_id}.json"


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private_json_exclusive(path: Path, value: Any) -> None:
    ensure_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise ImsgError(f"Could not create private preview: {path.name}") from error
    try:
        payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def read_private_json(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ImsgError(f"Preview is unavailable: {path.name}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ImsgError(f"Unsafe preview file: {path}")
        if info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ImsgError(f"Unsafe preview ownership or link count: {path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ImsgError(f"Preview permissions are too broad: {path}")
        if info.st_size > MAX_PREVIEW_BYTES:
            raise ImsgError(f"Preview is unexpectedly large: {path}")
        raw = os.read(descriptor, MAX_PREVIEW_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_PREVIEW_BYTES:
        raise ImsgError(f"Preview is unexpectedly large: {path}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImsgError(f"Could not decode preview {path.name}") from error
    if not isinstance(value, dict):
        raise ImsgError("Preview document must be a JSON object")
    return value


# Attachments legitimately come from anywhere -- Desktop, Downloads, a client
# folder -- so they cannot be confined to the drafts directory the way bubbles
# are. That leaves one cheap, high-value guard: the handful of paths where a
# mistaken send is not an embarrassment but a credential disclosure. This is a
# blocklist, so it is not a security boundary; it is a spell-check for the worst
# typo in the set.
# A spell-check, not a boundary. It exists so a mistaken `--file` is an error
# message instead of a credential disclosure, and it is split into four kinds of
# rule because one flat substring list got BOTH directions wrong: it missed the
# six secret files most likely to be on this Mac, and it refused `.envrc` and
# `client.envelope.pdf` because ".env" happened to appear inside them.
SENSITIVE_PATH_FRAGMENTS = (
    "/.ssh/", "/.gnupg/", "/.aws/", "/.kube/", "/.docker/", "/.config/gh/",
    "/.config/gcloud/", "/.password-store/", "/.gem/", "/.cargo/registry/",
    "/library/keychains/", "/library/messages/",
    "/library/application support/google/chrome/",
    "/library/application support/firefox/profiles/",
    "/library/cookies/",
)
SENSITIVE_BASENAMES = frozenset({
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk",
    ".env", ".netrc", ".pgpass", ".npmrc", ".pypirc", ".gitconfig",
    ".git-credentials", ".claude.json", ".zsh_history", ".bash_history",
    "credentials", "credentials.json", ".credentials.json",
    "tokens.json", "token.json", "secrets.json", "service-account.json",
    "chat.db", "login data", "cookies", "key3.db", "key4.db", "logins.json",
})
# A credential by file type. `.key` is deliberately NOT here: Keynote decks use
# it, and refusing one of the owner's presentations with "rename it and send that
# copy" would be both wrong and confusing.
SENSITIVE_SUFFIXES = (
    ".pem", ".p12", ".pfx", ".p8", ".keystore", ".jks", ".ovpn", ".kdbx",
    ".asc", ".gpg", ".env", ".keychain", ".keychain-db",
)
# Heuristic, and matched on the BASENAME only. Matching these anywhere in the
# path is what made an innocent file inside a folder called "secrets" unsendable.
SENSITIVE_NAME_HINTS = ("keychain", "credential", "apikey", "api_key", "privatekey",
                        "private_key", "secrets", "password")
# The name hints, and ONLY the hints, stand down for a picture or a video. An
# image cannot be a key file, and refusing `keychain-diagram.png` with "rename it"
# is the kind of wrong that teaches a caller to stop trusting the guard. A PDF is
# deliberately not on this list: `passwords.pdf` is exactly the thing to catch.
INERT_MEDIA_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".heic", ".webp", ".bmp", ".tiff",
    ".mov", ".mp4", ".m4v", ".webm",
)
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024


def reject_sensitive_path(path: str) -> None:
    """Refuse a credential-shaped path. Pure string work, no filesystem.

    Separate from validate_attachment so it can be re-run at preview LOAD time.
    A stored preview is content-hashed, but anyone who can rewrite the file can
    recompute the hash, so every content rule has to hold again when it is read
    back -- and this one costs nothing to re-run.
    """
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    hit = (
        next((f for f in SENSITIVE_PATH_FRAGMENTS if f in lowered), None)
        or (name if name in SENSITIVE_BASENAMES else None)
        or next((x for x in SENSITIVE_SUFFIXES if name.endswith(x)), None)
        or (
            None if name.endswith(INERT_MEDIA_SUFFIXES)
            else next((h for h in SENSITIVE_NAME_HINTS if h in name), None)
        )
    )
    if hit is not None:
        raise ImsgError(
            f"Refusing to attach {path}: it matches {hit!r}, which is where "
            "credentials live. This is a spell-check, not a judgement -- if it really "
            "is an ordinary file, show the owner the path and let them say so, then send a "
            "renamed copy. Nothing was sent."
        )


def validate_attachment(path: Path) -> dict[str, Any]:
    """Check one outgoing attachment and return what is needed to verify it later.

    Deliberately NOT confined to the drafts directory: a real attachment lives
    wherever the owner keeps it. The size and the basename come back because an
    attachment carries no text, so a delivered row has to be matched on those.
    """
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ImsgError(f"Attachment not found: {path}") from error
    # Judge the TARGET of a symlink, not the link, which is why this runs on
    # the resolved path rather than the one the caller typed.
    reject_sensitive_path(str(resolved))
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ImsgError(f"Attachment must be a regular file: {resolved}")
    if info.st_size == 0:
        raise ImsgError(f"Attachment is empty: {resolved}")
    if info.st_size > MAX_ATTACHMENT_BYTES:
        raise ImsgError(
            f"Attachment is {info.st_size} bytes, over the "
            f"{MAX_ATTACHMENT_BYTES}-byte ceiling: {resolved}"
        )
    try:
        with open(resolved, "rb"):
            pass
    except OSError as error:
        raise ImsgError(f"Attachment cannot be read: {resolved}") from error
    return {"path": str(resolved), "name": resolved.name, "bytes": info.st_size}


def read_private_text(path: Path) -> str:
    # Mode 0600 and current-user ownership describe an SSH private key and a
    # .env exactly as well as they describe a draft, and both fit under the
    # 4,000-character cap. Nothing downstream ever shows the bytes to a human,
    # so the path is the only place this can be caught: a draft lives in the
    # drafts directory or it is not a draft.
    drafts = (state_root() / "drafts").resolve()
    try:
        resolved = path.resolve()
    except OSError as error:
        raise ImsgError(f"Draft path cannot be resolved: {path.name}") from error
    if resolved.parent != drafts:
        raise ImsgError(
            f"Drafts must live directly in {drafts}; refusing to read {resolved.parent}. "
            "Write the bubble there first rather than pointing at an arbitrary file."
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ImsgError(f"Draft file is unavailable: {path.name}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ImsgError("Draft must be a regular, current-user-owned file with one link")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ImsgError("Draft permissions are too broad; chmod 0600 before preparing")
        if info.st_size > MAX_TEXT_CHARS * 4:
            raise ImsgError("Draft file is unexpectedly large")
        raw = os.read(descriptor, MAX_TEXT_CHARS * 4 + 1)
    finally:
        os.close(descriptor)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ImsgError("Draft must be UTF-8 text") from error


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_upstream_binary() -> Path:
    binary = upstream_binary()
    try:
        info = binary.lstat()
    except OSError as error:
        raise ImsgError(f"Pinned imsg runtime is unavailable: {binary}") from error
    if binary.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ImsgError("Pinned imsg runtime must be a regular, non-symlink file")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise ImsgError("Pinned imsg runtime has unsafe ownership or write permissions")
    if not os.access(binary, os.X_OK):
        raise ImsgError("Pinned imsg runtime is not executable")
    if sha256_file(binary) != UPSTREAM_BINARY_SHA256:
        raise ImsgError("Pinned imsg runtime checksum mismatch; reinstall the verified release")
    try:
        version = subprocess.run(
            [str(binary), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired as error:
        raise ImsgError("Pinned imsg runtime version check timed out") from error
    if version.returncode != 0 or version.stdout.strip() != UPSTREAM_VERSION:
        raise ImsgError(f"Pinned imsg runtime must report exact version {UPSTREAM_VERSION}")
    return binary


def bounded_error_text(value: str) -> str:
    compact = " ".join(value.split())
    return compact[:500] if compact else "unknown upstream failure"


def run_imsg(arguments: Iterable[str], *, timeout: int = 30) -> str:
    binary = validate_upstream_binary()
    argv = list(arguments)
    try:
        completed = subprocess.run(
            [str(binary), *argv],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ImsgError("imsg timed out before completing the read-only command") from error
    if completed.returncode != 0:
        detail = bounded_error_text(completed.stderr or completed.stdout)
        raise ImsgError(f"imsg exited {completed.returncode}: {detail}")
    return completed.stdout


def parse_json_lines(raw: str, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ImsgError(f"Invalid JSON from imsg {label} at line {line_number}") from error
        if not isinstance(value, dict):
            raise ImsgError(f"Unexpected non-object JSON from imsg {label}")
        rows.append(value)
    return rows


def list_chats(
    limit: int, *, unread_only: bool = False, timeout: int = 30
) -> list[dict[str, Any]]:
    arguments = ["chats", "--limit", str(limit)]
    if unread_only:
        arguments.append("--unread-only")
    arguments.append("--json")
    return parse_json_lines(run_imsg(arguments, timeout=timeout), "chats")


def chat_detail(chat_id: int) -> dict[str, Any]:
    rows = parse_json_lines(
        run_imsg(["group", "--chat-id", str(chat_id), "--json"]),
        "group",
    )
    if len(rows) != 1:
        raise ImsgError(f"Expected one chat detail record for chat {chat_id}, got {len(rows)}")
    detail = rows[0]
    if detail.get("id") != chat_id:
        raise ImsgError("imsg returned a different chat ID than requested")
    return detail


def require_direct_imessage(detail: dict[str, Any]) -> dict[str, Any]:
    if detail.get("is_group") is not False:
        raise ImsgError("Group chats are blocked by the iMessage wrapper")
    if str(detail.get("service", "")).casefold() != "imessage":
        raise ImsgError("Only existing iMessage chats are allowed; SMS and unknown services are blocked")
    return _require_one_to_one(detail)


# Owner decision: let the tool read SMS chats, read only. The same services
# headless send allows since 2026-09-09. require_direct_imessage stays iMessage-only:
# it still guards the dialog send path.
READABLE_DIRECT_SERVICES = {"imessage", "sms", "rcs"}


def require_readable_direct(detail: dict[str, Any]) -> dict[str, Any]:
    if detail.get("is_group") is not False:
        raise ImsgError("Group chats are blocked by the iMessage wrapper")
    service = str(detail.get("service", "")).casefold()
    if service not in READABLE_DIRECT_SERVICES:
        raise ImsgError(
            f"Chat service is {service or 'unknown'}; only iMessage, SMS and RCS "
            "one-to-one chats are readable"
        )
    return _require_one_to_one(detail)


def _require_one_to_one(detail: dict[str, Any]) -> dict[str, Any]:
    participants = detail.get("participants")
    if (
        not isinstance(participants, list)
        or len(participants) != 1
        or not isinstance(participants[0], str)
        or not participants[0].strip()
    ):
        raise ImsgError("A direct chat must have exactly one authoritative participant handle")
    identifier = detail.get("identifier")
    guid = detail.get("guid")
    if not isinstance(identifier, str) or not identifier.strip() or ";+;" in identifier:
        raise ImsgError("Direct-chat identifier is missing or group-shaped")
    guid_parts = guid.split(";") if isinstance(guid, str) else []
    if len(guid_parts) < 3 or guid_parts[1] != "-" or ";+;" in guid:
        raise ImsgError("Direct-chat GUID is missing or inconsistent with one-to-one routing")
    return detail


def require_sendable_chat(detail: dict[str, Any]) -> dict[str, Any]:
    """Eligibility for SENDING after 2026-09-09, once groups and SMS were unblocked.

    Deliberately looser than require_direct_imessage, which still guards the
    dialog path. What it does NOT drop: the chat has to exist, be addressable,
    and carry at least one real participant. Those are not policy, they are what
    routing revalidation compares against later -- a snapshot of nothing
    revalidates against anything.
    """
    # Measured on one real Mac: direct iMessage, direct SMS,
    # iMessage groups and RCS chats all occur. RCS was nearly missed -- it is not one of the
    # runtime's three --service values, so an RCS chat is reached as sms/auto,
    # but it is still a real chat he has and refusing it would look like a bug.
    service = str(detail.get("service", "")).casefold()
    if service not in {"imessage", "sms", "rcs"}:
        raise ImsgError(
            f"Chat service is {service or 'unknown'}; only iMessage, SMS and RCS chats "
            "can be sent to"
        )
    participants = detail.get("participants")
    if not isinstance(participants, list) or not participants:
        raise ImsgError("Chat has no authoritative participant handles; refusing to send")
    if not all(isinstance(value, str) and value.strip() for value in participants):
        raise ImsgError("Chat has a blank or non-string participant handle; refusing to send")
    identifier = detail.get("identifier")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ImsgError("Chat identifier is missing; refusing to send")
    guid = detail.get("guid")
    if not isinstance(guid, str) or not guid.strip():
        raise ImsgError("Chat GUID is missing; refusing to send")
    guid_parts = guid.split(";")
    if len(guid_parts) < 3 or guid_parts[1] not in {"-", "+"}:
        raise ImsgError("Chat GUID is malformed; refusing to send")
    is_group = bool(detail.get("is_group"))
    if is_group != (guid_parts[1] == "+"):
        raise ImsgError(
            "Chat GUID shape disagrees with is_group; refusing to send on inconsistent routing"
        )
    if is_group and len(participants) < 2:
        raise ImsgError("Chat claims to be a group but lists fewer than two participants")
    if not is_group and len(participants) != 1:
        raise ImsgError("A direct chat must have exactly one authoritative participant handle")
    return detail


def sendable_chat(chat_id: int) -> dict[str, Any]:
    return require_sendable_chat(chat_detail(chat_id))


def safe_chat(chat_id: int) -> dict[str, Any]:
    return require_direct_imessage(chat_detail(chat_id))


def classify_readable_chat(detail: dict[str, Any]) -> dict[str, Any]:
    """Allow one-to-one iMessage, SMS and RCS chats, and authoritative existing
    iMessage groups, for reads.

    Kept as a pure predicate over one chat record so that the recent-chat
    listing and an authoritative per-chat lookup cannot drift into two
    different definitions of "readable". Measured 2026-09-09: the two sources
    classify all 362 local chats identically, which is what lets `search`
    filter from the cheap listing instead of one lookup per hit.
    """
    if detail.get("is_group") is not True:
        return require_readable_direct(detail)
    if str(detail.get("service", "")).casefold() != "imessage":
        raise ImsgError("Only existing iMessage groups are readable; SMS and unknown services are blocked")
    participants = detail.get("participants")
    if not isinstance(participants, list) or not participants or any(
        not isinstance(handle, str) or not handle.strip() for handle in participants
    ):
        raise ImsgError("A group read requires authoritative participant handles")
    identifier = detail.get("identifier")
    guid = detail.get("guid")
    guid_parts = guid.split(";") if isinstance(guid, str) else []
    if (
        not isinstance(identifier, str)
        or not identifier.strip()
        or len(guid_parts) != 3
        or guid_parts[0].casefold() not in {"any", "imessage"}
        or guid_parts[1] != "+"
        or guid_parts[2] != identifier
    ):
        raise ImsgError("Group-chat identifier and GUID are missing or inconsistent")
    return detail


def readable_chat(chat_id: int) -> dict[str, Any]:
    return classify_readable_chat(chat_detail(chat_id))


def require_instant(value: str, flag: str) -> str:
    text = value.strip()
    if not ISO_INSTANT_RE.fullmatch(text):
        raise ImsgError(f"{flag} must be an ISO-8601 UTC instant such as 2026-09-01T00:00:00Z")
    return text


def require_handle(value: str) -> str:
    text = value.strip()
    if not HANDLE_RE.fullmatch(text):
        raise ImsgError("A participant filter must be a plain phone number or email address")
    return text


def history_rows(
    chat_id: int,
    limit: int,
    *,
    attachments: bool = False,
    since: str | None = None,
    until: str | None = None,
    participants: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    arguments = ["history", "--chat-id", str(chat_id), "--limit", str(limit)]
    if attachments:
        arguments.append("--attachments")
    if since is not None:
        # Upstream spells the window --start/--end; this wrapper spells it
        # --since/--until so it cannot be confused with the send path's
        # started/expiry timestamps.
        arguments += ["--start", require_instant(since, "--since")]
    if until is not None:
        arguments += ["--end", require_instant(until, "--until")]
    handles = [require_handle(handle) for handle in (participants or [])]
    if handles:
        arguments += ["--participants", ",".join(handles)]
    arguments.append("--json")
    return parse_json_lines(run_imsg(arguments), "history")


def history_window(
    detail: dict[str, Any],
    *,
    since: str | None,
    until: str | None,
    participants: Iterable[str],
) -> dict[str, Any]:
    """Describe the read window, and say where a filter does not mean what it looks like.

    `--participant` matches the row's `sender` column. Measured 2026-09-09
    across four one-to-one chats: an OUTGOING row in a direct chat carries the
    other party's handle in `sender`, not the owner's. So filtering a direct chat
    by the person you are talking to returns the same rows it would anyway
    (20 of 20 on two chats) and does NOT isolate what they wrote -- a silent
    trap for any summary that then attributes the owner's own words to them.
    Reporting it beats documenting it, because the caller sees this and not the
    skill file.
    """
    handles = list(participants)
    window: dict[str, Any] = {"since": since, "until": until, "participants": handles}
    if handles and detail.get("is_group") is not True:
        window["note"] = (
            "A participant filter matches each row's sender, and in a one-to-one chat "
            "an outgoing row carries the OTHER party's handle. This filter is therefore "
            "close to a no-op here and does not isolate what they wrote. Decide who "
            "wrote a row from is_from_me, not from this filter."
        )
    return window


def run_imsg_read_rpc(method: str, params: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    """Call one allowlisted read-only JSON-RPC method over stdin.

    A search query goes over stdin for the same reason message text does: a
    process argument is readable by every other process on the machine. The
    method is checked against READ_ONLY_RPC_METHODS first, because this same
    transport reaches send, edit, unsend, delete and tapback.
    """
    if method not in READ_ONLY_RPC_METHODS:
        raise ImsgError(f"Refusing to call a non-read-only RPC method: {method}")
    binary = validate_upstream_binary()
    request_id = f"read-{os.urandom(8).hex()}"
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    rpc_input = json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        completed = subprocess.run(
            [str(binary), "rpc"],
            input=rpc_input,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ImsgError(f"imsg RPC {method} timed out") from error
    if completed.returncode != 0:
        raise ImsgError(f"imsg RPC exited {completed.returncode}: {bounded_error_text(completed.stderr)}")
    responses = parse_json_lines(completed.stdout, f"rpc {method}")
    response = next((row for row in responses if row.get("id") == request_id), None)
    if response is None:
        raise ImsgError(f"imsg RPC {method} returned no matching response")
    error_value = response.get("error")
    if isinstance(error_value, dict):
        raise ImsgError(f"imsg RPC {method} failed (code {error_value.get('code')})")
    result = response.get("result")
    if not isinstance(result, dict):
        raise ImsgError(f"imsg RPC {method} returned malformed result data")
    return result


class ChatEligibility:
    """One readable-chat verdict per chat, from the canonical predicate.

    Search is cross-chat, and some chats (SMS groups, unknown services, malformed
    rows) are ones the read interface refuses, so every hit needs a verdict on its chat.

    Verdicts are looked up lazily, one authoritative `imsg group` call per
    distinct chat, cached. An earlier version pre-loaded the whole chat listing
    to avoid those subprocesses; that was measured backwards. On 2026-09-09:
    `imsg chats --limit 500` costs **6100 ms** while `imsg group --chat-id N`
    costs **32 ms**, so the listing only pays off above roughly 190 distinct
    chats -- and a search returns at most MAX_SEARCH_LIMIT rows, so it can
    never touch that many. Lazy is both faster here and authoritative.

    The budget bounds the worst case and is never expected to bind, because
    callers set it to at least the row limit. A chat left unchecked is
    reported, never silently treated as ineligible.
    """

    def __init__(self, *, lookup_budget: int = MAX_SEARCH_CHAT_LOOKUPS) -> None:
        self._verdicts: dict[int, dict[str, Any] | None] = {}
        self._lookups_left = lookup_budget
        self.unchecked: set[int] = set()

    @staticmethod
    def _classify(detail: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return classify_readable_chat(detail)
        except ImsgError:
            return None

    def readable(self, chat_id: Any) -> bool:
        if not isinstance(chat_id, int):
            return False
        if chat_id not in self._verdicts:
            if self._lookups_left <= 0:
                self.unchecked.add(chat_id)
                return False
            self._lookups_left -= 1
            try:
                self._verdicts[chat_id] = self._classify(chat_detail(chat_id))
            except ImsgError:
                self._verdicts[chat_id] = None
        return self._verdicts[chat_id] is not None

    def label(self, chat_id: int) -> dict[str, Any]:
        detail = self._verdicts.get(chat_id) or {}
        return {
            "id": chat_id,
            "name": detail.get("name") or detail.get("display_name") or detail.get("contact_name"),
            "is_group": detail.get("is_group"),
            "service": detail.get("service"),
            "participants": detail.get("participants", []),
        }


def search_messages(query: str, match: str, limit: int) -> dict[str, Any]:
    """Search local Messages history, filtered to chats this interface may read."""
    if match not in SEARCH_MATCH_MODES:
        raise ImsgError(f"Search match mode must be one of: {', '.join(SEARCH_MATCH_MODES)}")
    text = query.strip()
    if not text:
        raise ImsgError("Search query cannot be empty")
    if len(text) > MAX_TEXT_CHARS:
        raise ImsgError("Search query is too long")

    result = run_imsg_read_rpc("messages.search", {"query": text, "match": match, "limit": limit})
    rows = result.get("messages")
    if not isinstance(rows, list):
        raise ImsgError("imsg search returned no message list")

    # At most `limit` rows come back, so at most `limit` distinct chats need a
    # verdict: the budget cannot bind, and unchecked_chats stays empty.
    eligibility = ChatEligibility(lookup_budget=max(limit, MAX_SEARCH_CHAT_LOOKUPS))
    kept: list[dict[str, Any]] = []
    chats: dict[int, dict[str, Any]] = {}
    refused_chats: set[int] = set()
    refused_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ImsgError("imsg search returned a non-object message row")
        chat_id = row.get("chat_id")
        if not eligibility.readable(chat_id):
            refused_rows += 1
            if isinstance(chat_id, int):
                refused_chats.add(chat_id)
            continue
        kept.append(row)
        chats.setdefault(chat_id, eligibility.label(chat_id))

    return {
        "query": {"text": text, "match": match, "limit": limit},
        "messages": kept,
        "chats": [chats[key] for key in sorted(chats)],
        "excluded": {
            "messages": refused_rows,
            "chats": sorted(refused_chats),
            "unchecked_chats": sorted(eligibility.unchecked),
            "reason": "not an existing iMessage chat this interface may read",
        },
        "scope": (
            f"Newest-first, capped at {limit} matched rows BEFORE the eligibility filter. "
            "A short result is not proof that nothing older matches."
        ),
    }


def whois_local(address: str, kind: str, *, chat_scan_limit: int = 200) -> dict[str, Any]:
    """Look up a handle from local history only; never the live IMCore probe.

    Also reports which existing chats carry the handle. "What service is this
    person on" and "is there a chat I could prepare a send for" are the same
    question in practice, and the second is what the answer gets used for. An
    earlier version returned a hardcoded ``sendable_here: False``, which was
    simply wrong for every handle that does have a direct chat.

    The chat scan is bounded exactly like ``resolve``'s, so a handle whose only
    conversation is older than the window reads as having none. That limit is
    stated in the result rather than implied.
    """
    if kind not in WHOIS_KINDS:
        raise ImsgError(f"Handle type must be one of: {', '.join(WHOIS_KINDS)}")
    handle = require_handle(address)
    rows = parse_json_lines(
        run_imsg(["whois", "--address", handle, "--type", kind, "--local", "--json"]),
        "whois",
    )
    if len(rows) != 1:
        raise ImsgError(f"Expected one whois record, got {len(rows)}")
    record = dict(rows[0])

    normalized = handle.casefold()
    direct: list[int] = []
    groups: list[int] = []
    for chat in list_chats(chat_scan_limit):
        chat_id = chat.get("id")
        participants = chat.get("participants")
        if not isinstance(chat_id, int) or not isinstance(participants, list):
            continue
        # Exact handle match only, like resolve. A stored handle is E.164 or an
        # email address; guessing at other spellings would silently widen this.
        if not any(isinstance(v, str) and v.strip().casefold() == normalized for v in participants):
            continue
        try:
            detail = classify_readable_chat(chat)
        except ImsgError:
            continue
        (groups if detail.get("is_group") else direct).append(chat_id)

    record["readable_direct_chat_ids"] = sorted(direct)
    record["readable_group_chat_ids"] = sorted(groups)
    record["has_readable_direct_chat"] = bool(direct)
    record["chats_scanned"] = chat_scan_limit
    record["scope"] = (
        "Service is inferred from local chat.db history only; unknown means no local "
        "history, not that the handle is unreachable, and reachability is never probed "
        f"live. Chat IDs come from an exact handle match over the {chat_scan_limit} most "
        "recent chats. A readable direct chat is a precondition for a send, not "
        "permission: a send still needs its own preview and freshly typed approval."
    )
    return record


def stats_summary() -> dict[str, Any]:
    """Bounded aggregate counts. The raw upstream payload is a per-day series."""
    rows = parse_json_lines(run_imsg(["stats", "--json"], timeout=120), "stats")
    if len(rows) != 1:
        raise ImsgError(f"Expected one stats record, got {len(rows)}")
    raw = rows[0]

    def top(key: str) -> list[Any]:
        value = raw.get(key)
        return value[:STATS_TOP_N] if isinstance(value, list) else []

    dates = raw.get("dates") if isinstance(raw.get("dates"), list) else []
    day_values = [str(day.get("date")) for day in dates if isinstance(day, dict) and day.get("date")]
    return {
        "totals": {
            "messages": raw.get("total_messages"),
            "sent": raw.get("sent_messages"),
            "received": raw.get("received_messages"),
            "chats": len(raw.get("chats") or []),
            "senders": len(raw.get("senders") or []),
        },
        "time_zone": raw.get("time_zone"),
        "days_covered": len(day_values),
        "first_day": min(day_values) if day_values else None,
        "last_day": max(day_values) if day_values else None,
        "services": raw.get("services", []),
        "top_chats": top("chats"),
        "top_senders": top("senders"),
        "scope": (
            "Counts cover EVERY local conversation, including SMS and other services this "
            f"interface refuses to open. Top lists are capped at {STATS_TOP_N}; the per-day "
            "series is summarised, not returned."
        ),
    }


def advanced_feature_status() -> dict[str, Any]:
    """Report why edit/unsend/tapback/mark-read do not exist here.

    They are not withheld by this wrapper alone: the upstream runtime needs a
    helper dylib injected into Messages.app, which needs System Integrity
    Protection turned off. Reporting the machine state stops the next session
    rediscovering it.
    """
    report: dict[str, Any] = {
        "requires_sip_disabled": [
            "edit", "unsend", "delete-message", "tapback", "mark-read", "typing",
            "chat-create", "group management", "send effects", "polls", "live whois",
        ],
        # The owner unblocked SMS, group sends, new recipients and attachment sends. What is left withheld is anything that would make this
        # a service instead of a tool he calls.
        "withheld_by_this_wrapper": [
            "live watch", "inbound push", "Messages UI automation",
        ],
        "sending_allowed_here": [
            "existing direct iMessage chats", "existing SMS and RCS chats",
            "existing iMessage groups", "attachments", "a new phone number or email",
        ],
    }
    try:
        rows = parse_json_lines(run_imsg(["status", "--json"]), "status")
    except ImsgError as error:
        report["upstream_status"] = f"unavailable: {error}"
        return report
    if len(rows) != 1:
        # Say so rather than reporting every field as absent, which reads
        # identically to "SIP is on" and is a different fact.
        report["upstream_status"] = f"unreadable: expected one status record, got {len(rows)}"
        return report
    raw = rows[0]
    report["sip"] = raw.get("sip")
    report["upstream_advanced_features"] = raw.get("advanced_features")
    report["bridge_version"] = raw.get("bridge_version")
    report["available_here"] = bool(raw.get("advanced_features"))
    return report


def exact_chat_values(chat: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("id", "identifier", "guid", "name", "display_name", "contact_name"):
        value = chat.get(key)
        if value is not None and str(value).strip():
            values.append(str(value).strip())
    participants = chat.get("participants")
    if isinstance(participants, list):
        values.extend(str(value).strip() for value in participants if str(value).strip())
    return values


def resolve_exact_chat(query: str, limit: int) -> dict[str, Any]:
    normalized = query.strip().casefold()
    if not normalized:
        raise ImsgError("Exact chat query cannot be empty")
    if normalized.isdecimal():
        return readable_chat(int(normalized))

    matches: dict[int, dict[str, Any]] = {}
    for chat in list_chats(limit):
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            continue
        if any(value.casefold() == normalized for value in exact_chat_values(chat)):
            matches[chat_id] = chat
    if not matches:
        raise ImsgError(
            f"No exact chat match for {query!r}. List chats and use the exact numeric chat ID."
        )
    if len(matches) > 1:
        candidates = ", ".join(
            f"{chat_id}:{matches[chat_id].get('name') or '(unnamed)'}" for chat_id in sorted(matches)
        )
        raise ImsgError(f"Ambiguous exact chat match; choose a numeric chat ID: {candidates}")

    listing = matches[next(iter(matches))]
    detail = readable_chat(next(iter(matches)))
    # The chat LISTING carries contact_name/display_name; chat_detail does not.
    # Re-running the QUERY against the detail therefore always failed for a
    # direct chat matched by contact name -- `resolve "Manny"` returned
    # "details changed during resolution" every time, while a group matched by
    # `name` succeeded because name does survive into the detail. Compare the
    # stable identity instead, which is what this check was actually guarding.
    for key in ("id", "guid", "identifier"):
        expected = listing.get(key)
        if expected is not None and detail.get(key) != expected:
            raise ImsgError("Authoritative chat details changed during resolution; resolve again")
    return detail


def routing_snapshot(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "chat",
        "id": detail.get("id"),
        "identifier": detail.get("identifier"),
        "guid": detail.get("guid"),
        "service": detail.get("service"),
        "is_group": detail.get("is_group"),
        "participants": sorted(str(value) for value in detail.get("participants", [])),
    }


def handle_routing_snapshot(handle: str, service: str, region: str) -> dict[str, Any]:
    """Routing for a recipient who has no chat yet.

    Deliberately a DIFFERENT shape from a chat snapshot rather than a chat
    snapshot with holes in it. There is no rowid, no GUID and no participant
    list here, so every check that guards a chat send would pass vacuously if
    this borrowed that shape -- and passing vacuously is how a gate disappears
    without anyone deleting it.
    """
    return {
        "kind": "handle",
        "handle": handle,
        "service": service,
        "region": region.upper(),
    }


def handle_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def handles_match(candidate: str, handle: str) -> bool:
    """Decide whether a chat's participant is the person --to names.

    A user types `2065550100`; Messages stores `+12065550100`. Only the runtime
    knows the real normalization, because it takes a region, so this compares
    digits and accepts one being a suffix of the other.
    """
    if "@" in handle or "@" in candidate:
        return candidate.strip().casefold() == handle.strip().casefold()
    left, right = handle_digits(candidate), handle_digits(handle)
    if not left or not right:
        return False
    if left == right:
        return True
    # Anything looser than "one carries a single-digit country code the other
    # omits" sends to the wrong person. An open suffix match made `--to 555-1234`
    # -- exactly what a model extracts from a transcript -- deliver to whichever
    # of the owner's direct chats happens to end in those digits, and then verify
    # it as a success. A 2-digit prefix is no safer: a UK number can end with
    # the same ten digits as a US number.
    longer, shorter = (left, right) if len(left) > len(right) else (right, left)
    if len(shorter) < NATIONAL_NUMBER_DIGITS:
        return False
    return len(longer) == len(shorter) + 1 and longer.endswith(shorter)


# The chat listing is the slow call and its timing is not stable. It is
# documented at ~2.4s for 200 chats, but measured 2026-09-09 under load it took
# 20s and sometimes blew the default 30s timeout -- on 10% CPU, so it is waiting
# on the database, not computing. A `--to` send must not fail for that reason:
# it fails closed, which is correct, but it fails on a machine being busy.
# Shrinking the window instead would be worse, because a chat just outside it
# means opening a SECOND thread to somebody the owner already texts.
HANDLE_SCAN_SECONDS = 120


def direct_chats_for_handle(handle: str, limit: int = 200) -> list[dict[str, Any]]:
    """Every recent one-to-one chat that already reaches this handle."""
    found: list[dict[str, Any]] = []
    for chat in list_chats(limit, timeout=HANDLE_SCAN_SECONDS):
        if chat.get("is_group"):
            continue
        participants = chat.get("participants") or []
        if len(participants) != 1 or not isinstance(participants[0], str):
            continue
        if handles_match(participants[0], handle):
            found.append(chat)
    return found


def require_send_handle(value: str) -> str:
    text = value.strip()
    if not HANDLE_RE.fullmatch(text):
        raise ImsgError("--to must be a plain phone number or email address")
    if "@" not in text and len(handle_digits(text)) < NATIONAL_NUMBER_DIGITS:
        raise ImsgError(
            f"--to needs a full {NATIONAL_NUMBER_DIGITS}-digit number, or E.164 with "
            "its country code. A local or short number cannot be routed reliably, and "
            "cannot be matched back to the chat it lands in, so the send could not be "
            "verified. Nothing was sent."
        )
    return text


def display_snapshot(detail: dict[str, Any]) -> dict[str, Any]:
    # Most groups have no name at all, so the old snapshot rendered
    # a wall of raw handles with nothing identifying the target. A group name is
    # also renameable by any participant and is NOT part of routing, so it is
    # derived here for display and never trusted for delivery.
    participants = list(detail.get("participants") or [])
    name = detail.get("name")
    if detail.get("is_group") and not (isinstance(name, str) and name.strip()):
        shown = ", ".join("..." + str(value)[-4:] for value in participants[:5])
        rest = f" +{len(participants) - 5} more" if len(participants) > 5 else ""
        name = f"Group of {len(participants)}: {shown}{rest}"
    return {
        "name": name,
        "participants": participants,
        "service": detail.get("service"),
    }


def canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_payload(payload)).hexdigest()


def approval_phrase(preview_id: str) -> str:
    return f"SEND IMESSAGE {preview_id}"


def max_message_id(rows: Iterable[dict[str, Any]]) -> int:
    return max((row.get("id", 0) for row in rows if isinstance(row.get("id"), int)), default=0)


def recent_context(rows: list[dict[str, Any]], count: int = 5) -> list[dict[str, Any]]:
    """The last few messages, with the writer named correctly.

    Pinned imsg history is newest-first; taking the tail exposes stale context.

    And `sender` does NOT mean "who wrote this". In a one-to-one chat it carries
    the OTHER party's handle on every row, incoming and outgoing alike --
    confirmed on chat 104, where an outgoing row and an incoming row report the
    identical sender. Reading it straight labelled the owner's own words with the
    name of the person they were texting, which is the worst possible way to
    misread a thread you are about to reply to. `is_from_me` is the only field
    that answers the question.
    """
    return [
        {
            "id": row.get("id"),
            "created_at": row.get("created_at"),
            "is_from_me": row.get("is_from_me"),
            "sender": (
                OWNER_LABEL if row.get("is_from_me") is True
                else (row.get("sender_name") or row.get("sender"))
            ),
            "text": row.get("text"),
        }
        for row in rows[:count]
    ]



# ---- P0 (2026-10-30): full read, outbound gate, CLI logging ---------------------------------
# The shared comms modules are found from THIS file (see core_dirs), not from the environment.
GATE_DENY_EXIT = 7
FULL_MAX_LIMIT = 1000


class GateRefused(ImsgError):
    """The outbound gate refused in enforce mode; nothing was sent."""


def comms_module(name: str) -> Any:
    import importlib.util  # noqa: PLC0415
    path = comms_dir() / f"{name}.py"
    have = sys.modules.get(name)
    if have is not None and getattr(have, "__file__", None) == str(path):
        return have
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - callers decide what a missing module means
        sys.modules.pop(name, None)
        return None


def gate_mode_fallback() -> str:
    value = os.environ.get("GATE_MODE")
    if value is None:
        try:
            value = (comms_dir() / "gate_mode").read_text()
        except FileNotFoundError:
            value = "report"
        except OSError:
            value = "enforce"
    value = value.strip().lower()
    return value if value in ("report", "enforce") else "enforce"


def gate_context(routing: dict[str, Any], parts: list[dict[str, Any]], override: str | None,
                 *, dry_run: bool, source: str) -> Any:
    """Evaluate the outbound gate on the exact parts. --override-check is the owner's yes to the
    exact text here, so it waives style AND money (the one documented mapping, PLAN.md §3.4)."""
    G = comms_module("outbound_gate")
    if G is None:
        if gate_mode_fallback() == "enforce":
            raise GateRefused("NOT SENT: outbound gate (enforce): the gate module could not load.")
        sys.stderr.write("outbound gate: module could not load; report mode, continuing\n")
        return None
    reason = (override or "").strip()
    approvals = G.Approvals.from_env(money_ok=len(reason) >= 4, override_reason=reason)
    texts = [p["text"] for p in parts if p.get("kind") == "text"]
    files = [p for p in parts if p.get("kind") == "file"]
    readable, unread = [], []
    for part in files:
        content = attachment_text_for_gate(part)
        if content is None:
            unread.append(str(part.get("name")))
        else:
            readable.append((str(part.get("name")), content))
    recipients = [str(v) for v in routing.get("participants") or []]
    if routing.get("handle"):
        recipients.append(str(routing["handle"]))
    target = str(routing.get("id") or routing.get("handle") or "")
    payload = G.OutboundPayload("imessage", target=target, texts=texts,
                                recipients=[r for r in recipients if r],
                                filenames=[str(p.get("name")) for p in files],
                                attachment_texts=readable, unread_attachments=unread,
                                attachment_hashes=[G.file_sha(str(p["path"])) or "unreadable" for p in files])
    verdict = G.evaluate(payload, approvals, dry_run=dry_run, source=source)
    if not verdict.allow:
        raise GateRefused(verdict.deny_line())
    return (G, payload, approvals)


def gate_begin(ctx: Any) -> str | None:
    if ctx is None:
        return None
    G, payload, approvals = ctx
    try:
        return G.begin(payload, approvals)
    except G.GateDenied as error:
        raise GateRefused(str(error)) from error


def gate_finish(ctx: Any, op: str | None, status: str, receipts: list[Any]) -> None:
    if ctx is not None and op:
        ctx[0].finish(op, status, [str(r) for r in receipts if r is not None])


def log_sent_parts(routing: dict[str, Any], chat_id: Any, parts: list[dict[str, Any]],
                   sent: list[dict[str, Any]]) -> None:
    """One archive row per delivered part, keyed by its message id. Never raises."""
    C = comms_module("commslib")
    if C is None:
        return
    target = str(chat_id if chat_id is not None else routing.get("handle") or "")
    for item in sent:
        index = int(item.get("bubble") or 0) - 1
        part = parts[index] if 0 <= index < len(parts) else {}
        is_text = part.get("kind") == "text"
        C.log_event("imessage", "send", target, text=part.get("text") if is_text else None,
                    message_id=item.get("message_id"), verb="send",
                    files=None if is_text else [str(part.get("name"))],
                    command="imessage send --chat-id %s" % target)


def history_full(args: argparse.Namespace, detail: dict[str, Any]) -> dict[str, Any]:
    """Lossless read: reactions folded onto their message, reply parents filled, attachments
    always listed, -n counted in real messages. msgschema/1."""
    S = comms_module("msgschema")
    if S is None:
        raise ImsgError(f"--full needs msgschema.py in {comms_dir()}")
    n = args.limit
    before_row = getattr(args, "before_row", None)
    if before_row is not None and (before_row < 1 or not args.until):
        raise ImsgError("--before-row requires a positive row ID and its paired --until boundary")
    limit = min(FULL_MAX_LIMIT, max(n * 2, n + 20))
    while True:
        rows = history_rows(args.chat_id, limit, attachments=True, since=args.since,
                            until=args.until, participants=args.participant)
        eligible = [r for r in rows if before_row is None or int(r["id"]) < before_row]
        real = [r for r in eligible if not r.get("is_reaction")]
        if len(real) >= n or len(rows) < limit or limit >= FULL_MAX_LIMIT:
            break
        limit = min(FULL_MAX_LIMIT, limit * 2)
    end = len(rows) < limit                      # history ran out before the asked limit
    selected = real[:n]                          # rows arrive newest first
    by_guid = {r.get("guid"): r for r in rows if r.get("guid")}
    wanted = {r.get("guid") for r in selected}
    active: dict[str, dict[tuple[str, str], str]] = {}
    orphans = 0
    for r in sorted((r for r in rows if r.get("is_reaction")),
                    key=lambda r: parse_message_epoch(r.get("created_at")) or 0):
        target = r.get("reacted_to_guid") or r.get("associated_message_guid")
        if target not in wanted:
            orphans += 1
            continue
        who = OWNER_LABEL if r.get("is_from_me") is True else str(r.get("sender_name") or r.get("sender") or "?")
        key = str(r.get("reaction_emoji") or r.get("reaction_type") or "?")
        slot = active.setdefault(target, {})
        if r.get("is_reaction_add") is False:
            slot.pop((who, key), None)
        else:
            slot[(who, key)] = key
    messages = []
    for r in reversed(selected):
        guid = r.get("guid")
        raw_text = r.get("text") or ""
        text = raw_text.replace("\ufffc", "")
        msg = S.Message(id=str(r.get("id")), chat_id=str(args.chat_id), channel="imessage",
                        sender=OWNER_LABEL if r.get("is_from_me") is True
                        else str(r.get("sender_name") or r.get("sender") or "?"),
                        sender_id=None if r.get("is_from_me") is True else r.get("sender"),
                        is_from_me=r.get("is_from_me") is True,
                        ts=parse_message_epoch(r.get("created_at")), text=text)
        if text != raw_text:
            msg.extra["attachment_placeholders"] = raw_text.count("\ufffc")
        msg.extra["guid"] = guid
        for a in r.get("attachments") or []:
            path = a.get("original_path") or a.get("filename")
            local = os.path.expanduser(path) if isinstance(path, str) else None
            mime = a.get("mime_type")
            msg.attachments.append(S.Attachment(
                id=str(a.get("filename") or a.get("transfer_name") or ""),
                name=a.get("transfer_name") or (os.path.basename(path) if isinstance(path, str) else None),
                mime=mime, size=a.get("total_bytes"),
                local_path=None if a.get("missing") else local,
                transcript_status="not_read_here" if str(mime or "").startswith("audio/") else "none"))
        parent = r.get("reply_to_guid") or r.get("thread_originator_guid")
        if parent:
            p = by_guid.get(parent)
            msg.reply_to = S.ReplyRef(id=parent, text=None if p is None else (p.get("text") or "").replace("\ufffc", ""),
                                      incomplete=p is None)
        for (who, key) in sorted(active.get(guid, {})):
            msg.reactions.append(S.Reaction(key=key, sender=who))
        for extra_key in ("edited_message", "date_edited", "date_retracted", "service", "destination_caller_id"):
            if r.get(extra_key) not in (None, "", False):
                msg.extra[extra_key] = r.get(extra_key)
        messages.append(msg)
    truncated = len(selected) < n and not end
    # Include the timestamp boundary and exclude by stable row ID. Exclusive --until alone
    # loses messages that share the last returned timestamp. Reaction-only pages must advance
    # using their last raw row, rather than publishing no continuation.
    boundary = selected[-1] if selected else (eligible[-1] if eligible else None)
    more = None
    if boundary and not (end and len(real) <= n):
        instant = datetime.fromisoformat(boundary["created_at"].replace("Z", "+00:00"))
        inclusive = (instant + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z")
        more = f"--until {inclusive} --before-row {int(boundary['id'])}"
    chat = dict(display_snapshot(detail), id=detail["id"], is_group=detail["is_group"])
    window = history_window(detail, since=args.since, until=args.until, participants=args.participant)
    return S.envelope("imessage", chat, messages, complete=not truncated, truncated=truncated,
                      more=more, requested=n, orphan_reactions=orphans, window=window,
                      error="timestamp peer group exceeds upstream cap; cannot advance safely"
                            if truncated and not more else None,
                      more_note="Use both flags together; the row ID preserves same-time peers."
                                if more else None)


def reject_tool_markup(text: str) -> None:
    # File-based drafts do not pass through Claude's shell-command text guard.
    # Enforce its marker check on the actual payload for both assistants.
    markers = (
        "</invoke>", "</parameter>", "</function_calls>", "<function_calls>",
        "<invoke name=", "<parameter name=", "antml:",
    )
    if any(marker in text for marker in markers):
        raise ImsgError("Message text contains tool-call markup; repair the draft before sending")


def require_bubbles(texts: Any) -> list[str]:
    """Validate the whole bubble sequence one approval will cover.

    Every check a single bubble used to get individually now has to run over the
    sequence, because after this change nobody sees the bubbles again between the
    typed phrase and the send.
    """
    if not isinstance(texts, list) or not texts:
        raise ImsgError("A preview needs at least one message bubble")
    if len(texts) > MAX_PREVIEW_BUBBLES:
        raise ImsgError(
            f"A preview carries at most {MAX_PREVIEW_BUBBLES} bubbles; "
            f"got {len(texts)}. Split the message across separate previews."
        )
    bubbles: list[str] = []
    for index, text in enumerate(texts, start=1):
        if not isinstance(text, str) or not text.strip():
            raise ImsgError(f"Bubble {index} is empty; every bubble must carry text")
        reject_tool_markup(text)
        # `--file /path/to/key` is refused by the attachment guard, but
        # `--text "$(cat key)"` used to walk straight through: measured, the
        # draft checker calls a full OPENSSH private key "Clean". Whatever the
        # transport, a key block is never something the owner is texting somebody.
        if "-----BEGIN" in text and "PRIVATE KEY-----" in text:
            raise ImsgError(
                f"Bubble {index} contains a private key block; nothing was sent"
            )
        bubbles.append(text)
    total = sum(len(text) for text in bubbles)
    if total > MAX_TEXT_CHARS:
        raise ImsgError(
            f"The message totals {total} characters across {len(bubbles)} bubbles, "
            f"over the {MAX_TEXT_CHARS}-character safety limit"
        )
    return bubbles


def approval_summary(payload: dict[str, Any]) -> str:
    """One line naming what the single typed phrase is about to authorize.

    The phrase itself is only a preview id, so without this the dialog cannot
    tell a one-bubble send from a five-bubble one -- and that difference is
    exactly what this change introduced.
    """
    display = payload.get("display") or {}
    name = display.get("name") or ", ".join(
        str(value) for value in display.get("participants", [])
    ) or "this chat"
    parts = payload.get("parts") or parts_from_texts(payload.get("texts") or [])
    bubbles = sum(1 for part in parts if part.get("kind") == "text")
    files = [part for part in parts if part.get("kind") == "file"]
    pieces = []
    if bubbles:
        pieces.append(f"{bubbles} bubble{'s' if bubbles != 1 else ''}")
    if files:
        # Name the files. "1 file" tells the approver nothing about what is
        # leaving the machine, which is the whole question an attachment asks.
        pieces.append(
            f"{len(files)} file{'s' if len(files) != 1 else ''} "
            f"({', '.join(str(part.get('name')) for part in files)})"
        )
    what = " and ".join(pieces) or "nothing"
    if len(parts) == 1:
        return f"{what} to {name}."
    return (
        f"{what} to {name}, sent in order. "
        f"This one phrase authorizes all {len(parts)} parts."
    )


def service_for_chat(detail: dict[str, Any]) -> str:
    service = str(detail.get("service", "")).casefold()
    if service == "imessage":
        return "imessage"
    if service == "sms":
        return "sms"
    # RCS is a real service on this Mac (29 direct chats) but not one the
    # runtime accepts as a --service value, so hand it back to the runtime.
    return "auto"


def preview_part_labels(parts: list[dict[str, Any]]) -> list[str]:
    """What each part shows as. A file shows its NAME, never its contents."""
    return [part["name"] if part["kind"] == "file" else part["text"] for part in parts]


def parts_from_texts(texts: list[str]) -> list[dict[str, Any]]:
    return [{"kind": "text", "text": text} for text in texts]


def require_parts(parts: Any) -> list[dict[str, Any]]:
    """Validate the whole sequence one send will deliver.

    Text parts get exactly the checks bubbles always got. File parts get every
    rule that is PURE -- absolute path, credential-path refusal, name matching
    path -- because those cost nothing to re-run on a record that recomputed its
    own digest. The filesystem checks deliberately do not run here: this function
    is also on the read path (`show-preview`, `discard-preview`), and a preview
    whose attachment was since deleted must still be inspectable and discardable.
    commit_preview re-stats every file immediately before the first part goes out.
    """
    if not isinstance(parts, list) or not parts:
        raise ImsgError("A message needs at least one part")
    if len(parts) > MAX_PREVIEW_BUBBLES:
        raise ImsgError(
            f"A message carries at most {MAX_PREVIEW_BUBBLES} parts; got {len(parts)}."
        )
    texts = [p.get("text") for p in parts if isinstance(p, dict) and p.get("kind") == "text"]
    if texts:
        require_bubbles(texts)
    for index, part in enumerate(parts, start=1):
        if not isinstance(part, dict) or part.get("kind") not in {"text", "file"}:
            raise ImsgError(f"Part {index} is not a text or file part")
        if part["kind"] == "file":
            if not isinstance(part.get("path"), str) or not part["path"].strip():
                raise ImsgError(f"Part {index} is a file part with no path")
            if not isinstance(part.get("name"), str) or not part["name"].strip():
                raise ImsgError(f"Part {index} is a file part with no name")
            # A relative path would be resolved against whatever directory the
            # process happens to be in at send time, which is not a decision the
            # record gets to defer.
            if not part["path"].startswith("/"):
                raise ImsgError(f"Part {index} has a relative attachment path")
            # Cheap and pure, so it runs again here. Without it a hand-edited
            # preview could point a file part at an SSH key, recompute its own
            # digest, and walk straight past the guard that ran at collection.
            reject_sensitive_path(part["path"])
            if Path(part["path"]).name != part["name"]:
                raise ImsgError(
                    f"Part {index} names a different file than its path; the record "
                    "has been edited"
                )
    return parts


def create_preview(
    chat_id: int,
    texts: list[str] | None = None,
    *,
    parts: list[dict[str, Any]] | None = None,
    service: str | None = None,
    headless: bool = False,
) -> dict[str, Any]:
    if parts is None:
        parts = parts_from_texts(require_bubbles(texts))
    parts = require_parts(parts)
    bubbles = [part["text"] for part in parts if part["kind"] == "text"]
    # The dialog path still refuses anything but a direct iMessage chat, because
    # a typed phrase is a poor place to notice you are texting a group. The
    # headless path says so explicitly rather than inferring it from whether a
    # service happened to be passed -- an inference that would have silently
    # widened the dialog path the first time someone passed a service to it.
    detail = sendable_chat(chat_id) if headless else safe_chat(chat_id)
    rows = history_rows(chat_id, 50)
    now = int(time.time())
    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now,
        "expires_at": now + PREVIEW_TTL_SECONDS,
        "nonce": os.urandom(8).hex(),
        "routing": routing_snapshot(detail),
        "display": display_snapshot(detail),
        "texts": bubbles,
        "parts": parts,
        "service": service or service_for_chat(detail),
        "baseline_rowid": max_message_id(rows),
    }
    digest = payload_digest(payload)
    preview_id = digest[:24]
    document = {"preview_id": preview_id, "digest": digest, "payload": payload}
    write_private_json_exclusive(preview_path(preview_id), document)
    return {
        "preview_id": preview_id,
        "target": payload["display"],
        "chat_id": chat_id,
        "texts": bubbles,
        "parts": preview_part_labels(parts),
        "bubbles": len(parts),
        "created_at": iso_time(now),
        "expires_at": iso_time(payload["expires_at"]),
        "recent_context": recent_context(rows),
        "approval_phrase": approval_phrase(preview_id),
        "approval_covers": approval_summary(payload),
        "nothing_sent": True,
    }


def create_handle_preview(
    handle: str,
    parts: list[dict[str, Any]],
    *,
    service: str,
    region: str,
) -> dict[str, Any]:
    """Preview a send to somebody with no existing chat.

    Its own function rather than a branch inside create_preview: almost nothing
    is shared. There is no chat to look up, no routing to snapshot, no history
    to baseline against and no recent context to show. Pretending otherwise
    would mean four `if chat_id is None` branches through code whose whole job
    is to be hard to get wrong.
    """
    parts = require_parts(parts)
    if service not in SEND_SERVICES:
        raise ImsgError(f"Unsupported service {service!r} for a new-recipient send")
    if not REGION_RE.fullmatch(region):
        raise ImsgError("--region must be a two-letter country code such as US")
    bubbles = [part["text"] for part in parts if part["kind"] == "text"]
    now = int(time.time())
    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now,
        "expires_at": now + PREVIEW_TTL_SECONDS,
        "nonce": os.urandom(8).hex(),
        "routing": handle_routing_snapshot(handle, service, region),
        "display": {"name": None, "participants": [handle], "service": service},
        "texts": bubbles,
        "parts": parts,
        "service": service,
        # No chat exists, so there is no rowid to baseline against. The first
        # send creates the chat, and its verification leans on the rowid and
        # GUID the runtime hands back instead.
        "baseline_rowid": 0,
    }
    digest = payload_digest(payload)
    preview_id = digest[:24]
    write_private_json_exclusive(
        preview_path(preview_id),
        {"preview_id": preview_id, "digest": digest, "payload": payload},
    )
    return {
        "preview_id": preview_id,
        "target": payload["display"],
        "chat_id": None,
        "to": handle,
        "texts": bubbles,
        "parts": preview_part_labels(parts),
        "bubbles": len(parts),
        "created_at": iso_time(now),
        "expires_at": iso_time(payload["expires_at"]),
        "recent_context": [],
        "new_recipient": True,
        "approval_phrase": approval_phrase(preview_id),
        "approval_covers": approval_summary(payload),
        "nothing_sent": True,
    }


def validate_preview_payload(payload: dict[str, Any]) -> None:
    created_at = payload.get("created_at")
    expires_at = payload.get("expires_at")
    baseline_rowid = payload.get("baseline_rowid")
    nonce = payload.get("nonce")
    texts = payload.get("texts")
    routing = payload.get("routing")
    display = payload.get("display")
    integers_are_valid = all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (created_at, expires_at, baseline_rowid)
    )
    if not integers_are_valid or baseline_rowid < 0:
        raise ImsgError("Malformed preview timestamps or baseline")
    if expires_at - created_at != PREVIEW_TTL_SECONDS:
        raise ImsgError("Malformed preview expiry window")
    if created_at > int(time.time()) + 60:
        raise ImsgError("Preview creation time is unexpectedly in the future")
    if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
        raise ImsgError("Malformed preview nonce")
    # Re-run the full sequence check on the stored payload, not just a type
    # check: the bubble cap, the total-character cap and the markup refusal all
    # have to hold at load time or a hand-edited preview walks past them.
    stored_parts = payload.get("parts")
    if stored_parts is None:
        require_bubbles(texts)
    else:
        require_parts(stored_parts)
        # Two representations of the same message can be edited apart. Caught by
        # the suite the moment `parts` was added: a hand-edited preview put tool
        # markup in `texts` while `parts` stayed clean, and validation read the
        # clean one while the send read the poisoned one. Whenever both exist
        # they must agree, or neither can be trusted.
        if texts is not None:
            expected = [part["text"] for part in stored_parts if part["kind"] == "text"]
            if texts != expected:
                raise ImsgError(
                    "Preview text and parts disagree; the record has been edited"
                )
        # The caps and the markup refusal have to hold on the text either way.
        if [p for p in stored_parts if p["kind"] == "text"]:
            require_bubbles([p["text"] for p in stored_parts if p["kind"] == "text"])
    if not isinstance(routing, dict) or not isinstance(display, dict):
        raise ImsgError("Malformed preview target")
    if display.get("name") is not None and not isinstance(display["name"], str):
        raise ImsgError("Malformed preview display name")
    # The service is handed straight to the runtime at send time, so a tampered
    # one could downgrade an iMessage to an unencrypted SMS without changing a
    # single visible character of the message.
    if payload.get("service") not in SEND_SERVICES:
        raise ImsgError("Malformed preview service")
    kind = routing.get("kind")
    if kind == "handle":
        handle = routing.get("handle")
        if not isinstance(handle, str) or not HANDLE_RE.fullmatch(handle.strip()):
            raise ImsgError("Malformed preview recipient handle")
        if routing.get("service") not in SEND_SERVICES:
            raise ImsgError("Malformed preview service for a new-recipient send")
        region = routing.get("region")
        if not isinstance(region, str) or not REGION_RE.fullmatch(region):
            raise ImsgError("Malformed preview region")
        if "id" in routing or "guid" in routing:
            raise ImsgError(
                "A new-recipient preview must not carry chat routing; refusing it "
                "rather than guessing which target is real"
            )
        if (
            display.get("participants") != [handle]
            or display.get("service") != routing["service"]
        ):
            raise ImsgError("Preview display target does not match its routing")
        return
    if kind != "chat":
        raise ImsgError("Preview routing does not name a supported target kind")
    chat_id = routing.get("id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id <= 0:
        raise ImsgError("Malformed preview routing ID")
    require_sendable_chat(routing)
    if (
        display.get("participants") != routing["participants"]
        or display.get("service") != routing["service"]
    ):
        raise ImsgError("Preview display target does not match its routing")


def preview_expired(payload: dict[str, Any]) -> bool:
    return payload["expires_at"] <= int(time.time())


def load_preview(
    preview_id: str,
    *,
    status: str = "pending",
    require_unexpired: bool = True,
) -> dict[str, Any]:
    document = read_private_json(preview_path(preview_id, status))
    payload = document.get("payload")
    digest = document.get("digest")
    if document.get("preview_id") != preview_id or not isinstance(payload, dict) or not isinstance(digest, str):
        raise ImsgError("Malformed preview document")
    recomputed = payload_digest(payload)
    if digest != recomputed or preview_id != recomputed[:24]:
        raise ImsgError("Preview integrity check failed; prepare a new preview")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ImsgError("Unsupported preview schema version")
    validate_preview_payload(payload)
    if require_unexpired and preview_expired(payload):
        raise ImsgError("Preview expired; prepare a new preview and approve its new phrase")
    return document


def show_preview(preview_id: str) -> dict[str, Any]:
    document = load_preview(preview_id)
    payload = document["payload"]
    return {
        "preview_id": preview_id,
        "target": payload["display"],
        "chat_id": payload["routing"].get("id"),
        "to": payload["routing"].get("handle"),
        "texts": payload["texts"],
        "parts": preview_part_labels(
            payload.get("parts") or parts_from_texts(payload["texts"])
        ),
        "bubbles": len(payload.get("parts") or payload["texts"]),
        "created_at": iso_time(payload["created_at"]),
        "expires_at": iso_time(payload["expires_at"]),
        "approval_phrase": approval_phrase(preview_id),
        "approval_covers": approval_summary(payload),
        "approval_ui": "macOS dialog required before send",
        "nothing_sent": True,
    }


def move_preview(preview_id: str, source_status: str, destination_status: str) -> Path:
    source = preview_path(preview_id, source_status)
    destination = preview_path(preview_id, destination_status)
    if destination.exists():
        raise ImsgError(f"Preview already has status {destination_status}")
    try:
        os.replace(source, destination)
    except OSError as error:
        raise ImsgError(f"Preview is no longer claimable from status {source_status}") from error
    fsync_directory(source.parent)
    fsync_directory(destination.parent)
    return destination


def transition_from_sending(preview_id: str, destination_status: str) -> None:
    try:
        move_preview(preview_id, "sending", destination_status)
    except ImsgError:
        # The pending preview was already consumed. A missing status transition
        # must never make it retryable again.
        return


def current_routing_matches(payload: dict[str, Any]) -> bool:
    routing = payload.get("routing")
    if not isinstance(routing, dict):
        return False
    if routing.get("kind") == "handle":
        # The preview promised somebody with NO chat. If one exists now, it was
        # created inside the last ten minutes, and the send would land in a
        # thread the preview never described. Fail closed and prepare again.
        return not direct_chats_for_handle(routing["handle"])
    chat_id = routing.get("id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return False
    # sendable_chat, not safe_chat: groups and SMS chats are legitimate targets
    # now, and the real guard here is the snapshot comparison below, which
    # refuses any drift in service, participants or GUID since the preview.
    current = routing_snapshot(sendable_chat(chat_id))
    return current == routing


def parse_message_epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def run_imsg_send_rpc(
    chat_id: int | None,
    text: str | None,
    request_id: str,
    *,
    expires_at: int,
    timeout: int = 20,
    file: str | None = None,
    to: str | None = None,
    service: str = "imessage",
    region: str = "US",
) -> dict[str, Any]:
    """Dispatch one part over the runtime's JSON-RPC `send` method.

    Param names are the runtime's, confirmed empirically 2026-09-09 against a
    nonexistent chat: unknown keys are rejected BY NAME before routing, so the
    accepted set is exactly chat_id / chat_identifier / chat_guid / to / text /
    file / service / region / allow_sms_fallback / reply_to. `attachment`,
    `path`, `file_path` and `handle` are all rejected -- the key is `file`, and
    the handle key is `to`, which is mutually exclusive with a chat selector.
    """
    binary = validate_upstream_binary()
    params: dict[str, Any] = {"service": service}
    if to is not None:
        params["to"] = to
        params["region"] = region
    else:
        params["chat_id"] = chat_id
    if text is not None:
        params["text"] = text
    if file is not None:
        params["file"] = file
    request = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "send",
        "params": params,
    }
    rpc_input = json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
    # Binary hashing/version validation may outlast a nearly expired preview.
    if expires_at <= int(time.time()):
        raise RpcSendError("Preview expired before RPC dispatch", "not_started")
    try:
        completed = subprocess.run(
            [str(binary), "rpc"],
            input=rpc_input,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RpcSendError("imsg RPC send timed out", "still_in_flight") from error
    if completed.returncode != 0:
        raise RpcSendError(f"imsg RPC exited {completed.returncode}", "unknown")

    responses = parse_json_lines(completed.stdout, "rpc send")
    response = next((row for row in responses if row.get("id") == request_id), None)
    if response is None:
        raise RpcSendError("imsg RPC returned no matching response", "unknown")
    error_value = response.get("error")
    if isinstance(error_value, dict):
        data = error_value.get("data")
        code = error_value.get("code")
        if isinstance(data, dict):
            disposition = data.get("disposition") or "unknown"
            detail = data.get("detail") or ""
        else:
            # -32700/-32601/-32602 all carry a STRING here and all fail BEFORE
            # the transport is touched. Reporting them as "unknown" made a
            # misspelled parameter indistinguishable from a send that might
            # already have landed, which is the one thing that must never blur.
            disposition = "not_started"
            detail = str(data or "")
        raise RpcSendError(
            f"imsg RPC send failed (code {code}, disposition {disposition})"
            + (f": {bounded_error_text(detail)}" if detail else ""),
            str(disposition),
        )
    result = response.get("result")
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RpcSendError("imsg RPC send returned malformed success data", "unknown")
    return result


def attachment_row_text(caption: str | None, file_count: int) -> str:
    """What Messages actually stores as `text` for an attachment row.

    Measured over 531 attachment objects: a caption-less attachment is NOT an
    empty string. It carries one U+FFFC OBJECT REPLACEMENT CHARACTER per file,
    and a caption follows them as a suffix. `text` is never absent on any row,
    so this is a comparison, never a KeyError.
    """
    return "\ufffc" * max(file_count, 1) + (caption or "")


def find_verified_send(
    chat_id: int,
    text: str,
    baseline_rowid: int,
    send_started_at: float,
    upstream_result: dict[str, Any],
    *,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Confirm the exact row this send produced, or return None.

    Text and attachments do NOT get the same quality of proof, and it is worth
    knowing which one you have. Measured live 2026-09-09: a text send's RPC
    reply carries `id` and `guid`, so the row is identified outright. An
    attachment send's reply carries NEITHER -- only `ok`, `service` and
    `chat_guid`. So an attachment is identified by its U+FFFC row text, the
    rowid baseline, the time window and the transfer_name.

    Filename and byte count are corroboration ONLY. On this Mac `PNG image.png`
    occurs seven times and `hl.png` four times at an identical 418 bytes, so
    name+size alone would confirm the wrong row. Messages also re-encodes: a
    70-byte PNG sent today was stored as 163 bytes, which is why size is never
    compared and a size mismatch must not by itself fail a send.
    """
    expected_id = upstream_result.get("id")
    expected_guid = upstream_result.get("guid")
    expected_names = sorted(item["name"] for item in attachments or [])
    for attempt in range(3):
        # --attachments costs an extra join, so only pay for it when an
        # attachment name is part of what we are corroborating.
        rows = history_rows(chat_id, 50, attachments=bool(expected_names))
        now = time.time()
        matches: list[dict[str, Any]] = []
        for row in rows:
            created_epoch = parse_message_epoch(row.get("created_at"))
            if (
                row.get("chat_id") == chat_id
                and row.get("is_from_me") is True
                and row.get("text") == text
                and (
                    not expected_names
                    or sorted(
                        str(a.get("transfer_name") or "")
                        for a in (row.get("attachments") or [])
                    ) == expected_names
                )
                and isinstance(row.get("id"), int)
                and row["id"] > baseline_rowid
                and created_epoch is not None
                and send_started_at - 5 <= created_epoch <= now + 30
                and (not isinstance(expected_id, int) or row["id"] == expected_id)
                and (not isinstance(expected_guid, str) or row.get("guid") == expected_guid)
            ):
                matches.append(row)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ImsgError("Multiple outgoing rows match this send; verification is ambiguous")
        if attempt < 2:
            time.sleep(1)
    return None


def close_dialog_process(process: subprocess.Popen) -> None:
    """Stop only our session's dialog process group, with bounded reaping."""
    try:
        if process.poll() is None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass  # It exited between poll and signal; still reap it.
                try:
                    process.wait(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    if sig == signal.SIGKILL:
                        raise ImsgError("Native dialog could not be stopped; nothing was sent")
    except OSError as error:
        raise ImsgError("Native dialog cleanup failed; nothing was sent") from error
    finally:
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


def run_dialog_process(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Keep timeout, Ctrl-C and SIGTERM before approval fail-closed."""
    cancelled = False
    interruptible = False

    def interrupted(signum: int, frame: Any) -> None:
        nonlocal cancelled
        cancelled = True
        if interruptible:
            raise ImsgError("Native dialog was interrupted; nothing was sent")

    previous_handlers = {}
    process = None
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, interrupted)
        # Defer cancellation until Popen returns ownership of the child.
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        if cancelled:
            raise ImsgError("Native dialog was interrupted; nothing was sent")
        interruptible = True
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as error:
        raise ImsgError("Native dialog timed out; nothing was sent. Do not retry automatically.") from error
    except (OSError, UnicodeError) as error:
        raise ImsgError("Native dialog was unavailable; nothing was sent") from error
    finally:
        # Repeated signals must not interrupt TERM/wait/KILL/reap. A signal
        # during successful cleanup must also discard any submitted approval.
        interruptible = False
        try:
            if process is not None:
                close_dialog_process(process)
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
        if cancelled:
            raise ImsgError("Native dialog was interrupted; nothing was sent")


def typed_dialog(expected: str, *, diagnostic: bool = False, summary: str | None = None) -> str:
    """Request a foreground AppKit prompt; never substitute the expected text."""
    if platform.system() != "Darwin":
        raise ImsgError("The native dialog requires macOS; nothing was sent")
    seconds = UI_DIAGNOSTIC_SECONDS if diagnostic else APPROVAL_DIALOG_SECONDS
    title = "iMessage UI diagnostic" if diagnostic else "iMessage approval"
    button = "Check UI" if diagnostic else "Authorize"
    instruction = (
        "UI diagnostic only. This cannot send messages or authorize a send.\n\n"
        "Type this exact phrase to confirm you can see and use the dialog:"
        if diagnostic else "Type this exact phrase to authorize this iMessage send:"
    )
    # The phrase is only a preview id, so on its own the window cannot tell a
    # one-bubble send from a five-bubble one. Since one phrase now covers the
    # whole message, the count and the recipient have to be ON the window.
    if summary:
        instruction = f"{instruction}\n\n{summary}"
    # Fixed JXA source; all variable data is passed separately, never evaluated.
    # No Standard Additions or external application hosts are involved.
    script = r"""
ObjC.import('AppKit');
function run(argv) {
    var seconds = Number(argv[4]);
    if (argv.length !== 5 || (seconds !== 60 && seconds !== 300)) {
        throw new Error('Invalid dialog parameters');
    }
    var app = $.NSApplication.sharedApplication;
    if (!app.setActivationPolicy($.NSApplicationActivationPolicyRegular)) {
        throw new Error('Dialog activation policy unavailable');
    }
    var alert = $.NSAlert.alloc.init;
    alert.messageText = argv[1];
    alert.informativeText = argv[2] + '\n\n' + argv[0];
    alert.addButtonWithTitle(argv[3]);
    var cancel = alert.addButtonWithTitle('Cancel');
    cancel.keyEquivalent = '\x1b';
    var field = $.NSTextField.alloc.initWithFrame($.NSMakeRect(0, 0, 440, 24));
    field.stringValue = '';
    field.editable = true;
    field.selectable = true;
    alert.accessoryView = field;
    var window = alert.window;
    var timer = null;
    var timedOut = false;
    var deadline = Number($.NSProcessInfo.processInfo.systemUptime) + seconds;
    try {
        ObjC.registerSubclass({
            name: 'ApprovalDeadline',
            superclass: 'NSObject',
            methods: {
                'expire:': {
                    types: ['void', ['id']],
                    implementation: function (sender) {
                        timedOut = true;
                        app.stopModalWithCode(-1002);
                    }
                }
            }
        });
        var target = $.ApprovalDeadline.alloc.init;
        timer = $.NSTimer.timerWithTimeIntervalTargetSelectorUserInfoRepeats(
            seconds, target, 'expire:', $(), false
        );
        $.NSRunLoop.mainRunLoop.addTimerForMode(timer, $.NSModalPanelRunLoopMode);
        // JXA invokes zero-argument Objective-C methods by property access.
        alert.layout;
        window.title = argv[1];
        window.initialFirstResponder = field;
        // This process CANNOT win cooperative activation (macOS 14+): a helper
        // with no recent user event is refused focus, and all three calls report
        // it -- setActivationPolicy(Regular) succeeds while `app.active`,
        // activateIgnoringOtherApps and activateWithOptions all stay false.
        // Measured 2026-09-09 on macOS 26.6. So the window was drawn correctly
        // and then abandoned: ordinary level, single Space, centred on the
        // menu-bar display, and never key -- which meant that even when it WAS
        // on screen, the typed phrase went to whatever app owned the keyboard.
        // Three sessions read that as "no dialog appeared".
        //
        // The panel therefore has to reach the user WITHOUT activation:
        //   NonactivatingPanel  -- may become key while the app stays inactive
        //   NSStatusWindowLevel -- rises above ordinary windows
        //   CanJoinAllSpaces + FullScreenAuxiliary -- shows over a full-screen
        //                          app, on 13 Spaces across 2 displays here
        //   pointer's screen    -- where the user is, not NSScreen.mainScreen
        // Verified A/B/A: with the style mask, key=true and the first responder
        // is the text field; without it, key=false. Nothing about the SAFETY
        // contract moves -- the phrase must still be typed by hand.
        window.styleMask = Number(window.styleMask) | $.NSWindowStyleMaskNonactivatingPanel;
        window.level = $.NSStatusWindowLevel;
        window.collectionBehavior =
            $.NSWindowCollectionBehaviorCanJoinAllSpaces |
            $.NSWindowCollectionBehaviorFullScreenAuxiliary;
        // NOT named `target`: that identifier already holds the NSTimer's
        // target in this same function scope, and `var` is function-scoped.
        var pointer = $.NSEvent.mouseLocation;
        var screens = $.NSScreen.screens;
        var pointerScreen = $.NSScreen.mainScreen;
        for (var i = 0; i < Number(screens.count); i++) {
            var candidate = screens.objectAtIndex(i);
            var bounds = candidate.frame;
            if (pointer.x >= bounds.origin.x &&
                pointer.x < bounds.origin.x + bounds.size.width &&
                pointer.y >= bounds.origin.y &&
                pointer.y < bounds.origin.y + bounds.size.height) {
                pointerScreen = candidate;
                break;
            }
        }
        var area = pointerScreen.visibleFrame;
        var own = window.frame;
        window.setFrameOrigin($.NSMakePoint(
            area.origin.x + (area.size.width - own.size.width) / 2,
            area.origin.y + (area.size.height - own.size.height) * 0.6
        ));
        if (app.respondsToSelector('activate')) {
            app.activate;
        } else {
            app.activateIgnoringOtherApps(true);
        }
        window.makeKeyAndOrderFront($());
        // Activation is refused above, so ordering in front must not depend on it.
        window.orderFrontRegardless;
        if (!window.makeFirstResponder(field)) {
            throw new Error('Dialog input focus unavailable');
        }
        // JXA can bridge NSInteger values as strings; compare numeric codes.
        var response = Number(alert.runModal);
        if (timedOut || Number($.NSProcessInfo.processInfo.systemUptime) >= deadline) {
            return JSON.stringify({status: 'timed_out'});
        }
        if (response === Number($.NSAlertSecondButtonReturn)) {
            return JSON.stringify({status: 'cancelled'});
        }
        if (response !== Number($.NSAlertFirstButtonReturn)) {
            throw new Error('Unexpected dialog response');
        }
        field.validateEditing;
        var text = ObjC.unwrap(field.stringValue);
        if (typeof text !== 'string') {
            throw new Error('Invalid dialog input');
        }
        return JSON.stringify({status: 'submitted', text: text});
    } finally {
        if (timer) {
            timer.invalidate;
        }
        window.orderOut($());
    }
}
"""
    completed = run_dialog_process(
        ["/usr/bin/osascript", "-l", "JavaScript", "-e", script,
         expected, title, instruction, button, str(seconds)],
        timeout=seconds + 10,
    )
    if completed.returncode != 0:
        raise ImsgError("Native dialog was unavailable; nothing was sent")

    def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for key, value in pairs:
            if key in fields:
                raise ValueError("Duplicate dialog result field")
            fields[key] = value
        return fields

    try:
        result = json.loads(completed.stdout, object_pairs_hook=unique_fields)
    except (ValueError, TypeError) as error:
        raise ImsgError("Native dialog returned an invalid result; nothing was sent") from error
    if result == {"status": "timed_out"}:
        # Deliberately NOT the same sentence as the subprocess timeout in
        # run_dialog_process. This one means the window was up for its full
        # window and nobody typed; that one means the child produced no output
        # at all. They are opposite diagnoses, and three sessions guessed
        # because one string covered both.
        raise ImsgError(
            "Native dialog was shown but no phrase was typed in time; nothing was sent."
        )
    if result == {"status": "cancelled"}:
        raise ImsgError("Native dialog was cancelled; nothing was sent")
    if (
        not isinstance(result, dict)
        or set(result) != {"status", "text"}
        or result["status"] != "submitted"
        or not isinstance(result["text"], str)
    ):
        raise ImsgError("Native dialog returned an invalid result; nothing was sent")
    return result["text"]


def prompt_for_approval(preview_id: str, summary: str | None = None) -> str:
    return typed_dialog(approval_phrase(preview_id), summary=summary)


def check_approval_ui() -> dict[str, Any]:
    """UI-only diagnostic: no backend access, preview state, or send path."""
    expected = f"CHECK UI {os.urandom(4).hex()}"
    if typed_dialog(expected, diagnostic=True) != expected:
        raise ImsgError("UI diagnostic phrase did not match; nothing was sent")
    return {
        "approval_ui": "typed diagnostic confirmed",
        "nothing_sent": True,
        "send_automation": "not tested",
        "messages_accessed": False,
    }


def end_send_sequence(
    preview_id: str,
    parts: list[dict[str, Any]],
    sent: list[dict[str, Any]],
    failed_index: int,
    disposition: str,
    reason: str,
) -> SendSequenceError:
    """Lock the preview and say exactly how far the message got.

    A mid-sequence failure is an outcome per-bubble approval could not produce:
    part of the message is on the recipient's phone and the rest never will be.
    Reporting that as a plain failure would be a lie in the dangerous direction,
    so a delivered bubble forces status "partial" whatever the disposition was.
    The preview is terminal either way, so no retry can double-send what landed.
    """
    status = disposition if not sent else "partial"
    transition_from_sending(preview_id, status)
    landed = [row["bubble"] for row in sent]
    report = {
        "preview_id": preview_id,
        "status": status,
        "bubbles": len(parts),
        "parts": preview_part_labels(parts),
        "sent": sent,
        "failed_bubble": failed_index + 1,
        "failed_kind": parts[failed_index]["kind"] if failed_index < len(parts) else None,
        "never_attempted": list(range(failed_index + 2, len(parts) + 1)),
        "reason": reason,
        "retry": "never; this preview is consumed",
    }
    if not sent and status == "failed":
        message = (
            "Send failed before anything left the machine; nothing was sent. "
            f"The record is locked and must not be retried: {reason}"
        )
    elif not sent:
        # "uncertain" means we cannot tell, which is the OPPOSITE of "nothing was
        # sent" -- and saying the wrong one here is how a 60 MB attachment that
        # timed out at 20s but finished uploading gets sent to somebody twice.
        message = (
            f"UNCERTAIN SEND ({status}): the message may already have reached the "
            "chat, and it may not. Do NOT resend and do not report it as failed. "
            f"Read the chat and tell the owner what you find. Reason: {reason}"
        )
    else:
        message = (
            f"PARTIAL SEND: part(s) {landed} of {len(parts)} reached the chat and "
            f"part {failed_index + 1} did not. The recipient is holding an "
            f"incomplete message. Do not retry this preview; read the chat and "
            f"decide what to send next. Reason: {reason}"
        )
    return SendSequenceError(message, report)


def chat_for_new_recipient(handle: str) -> int:
    """Find the chat the runtime just created for a first-time recipient.

    The pre-send scan found ZERO chats matching this handle, so exactly one
    should exist now. Anything else means the ground moved mid-send and the
    outgoing row cannot be attributed with confidence -- which is a verification
    failure to report honestly, never something to resolve by picking one.
    """
    matches = direct_chats_for_handle(handle)
    if len(matches) != 1:
        raise ImsgError(
            f"The message was claimed sent to {handle}, but {len(matches)} chats now "
            "match that handle, so the outgoing row cannot be verified. Read the "
            "conversation in Messages before deciding anything; do not resend."
        )
    chat_id = matches[0].get("id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        raise ImsgError("The new chat for this recipient has no usable chat ID")
    return chat_id


def commit_preview(preview_id: str, approval: str, override: str | None = None) -> dict[str, Any]:
    expected_approval = approval_phrase(preview_id)
    if approval != expected_approval:
        raise ImsgError(f"Approval must exactly match: {expected_approval}")
    document = load_preview(preview_id)
    payload = document["payload"]
    move_preview(preview_id, "pending", "sending")

    if preview_expired(payload):
        transition_from_sending(preview_id, "invalid")
        raise ImsgError("Preview expired before send; nothing was sent")
    try:
        routing_matches = current_routing_matches(payload)
    except ImsgError as error:
        transition_from_sending(preview_id, "invalid")
        raise ImsgError("Could not revalidate chat routing; nothing was sent") from error
    if not routing_matches:
        transition_from_sending(preview_id, "invalid")
        raise ImsgError("Chat routing changed after preview; preview invalidated and nothing was sent")
    if preview_expired(payload):
        transition_from_sending(preview_id, "invalid")
        raise ImsgError("Preview expired during final checks; nothing was sent")

    routing = payload["routing"]
    parts = require_parts(payload.get("parts") or parts_from_texts(payload["texts"]))
    service = payload["service"]
    region = routing.get("region", "US")
    baseline = payload["baseline_rowid"]
    chat_id = routing.get("id")
    # Set only for a first-ever message to somebody. It is cleared as soon as
    # the chat it creates has been found, so parts 2..n go by chat id like any
    # other send and get the same verification.
    pending_handle = routing.get("handle") if routing.get("kind") == "handle" else None
    # Re-stat every attachment BEFORE the first part goes out, not lazily as the
    # loop reaches it. A file that has vanished or been swapped since the record
    # was written should abort the whole message while nothing has been sent,
    # rather than half way through with two bubbles already on the phone.
    try:
        for part in parts:
            if part["kind"] == "file":
                validate_attachment(Path(part["path"]))
    except ImsgError:
        transition_from_sending(preview_id, "invalid")
        raise

    # Outbound gate (P0), here on the shared path so `send` AND `send-preview` of an older
    # preview are both covered, after routing revalidation, before any RPC.
    try:
        gate = gate_context(routing, parts, override, dry_run=False, source="imessage commit")
        gate_op = gate_begin(gate)
    except GateRefused:
        transition_from_sending(preview_id, "invalid")
        raise

    sent: list[dict[str, Any]] = []
    # The TTL decides whether the sequence may START. It deliberately stops
    # governing once part 1 is claimed -- see SEND_SEQUENCE_GRACE_SECONDS.
    deadline = payload["expires_at"]
    try:
        _send_parts(preview_id, parts, sent, chat_id, pending_handle, deadline, service,
                    region, baseline)
    except SendSequenceError as error:
        status = str(error.report.get("status", "uncertain"))
        gate_finish(gate, gate_op, "failed_before_send" if (status == "failed" and not sent)
                    else "uncertain", [item.get("message_id") for item in sent])
        log_sent_parts(routing, chat_id, parts, sent)
        raise
    except BaseException:
        gate_finish(gate, gate_op, "uncertain", [item.get("message_id") for item in sent])
        raise
    chat_id = sent[-1].get("chat_id", chat_id) if sent else chat_id
    gate_finish(gate, gate_op, "sent", [item.get("message_id") for item in sent])
    log_sent_parts(routing, chat_id, parts, sent)

    move_preview(preview_id, "sending", "sent")
    return {
        "preview_id": preview_id,
        "status": "sent_and_verified",
        "chat_id": chat_id,
        "bubbles": len(parts),
        "parts": preview_part_labels(parts),
        "sent": [{k: v for k, v in item.items() if k != "chat_id"} for item in sent],
    }


def _send_parts(preview_id: str, parts: list[dict[str, Any]], sent: list[dict[str, Any]],
                chat_id: Any, pending_handle: str | None, deadline: int, service: str,
                region: str, baseline: int) -> None:
    """The per-part send loop, moved out of commit_preview unchanged except that each sent
    item also carries `chat_id` (a first message to a new handle finds its chat mid-loop)."""
    for index, part in enumerate(parts):
        # Distinct RPC ids per part, or run_imsg_send_rpc matches part 2's
        # reply against part 1's request and reports the wrong outcome.
        request_id = preview_id if len(parts) == 1 else f"{preview_id}-{index + 1}"
        is_file = part["kind"] == "file"
        # An attachment row's own `text` is what verification compares against,
        # and it is NOT empty: Messages stores one U+FFFC per file.
        expected_text = attachment_row_text(None, 1) if is_file else part["text"]
        send_started_at = time.time()
        try:
            upstream_result = run_imsg_send_rpc(
                chat_id,
                None if is_file else part["text"],
                request_id,
                expires_at=deadline,
                file=part["path"] if is_file else None,
                to=pending_handle,
                service=service,
                region=region,
            )
            if index == 0:
                deadline = int(time.time()) + SEND_SEQUENCE_GRACE_SECONDS
            if pending_handle is not None:
                # Runs only AFTER the message is claimed sent, so a failure here
                # is a verification failure, never a send that did not happen.
                chat_id = chat_for_new_recipient(pending_handle)
                pending_handle = None
            verified = find_verified_send(
                chat_id,
                expected_text,
                baseline,
                send_started_at,
                upstream_result,
                attachments=[part] if is_file else None,
            )
            if verified is None:
                raise ImsgError("No unique matching outgoing history row was found")
        except KeyboardInterrupt as error:
            raise end_send_sequence(
                preview_id, parts, sent, index, "uncertain",
                "Interrupted after a send was claimed",
            ) from error
        except RpcSendError as error:
            raise end_send_sequence(
                preview_id, parts, sent, index,
                "failed" if error.disposition == "not_started" else "uncertain",
                str(error),
            ) from error
        except Exception as error:
            raise end_send_sequence(
                preview_id, parts, sent, index, "uncertain",
                "Unverified post-claim failure",
            ) from error

        # Advance past the row just written. Without this, two parts carrying
        # IDENTICAL text -- or two attachments, which share the same U+FFFC row
        # text -- make the second one match both rows, and find_verified_send
        # correctly refuses an ambiguous verification, so a perfectly good send
        # would be reported as a partial failure.
        if isinstance(verified.get("id"), int):
            baseline = max(baseline, verified["id"])
        sent.append({
            "bubble": index + 1,
            "kind": part["kind"],
            "file": part.get("name"),
            "message_id": verified.get("id"),
            "message_guid": verified.get("guid"),
            "created_at": verified.get("created_at"),
            "upstream": upstream_result,
            "chat_id": chat_id,
        })


def discard_preview(preview_id: str) -> dict[str, Any]:
    load_preview(preview_id, require_unexpired=False)
    path = preview_path(preview_id)
    path.unlink()
    fsync_directory(path.parent)
    return {"preview_id": preview_id, "status": "discarded", "nothing_sent": True}


def doctor() -> tuple[dict[str, Any], bool]:
    binary = upstream_binary()
    result: dict[str, Any] = {
        "platform": platform.system(),
        "binary": str(binary),
        "expected_version": UPSTREAM_VERSION,
        "database_readable": False,
        "send_automation": "not tested; no message was sent",
        "live_watch": "not installed by design",
    }
    errors: list[str] = []
    if platform.system() != "Darwin":
        errors.append("iMessage sending requires macOS")
    try:
        result["version"] = run_imsg(["--version"]).strip()
    except ImsgError as error:
        errors.append(str(error))
    try:
        list_chats(1)
        result["database_readable"] = True
    except ImsgError as error:
        errors.append(str(error))
    ensure_private_dir(state_root())
    result["state_directory"] = str(state_root())
    result["advanced_features"] = advanced_feature_status()
    result["ready_for_read_only"] = not errors
    result["errors"] = errors
    return result, not errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="imessage",
        description="Safe on-demand iMessage CLI (iMessage, SMS and RCS reads; no live watch)",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"imessage-kit 1 / imsg {UPSTREAM_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor_parser = commands.add_parser(
        "doctor", help="Check the pinned imsg runtime and read permission without sending", allow_abbrev=False
    )
    doctor_parser.add_argument(
        "--approval-ui",
        action="store_true",
        help="UI-only typed diagnostic (60 seconds); no Messages, previews, or send authorization",
    )

    chats = commands.add_parser("chats", help="List recent chats", allow_abbrev=False)
    chats.add_argument("--limit", type=int, default=20, choices=range(1, 501), metavar="1..500")
    chats.add_argument(
        "--unread-only",
        action="store_true",
        help="Only chats with unread inbound messages (still a read; nothing is marked read)",
    )

    resolve = commands.add_parser("resolve", help="Resolve one exact existing iMessage chat for reading", allow_abbrev=False)
    resolve.add_argument("query", help="Exact chat ID, handle, GUID, identifier, or exact display/contact name")
    resolve.add_argument("--limit", type=int, default=200, choices=range(1, 1001), metavar="1..1000")

    history = commands.add_parser("history", help="Read one existing direct or group iMessage chat", allow_abbrev=False)
    history.add_argument("--chat-id", type=int, required=True)
    history.add_argument("--limit", type=int, default=20, choices=range(1, 1001), metavar="1..1000")
    history.add_argument("--attachments", action="store_true")
    history.add_argument(
        "--full", action="store_true",
        help="Lossless read (msgschema/1 JSON): attachments always listed, reactions folded onto "
             "their message, reply parents filled, --limit counted in real messages")
    history.add_argument("--since", help="ISO-8601 UTC instant, inclusive (2026-09-01T00:00:00Z)")
    history.add_argument("--until", help="ISO-8601 UTC instant, exclusive (2026-09-08T00:00:00Z)")
    history.add_argument("--before-row", type=int,
                         help="Full-read continuation row ID; use with the advertised --until")
    history.add_argument(
        "--participant",
        action="append",
        default=[],
        metavar="HANDLE",
        help="Only messages from this handle; repeatable",
    )

    search = commands.add_parser(
        "search",
        help="Search local history across every readable iMessage chat",
        allow_abbrev=False,
    )
    search.add_argument("query", help="Text to look for")
    search.add_argument("--match", default="contains", choices=list(SEARCH_MATCH_MODES))
    search.add_argument(
        "--limit", type=int, default=25, choices=range(1, MAX_SEARCH_LIMIT + 1),
        metavar=f"1..{MAX_SEARCH_LIMIT}",
    )

    whois = commands.add_parser(
        "whois",
        help="Look up a handle's service from local history only (never a live probe)",
        allow_abbrev=False,
    )
    whois.add_argument("address", help="Phone number or email address")
    whois.add_argument("--type", dest="kind", default="phone", choices=list(WHOIS_KINDS))
    whois.add_argument(
        "--limit", type=int, default=200, choices=range(1, 1001), metavar="1..1000",
        help="How many recent chats to scan for this handle (same bound as resolve)",
    )

    commands.add_parser(
        "stats", help="Bounded aggregate counts over local Messages history", allow_abbrev=False
    )

    prepare = commands.add_parser("prepare-send", help="Create an immutable expiring send preview", allow_abbrev=False)
    # SingleValue here for the same reason it is on `send`: the shell wrapper can
    # only inspect argv[2], so `--chat-id 104 --chat-id 7` satisfied its guard and
    # then routed to 7. The fix was applied to `send` and missed here.
    prepare.add_argument("--chat-id", type=int, required=True, action=SingleValue)
    prepare.add_argument(
        "--text-file",
        type=Path,
        action="append",
        required=True,
        dest="text_file",
        metavar="PATH",
        help=(
            "Private 0600 UTF-8 file holding ONE exact bubble. Repeat the flag for a "
            f"multi-bubble message, in send order (max {MAX_PREVIEW_BUBBLES}); a single "
            "typed approval then covers every bubble in that one preview."
        ),
    )

    send_now = commands.add_parser(
        "send",
        help="Send text and/or files with no dialog; routing checks and verification still run",
        allow_abbrev=False,
    )
    target = send_now.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--chat-id", type=int, action=SingleValue,
        help="Existing chat rowid, direct or group. Get it from `resolve` or `chats`.",
    )
    target.add_argument(
        "--to", action=SingleValue, metavar="HANDLE",
        help=(
            "Phone number or email. Reuses the existing one-to-one chat with that "
            "person when there is one, and otherwise texts them for the first time. "
            "That second case is the one form that can reach a stranger, so read the "
            "number back to the owner before using it."
        ),
    )
    send_now.add_argument(
        "--text-file", action=OrderedPart, kind="text_file", dest="parts", metavar="PATH",
        help=(
            "Private 0600 UTF-8 file under the drafts directory holding ONE exact "
            "bubble. Repeatable; parts leave in the order the flags are given "
            f"(max {MAX_PREVIEW_BUBBLES} parts in total)."
        ),
    )
    send_now.add_argument(
        "--text", action=OrderedPart, kind="text", dest="parts", metavar="TEXT",
        help=(
            "ONE exact bubble as a literal argument. Convenient by hand, but the text "
            "is visible in `ps` and shell history, so prefer --text-file for anything "
            "private and for every automated caller."
        ),
    )
    send_now.add_argument(
        "--file", action=OrderedPart, kind="file", dest="parts", metavar="PATH",
        help=(
            "Attachment to send. Repeatable, and freely interleaved with text parts. "
            "Paths matching credential locations are refused."
        ),
    )
    send_now.add_argument(
        "--service", choices=SEND_SERVICES, default=None,
        action=SingleValue,
        help=(
            "By default the chat decides: an iMessage chat sends as iMessage, an SMS "
            "chat as SMS, and a brand-new --to recipient as iMessage so a first "
            "message is never silently downgraded to an unencrypted green bubble. "
            "Set this only to override. `auto` allows SMS fallback, `sms` forces it."
        ),
    )
    send_now.add_argument(
        # default=None, not "US". SingleValue refuses a dest that is already set,
        # and argparse installs defaults BEFORE parsing, so a literal default made
        # every `--region GB` exit 2 with "may only be given once". The real
        # default is applied in handle().
        "--region", default=None, action=SingleValue, metavar="CC",
        help="Region used to normalize a bare phone number in --to. Default US.",
    )
    send_now.add_argument(
        "--override-check", dest="override_check", action=SingleValue, metavar="REASON",
        help=(
            "Send even though the computed draft gate returned a finding. Requires a "
            "reason, which is recorded in the send audit log. Only after showing the owner "
            "the exact text and getting their yes. It cannot override a checker that is "
            "missing, crashed, or otherwise unable to answer."
        ),
    )

    show = commands.add_parser("show-preview", help="Recheck an unsent preview", allow_abbrev=False)
    show.add_argument("preview_id")

    send = commands.add_parser(
        "send-preview",
        help="Open a macOS approval dialog, then send the exact preview once",
        allow_abbrev=False,
    )
    send.add_argument("preview_id")

    discard = commands.add_parser("discard-preview", help="Delete one unsent preview", allow_abbrev=False)
    discard.add_argument("preview_id")
    return parser


def run_draft_check(texts: list[str]) -> tuple[str, str]:
    """Run the shared computed voice/consent gate on the exact bubbles.

    This check lived in the CALLER until 2026-09-09, and was forgotten within an
    hour of the headless send shipping: the checker printed BLOCKED, one shell
    script had dropped its exit-code test, and the message went out anyway. A
    gate the caller has to remember fails open, which is the wrong direction for
    the only gate left once the dialog is gone. So it lives here now.

    Bubbles are written to a private scratch directory rather than passed as
    arguments, for the same reason drafts are: message text does not belong in
    argv where `ps` can read it.

    Returns ("clean" | "finding", detail). Raises for anything that is not a
    trustworthy answer -- missing, unrunnable, crashed, or an undefined exit
    code. Those are deliberately NOT overridable: "the gate said no" is a
    judgement a human can overrule, "the gate could not answer" is not.
    """
    checker = DRAFT_CHECKER if DRAFT_CHECKER is not None else draft_checker()
    if not checker.is_file():
        raise ImsgError(
            f"The computed draft gate is missing at {checker}; nothing was sent. "
            "Restore it or set imessage.draft_checker; do not send around it."
        )
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        root.chmod(0o700)
        paths: list[str] = []
        for index, text in enumerate(texts, start=1):
            bubble = root / f"bubble{index}.txt"
            bubble.write_text(text, encoding="utf-8")
            bubble.chmod(0o600)
            paths.append(str(bubble))
        try:
            result = subprocess.run(
                ["/usr/bin/python3", "-I", str(checker), "--channel", "imessage", *paths],
                capture_output=True, text=True, timeout=DRAFT_CHECK_SECONDS, check=False,
                env={"PATH": "/usr/bin:/bin", "HOME": str(root), "LC_ALL": "C.UTF-8"},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ImsgError(
                f"The computed draft gate could not run; nothing was sent: {error}"
            ) from error
    detail = bounded_error_text((result.stdout + result.stderr).strip())
    if result.returncode == 0:
        return "clean", detail
    # draft_check.py has no try/except, so an uncaught exception leaves through
    # Python's default exit 1 -- the same code a real finding uses. Told apart by
    # shape: a finding always writes its report to stdout, a crash writes a
    # traceback to stderr and leaves stdout empty. Without this, a broken gate
    # arrives as "refused, you may override" and gets waved through.
    crashed = "Traceback (most recent call last)" in result.stderr or not result.stdout.strip()
    if result.returncode == 1 and not crashed:
        return "finding", detail
    raise ImsgError(
        f"The computed draft gate could not answer (exit {result.returncode}); nothing was "
        f"sent, and this is not overridable. Repair the checker: {detail}"
    )


# Generous UTF-8 headroom over the text cap; a longer file is not a message.
ATTACHMENT_TEXT_SCAN_BYTES = MAX_TEXT_CHARS * 4


def attachment_text_for_gate(part: dict[str, Any]) -> str | None:
    """An attachment's words, if it has any, so the wording gate can read them.

    A `.txt` reading "yes, $5,000 up front works" bypassed MONEY LOCK entirely
    while the identical words passed as `--text-file` did not. The gate reads
    words; these are words. A PNG or a PDF has no decodable text here and is
    reported as UNREAD rather than quietly counted as clean.
    """
    try:
        with Path(part["path"]).open("rb") as handle:
            raw = handle.read(ATTACHMENT_TEXT_SCAN_BYTES + 1)
    except OSError:
        return None
    if len(raw) > ATTACHMENT_TEXT_SCAN_BYTES or b"\x00" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if text.strip() else None


def collect_send_parts(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Resolve the ordered --text/--text-file/--file list into sendable parts.

    Each part is one thing that goes out on its own: a text bubble, or an
    attachment. They leave in the order given here, and the send sequence stops
    at the first failure, so the order is part of the contract rather than a
    detail.
    """
    raw = getattr(args, "parts", None) or []
    if not raw:
        raise ImsgError("Provide at least one --text, --text-file, or --file")
    if len(raw) > MAX_PREVIEW_BUBBLES:
        raise ImsgError(
            f"A message carries at most {MAX_PREVIEW_BUBBLES} parts; got {len(raw)}. "
            "Split it across separate sends."
        )
    parts: list[dict[str, Any]] = []
    for kind, value in raw:
        if kind == "text":
            parts.append({"kind": "text", "text": value})
        elif kind == "text_file":
            parts.append({"kind": "text", "text": read_private_text(Path(value))})
        elif kind == "file":
            parts.append(dict(validate_attachment(Path(value)), kind="file"))
        else:
            raise ImsgError(f"Unknown message part: {kind}")
    return parts


def handle(args: argparse.Namespace) -> tuple[Any, bool]:
    if args.command == "doctor":
        if args.approval_ui:
            return check_approval_ui(), True
        return doctor()
    if args.command == "chats":
        chats = list_chats(args.limit, unread_only=args.unread_only)
        return {"chats": chats, "count": len(chats), "unread_only": args.unread_only}, True
    if args.command == "resolve":
        detail = resolve_exact_chat(args.query, args.limit)
        return {"chat": detail, "safety": "existing iMessage reads; sends remain direct-only"}, True
    if args.command == "history" and getattr(args, "full", False):
        return history_full(args, readable_chat(args.chat_id)), True
    if args.command == "history":
        if getattr(args, "before_row", None) is not None:
            raise ImsgError("--before-row requires --full")
        detail = readable_chat(args.chat_id)
        rows = history_rows(
            args.chat_id,
            args.limit,
            attachments=args.attachments,
            since=args.since,
            until=args.until,
            participants=args.participant,
        )
        chat = dict(display_snapshot(detail), id=detail["id"], is_group=detail["is_group"])
        window = history_window(
            detail, since=args.since, until=args.until, participants=args.participant
        )
        return {"chat": chat, "messages": rows, "window": window}, True
    if args.command == "search":
        return search_messages(args.query, args.match, args.limit), True
    if args.command == "whois":
        return whois_local(args.address, args.kind, chat_scan_limit=args.limit), True
    if args.command == "stats":
        return stats_summary(), True
    if args.command == "prepare-send":
        texts = [read_private_text(path) for path in args.text_file]
        # The dialog names a count and a recipient, never the words, so a human
        # approving there has not necessarily read them either. A path that skips
        # the computed gate is a path the gate does not cover, and this one was
        # skipping it. No override flag here: the dialog route is legacy, and
        # anything needing an override should go through `send`.
        verdict, detail = run_draft_check(texts)
        if verdict == "finding":
            raise ImsgError(
                "The computed draft gate refused this message; no preview was created "
                f"and nothing was sent. Show the owner the exact text. Findings: {detail}"
            )
        preview = create_preview(args.chat_id, texts)
        # Outbound gate (P0): evaluated here so the preview shows a refusal early; the binding
        # check and the reservation happen at commit time, never at preparation.
        try:
            gate_context(load_preview(preview["preview_id"])["payload"]["routing"], parts_from_texts(texts),
                         None, dry_run=True, source="imessage prepare-send")
        except GateRefused:
            discard_preview(preview["preview_id"])
            raise
        return preview, True
    if args.command == "send":
        # Headless and wide by the owner's explicit instruction. This reuses the preview machinery rather than
        # opening a second send path: routing revalidation, tool-markup refusal,
        # attachment guards, per-part verification and the partial-send report
        # are all still enforced. What was removed is the human dialog, so the
        # record it writes is an audit trail, not a gate.
        parts = collect_send_parts(args)
        texts = [part["text"] for part in parts if part["kind"] == "text"]
        files = [part for part in parts if part["kind"] == "file"]
        # A bare truthiness test let `--override-check " "` through: whitespace is
        # truthy, so a single space skipped the only remaining gate while
        # recording a reason nobody could act on. The reason is the audit trail,
        # so an unreadable one is the same as no override at all.
        override = (args.override_check or "").strip()
        if args.override_check is not None and len(override) < 4:
            raise ImsgError(
                "--override-check needs a real reason of at least 4 characters "
                "naming what the owner approved; nothing was sent"
            )
        # Attachments that carry words go to the gate too. A .txt saying
        # "$5,000 up front" is the same consent question as the same words in a
        # bubble, and routing it through --file used to skip MONEY LOCK entirely.
        gate_texts = list(texts)
        read_files: list[str] = []
        unread_files: list[str] = []
        for part in files:
            content = attachment_text_for_gate(part)
            if content is None:
                unread_files.append(part["name"])
            else:
                read_files.append(part["name"])
                gate_texts.append(content)
        # The gate ALWAYS runs when there is anything to read, override or not.
        # It used to be skipped wholesale by --override-check, which meant the
        # override also skipped the missing-checker and crashed-checker refusals
        # its own help text promised it could not skip. An override waives a
        # finding; it can never waive an unanswered gate.
        if gate_texts:
            verdict, detail = run_draft_check(gate_texts)
        else:
            verdict, detail = "skipped", (
                f"nothing readable to check; the contents of {len(unread_files)} "
                "attachment(s) were NOT read. Show the owner the filenames."
            )
        if verdict == "finding" and not override:
            raise ImsgError(
                "The computed draft gate refused this message; nothing was sent. Show the owner "
                "the exact text and re-run with --override-check REASON only if they say so. "
                "If the finding is the bubble budget, cut a whole idea instead of overriding. "
                f"Findings: {detail}"
            )
        if verdict == "finding":
            draft_check = f"overridden: {override}"
        elif verdict == "skipped":
            draft_check = detail
        elif override:
            draft_check = f"passed; the override was not needed: {override}"
        else:
            draft_check = "passed"
        if read_files:
            draft_check += f"; also read the text of {', '.join(read_files)}"
        if unread_files and gate_texts:
            draft_check += f"; did NOT read {', '.join(unread_files)}"
        target: dict[str, Any]
        if args.to is not None:
            recipient = require_send_handle(args.to)
            existing = direct_chats_for_handle(recipient)
            if len(existing) > 1:
                raise ImsgError(
                    f"{len(existing)} existing chats match {recipient} "
                    f"(ids {[chat.get('id') for chat in existing]}); nothing was sent. "
                    "Name the one you mean with --chat-id."
                )
            if existing:
                # Reuse the thread he already has. A second thread to the same
                # person is confusing on the phone, and a real chat gives routing
                # revalidation and a history baseline that a bare handle cannot.
                chat_id = existing[0].get("id")
                if not isinstance(chat_id, int) or isinstance(chat_id, bool):
                    raise ImsgError("The existing chat for this handle has no usable ID")
                preview = create_preview(
                    chat_id, parts=parts, service=args.service, headless=True
                )
                target = {"chat_id": chat_id, "resolved_from": recipient}
            else:
                preview = create_handle_preview(
                    recipient,
                    parts,
                    service=args.service or "imessage",
                    region=args.region or "US",
                )
                target = {"to": recipient, "new_recipient": True}
        else:
            preview = create_preview(
                args.chat_id, parts=parts, service=args.service, headless=True
            )
            target = {"chat_id": args.chat_id}
        # Name WHO this actually reached. `--to` resolves a typed handle to a
        # stored chat, and without echoing the chat's own participants nothing in
        # the output would reveal that the two are different people.
        target["recipient"] = preview["target"]
        preview_id = preview["preview_id"]

        def audit(status: str) -> None:
            append_send_audit({
                "at": iso_time(int(time.time())),
                "preview_id": preview_id,
                # Counts, routing and the gate verdict only. The audit log never
                # learns what was said or which file went out.
                "chat_id": target.get("chat_id"),
                "new_recipient": bool(target.get("new_recipient")),
                "bubbles": len(texts),
                "files": len(files),
                "draft_check": verdict,
                "override_reason": override or None,
                "status": status,
            })

        try:
            result = commit_preview(preview_id, approval_phrase(preview_id), override or None)
        except SendSequenceError as error:
            # A partial or failed send is exactly when the audit trail matters
            # most, and it used to be the one outcome that never wrote a line:
            # an overridden gate followed by a half-delivered message left no
            # trace of either.
            audit(str(error.report.get("status", "failed")))
            raise
        except ImsgError:
            audit("refused")
            raise
        result["approval"] = "headless; no dialog was shown"
        result["draft_check"] = draft_check
        result["target"] = target
        audit(str(result.get("status")))
        return result, True
    if args.command == "show-preview":
        return show_preview(args.preview_id), True
    if args.command == "send-preview":
        # Invalid, expired, tampered or consumed previews must not open a dialog.
        # commit_preview rechecks after approval, including expiry and routing.
        document = load_preview(args.preview_id)
        typed_approval = prompt_for_approval(
            args.preview_id, approval_summary(document["payload"])
        )
        return commit_preview(args.preview_id, typed_approval), True
    if args.command == "discard-preview":
        return discard_preview(args.preview_id), True
    raise ImsgError(f"Unsupported command: {args.command}")


def log_read(args: argparse.Namespace, value: Any) -> None:
    """Archive a history read outside a hook runtime (Claude Code sets CLAUDECODE; pi's
    Claude-hooks bridge sets CLAUDE_CODE_SESSION_ID; both log reads in their hook) and outside
    comms' own scans. The body is exactly what print_json wrote."""
    if (args.command != "history" or os.environ.get("CLAUDECODE")
            or os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("COMMS_INTERNAL")):
        return
    C = comms_module("commslib")
    if C is None:
        return
    body = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    C.log_event("imessage", "read", str(args.chat_id), body=body, verb="history",
                command="imessage history --chat-id %s" % args.chat_id)


def main() -> int:
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args()
    try:
        value, ok = handle(args)
        print_json(value)
        log_read(args, value)
        return 0 if ok else 1
    except KeyboardInterrupt:
        print_json({"error": "Cancelled; inspect preview status before any retry"}, stream=sys.stderr)
        return 130
    except SendSequenceError as error:
        # Must precede the ImsgError arm (it is a subclass). A partial send has
        # to reach the caller as structured data: "which bubbles landed" is not
        # something a prose error string can be parsed for.
        print_json({"error": str(error), "send_report": error.report}, stream=sys.stderr)
        return 2
    except GateRefused as error:
        print_json({"error": str(error)}, stream=sys.stderr)
        return GATE_DENY_EXIT
    except ImsgError as error:
        print_json({"error": str(error)}, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
