#!/usr/bin/env python3
"""Pull phone-call transcripts into the comms archive. Read-only. Python stdlib only.

Verbs: pull, list, show, doctor.
Sources: Apple Notes (call recordings), Quo (formerly OpenPhone), litepipe (local meeting db).

Nothing is sent to a person and nothing changes on a service. Quo is read with GET only. The
Notes store and the litepipe db are opened read-only. The one POST is the optional --whisper
step: it sends the audio of a recording that has no transcript to the whisper service named in
config (calls.whisper_url). No model runs on this machine.

Each call becomes one plain-text file, <archive>/calls/YYYY-MM-DD_<source>_<id>.txt, and one
archive row, so `comms find` reaches it. A second pull adds nothing. A call that is not ready
yet (still transcribing, still running) is left out and tried again on the next pull.

Exit codes: 0 ok (nothing to do, or a source skipped), 1 a source failed, 2 usage or config.
"""
from __future__ import annotations

import argparse
import contextlib
from collections import Counter
import datetime as dt
import glob
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field

try:
    import fcntl
except ImportError:  # pragma: no cover - no flock on Windows
    fcntl = None

QUO_BASE = "https://api.quo.com"
QUO_VERSION = "2026-03-30"
COCOA_EPOCH = 978307200
DEFAULT_NOTES_DB = "~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"
DEFAULT_FOLDER = "Call Recordings"
DEFAULT_APPS = ["FaceTime", "Phone"]
AUDIO_UTIS = ("com.apple.m4a-audio", "public.mpeg-4-audio")
TRANSCRIPT_WAIT_SECONDS = 24 * 3600   # a fresh Quo call with no transcript row yet is not ready
HTTP_OK = 2 * 100
NO_TRANSCRIPT = "No transcript is available for this call."


class ApiError(RuntimeError):
    """A failed call. The message never holds the key."""


class SchemaError(RuntimeError):
    """The Notes store does not look like the layout this tool knows."""


class ArchiveError(RuntimeError):
    """The archive row could not be written."""


def die(code: int, line: str):
    print(line, file=sys.stderr)
    raise SystemExit(code)


# ---- finding core and config -----------------------------------------------------------
def _find_dir(env_name: str, *candidates: str):
    paths = []
    if env_name and os.environ.get(env_name):
        paths.append(os.path.expanduser(os.environ[env_name]))
    here = os.path.dirname(os.path.abspath(__file__))
    paths += [os.path.join(here, c) for c in candidates]  # kit: code path
    paths.append(os.path.expanduser("~/.claude/communications"))
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            yield p


def _comms_module(name: str):
    """core/<name>.py loaded by exact path; None when it cannot load."""
    for d in _find_dir("COMMS_KIT_CORE", "../../core"):
        path = os.path.join(d, name + ".py")
        if not os.path.exists(path):
            continue
        have = sys.modules.get(name)
        if have is not None and getattr(have, "__file__", None) == path:
            return have
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
        except Exception:  # noqa: BLE001 - the caller decides what a missing module means
            sys.modules.pop(name, None)
    return None


def _get_commslib():
    return _comms_module("commslib")


def _cfg(key: str, default=None):
    kc = _comms_module("kitconfig")
    if kc is None:
        return default
    return kc.get(key, default)


def _archive_root() -> str:
    kc = _comms_module("kitconfig")
    if kc is None:
        die(2, "comms-kit core not found (set COMMS_KIT_CORE).")
    return kc.archive_dir()


def _calls_dir() -> str:
    return os.path.join(_archive_root(), "calls")


# ---- keys and HTTP ---------------------------------------------------------------------
def _real_run_key_cmd(cmd) -> str:
    """Run a key command with no shell. Returns the key; raises ApiError with a key-free line."""
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
        raise ApiError("the key command must be a list of strings")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, shell=False)
    except FileNotFoundError:
        raise ApiError("the key command was not found") from None
    except (subprocess.TimeoutExpired, OSError):
        raise ApiError("the key command did not finish") from None
    key = (proc.stdout or "").strip()
    if proc.returncode != 0 or not key:
        raise ApiError(f"the key command failed (exit {proc.returncode})")
    return key


_run_key_cmd = _real_run_key_cmd


def _load_fallback_bundle(ctx) -> bool:
    """The python.org build on macOS ships no CA bundle. Use the system one, else certifi."""
    for p in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):
        if os.path.exists(p):
            ctx.load_verify_locations(p)
            return True
    try:
        import certifi  # noqa: PLC0415
        ctx.load_verify_locations(certifi.where())
        return True
    except Exception:  # noqa: BLE001
        return False


def _ssl_context() -> ssl.SSLContext:
    """Always verified. Under launchd the shell's SSL_CERT_FILE is gone, so find a bundle."""
    ctx = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    env_file = os.environ.get("SSL_CERT_FILE", "")
    env_dir = os.environ.get("SSL_CERT_DIR", "")
    have = ((paths.cafile and os.path.exists(paths.cafile))
            or (paths.capath and os.path.isdir(paths.capath))
            or (env_file and os.path.exists(env_file)) or (env_dir and os.path.isdir(env_dir)))
    if not have:
        _load_fallback_bundle(ctx)
    return ctx


def _real_http_get(url: str, headers: dict, timeout: int = 30):
    """(status, json). A transport failure raises ApiError. The key never enters a message."""
    req = urllib.request.Request(url, method="GET", headers=dict(headers, **{
        "Accept": "application/json", "User-Agent": "comms-kit-calls/1"}))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read() or b"", exc.code
    except Exception:  # noqa: BLE001 - timeout, reset, DNS: no key in the text
        raise ApiError("could not reach the service") from None
    try:
        return status, json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except ValueError:
        return status, {}


http_get = _real_http_get


def whisper_post(base_url: str, audio: bytes, filename: str) -> str:
    """POST audio to <base_url>/v1/transcribe (multipart, field `file`). Returns the text.
    The service is a transcription box: it keeps nothing and messages no one."""
    boundary = "----calls" + uuid.uuid4().hex
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "audio.m4a"
    crlf = b"\r\n"
    body = b"".join([
        b"--" + boundary.encode() + crlf,
        ('Content-Disposition: form-data; name="file"; filename="%s"' % safe).encode() + crlf,
        b"Content-Type: application/octet-stream" + crlf, crlf, audio, crlf,
        b"--" + boundary.encode() + b"--" + crlf])
    req = urllib.request.Request(base_url.rstrip("/") + "/v1/transcribe", data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    try:
        with urllib.request.urlopen(req, timeout=300, context=_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise ApiError(f"whisper answered HTTP {exc.code}") from None
    except Exception:  # noqa: BLE001
        raise ApiError("could not reach the whisper service") from None
    text = str(data.get("text") or "").strip() if isinstance(data, dict) else ""
    if not text:
        raise ApiError("whisper returned no text")
    return text


# ---- model -----------------------------------------------------------------------------
@dataclass
class Item:
    source: str
    id: str
    ts: float | None                 # call time, unix
    status: str                      # ok | no_transcript | pending | known
    text: str = ""
    title: str = ""
    meta: dict = field(default_factory=dict)
    how: str = ""                    # who made the text: apple, quo, litepipe, whisper


def safe_id(raw) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(raw))[:80] or "unknown"


def local_date(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def parse_iso(value) -> float | None:
    if not value:
        return None
    try:
        d = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def render(item: Item) -> str:
    head = [f"source: {item.source}", f"id: {item.id}",
            f"date: {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(item.ts)) if item.ts else 'unknown'}"]
    if item.title:
        head.append(f"title: {item.title}")
    for k, v in item.meta.items():
        if v not in (None, ""):
            head.append(f"{k}: {v}")
    head.append(f"transcript: {item.how if item.status == 'ok' else 'none'}")
    body = item.text if item.status == "ok" else (item.text or NO_TRANSCRIPT)
    return "\n".join(head) + "\n\n" + body.rstrip() + "\n"


# ---- index and files -------------------------------------------------------------------
def _index_path() -> str:
    return os.path.join(_calls_dir(), "index.json")


def load_index() -> dict:
    try:
        with open(_index_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_index(idx: dict) -> None:
    folder = _calls_dir()
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".index-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(idx, fh, indent=1, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _index_path())


def ensure_dir() -> str:
    folder = _calls_dir()
    os.makedirs(folder, mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


def file_name(item: Item) -> str:
    return f"{local_date(item.ts)}_{item.source}_{safe_id(item.id)}.txt"


def has_file(item: Item) -> bool:
    pat = os.path.join(glob.escape(_calls_dir()), f"*_{item.source}_{glob.escape(safe_id(item.id))}.txt")
    return bool(glob.glob(pat))


def write_text(path: str, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


@contextlib.contextmanager
def _outside_hook_runtime():
    """The archive row IS the product of a pull, so it must be written even when a Claude Code
    session runs the pull (core drops read rows there, expecting the hook to log them)."""
    names = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "COMMS_INTERNAL")
    saved = {n: os.environ.pop(n) for n in names if n in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def archive_row(item: Item, path: str, text: str) -> None:
    lib = _get_commslib()
    if lib is None:
        raise ArchiveError("comms-kit core not found, so no archive row can be written")
    with _outside_hook_runtime():
        ok = lib.log_event("calls", "read", f"{item.source}:{item.id}", message_id=f"{item.source}:{item.id}",
                           source="calls-pull", verb="pull", body=text, files=[path], hook_covers=False,
                           command="calls.py pull")
    if not ok:
        raise ArchiveError("the archive row was not written")


def save_item(item: Item, idx: dict) -> str:
    folder = ensure_dir()
    path = os.path.join(folder, file_name(item))
    text = render(item)
    if _get_commslib() is None:
        raise ArchiveError("comms-kit core not found, so no archive row can be written")
    write_text(path, text)
    try:
        archive_row(item, path, text)
    except ArchiveError:
        with contextlib.suppress(OSError):
            os.remove(path)     # no row means not saved: the next pull tries again
        raise
    idx[f"{item.source}:{item.id}"] = {"file": os.path.basename(path), "status": item.status,
                                       "saved": int(time.time())}
    save_index(idx)
    return path


# ---- Notes -----------------------------------------------------------------------------
@contextlib.contextmanager
def notes_snapshot(db_path: str):
    """A consistent copy of the Notes store (WAL rows included), deleted on exit.
    The source opens read-only through an encoded file: URI (the real path holds a space)."""
    src = None
    tmp = tempfile.mkdtemp(prefix="calls-notes-")
    try:
        os.chmod(tmp, 0o700)
        uri = "file:" + urllib.parse.quote(os.path.abspath(db_path)) + "?mode=ro"
        src = sqlite3.connect(uri, uri=True, timeout=10)
        dst_path = os.path.join(tmp, "snapshot.sqlite")
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
        yield dst_path
    finally:
        if src is not None:
            src.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _notes_columns(con) -> set:
    return {r[1] for r in con.execute("PRAGMA table_info(ZICCLOUDSYNCINGOBJECT)")}


def read_notes(snapshot: str, folder: str):
    """-> (folder_found, notes). notes: list of dict, one per note in the folder with audio."""
    con = sqlite3.connect(snapshot)
    try:
        cols = _notes_columns(con)
        need = {"Z_PK", "ZTYPEUTI", "ZADDITIONALINDEXABLETEXT", "ZNOTE", "ZFOLDER", "ZIDENTIFIER"}
        ftitle = next((c for c in ("ZTITLE2", "ZTITLE") if c in cols), None)
        created = [c for c in ("ZCREATIONDATE3", "ZCREATIONDATE1", "ZCREATIONDATE") if c in cols]
        if not need <= cols or not ftitle or not created:
            raise SchemaError("The Notes store has a layout this tool does not know.")

        def opt(alias, col, default="NULL"):
            return f"{alias}.{col}" if col in cols else default
        title_col = next((c for c in ("ZTITLE1", "ZTITLE") if c in cols), None)
        found = con.execute(f"SELECT 1 FROM ZICCLOUDSYNCINGOBJECT WHERE lower({ftitle}) = lower(?) LIMIT 1",
                            (folder,)).fetchone()
        if not found:
            return False, []
        created_expr = ("COALESCE(%s)" % ", ".join("n." + c for c in created)) if len(created) > 1 else "n." + created[0]
        marks = ",".join("?" * len(AUDIO_UTIS))
        sql = f"""SELECT a.Z_PK, a.ZTYPEUTI, a.ZADDITIONALINDEXABLETEXT, {opt('a', 'ZNEEDSTRANSCRIPTION')},
                         {opt('a', 'ZDURATION')}, n.Z_PK, n.ZIDENTIFIER,
                         {('n.' + title_col) if title_col else 'NULL'},
                         {created_expr},
                         {opt('n', 'ZISPASSWORDPROTECTED')}, {opt('n', 'ZMARKEDFORDELETION')},
                         {opt('m', 'ZIDENTIFIER')}, {opt('m', 'ZFILENAME')}
                  FROM ZICCLOUDSYNCINGOBJECT a
                  JOIN ZICCLOUDSYNCINGOBJECT n ON n.Z_PK = a.ZNOTE
                  JOIN ZICCLOUDSYNCINGOBJECT f ON f.Z_PK = n.ZFOLDER
                  LEFT JOIN ZICCLOUDSYNCINGOBJECT m ON m.Z_PK = a.ZMEDIA
                  WHERE a.ZTYPEUTI IN ({marks}) AND lower(f.{ftitle}) = lower(?)
                  ORDER BY n.Z_PK, a.Z_PK"""
        notes: dict = {}
        for (apk, uti, text, needs, dur, npk, nident, title, cdate, locked, deleted, mid, mfile) in con.execute(
                sql, (*AUDIO_UTIS, folder)):
            n = notes.setdefault(npk, {"pk": npk, "ident": nident or f"pk{npk}", "title": title or "",
                                       "ts": (cdate + COCOA_EPOCH) if cdate is not None else None,
                                       "locked": bool(locked), "deleted": bool(deleted), "texts": [],
                                       "needs": False, "duration": 0.0, "media": None})
            if text and str(text).strip():
                n["texts"].append(str(text).strip())
            if needs:
                n["needs"] = True
            n["duration"] = max(n["duration"], float(dur or 0))
            if uti == AUDIO_UTIS[0] and mid and mfile and n["media"] is None:
                n["media"] = (mid, mfile)
        return True, list(notes.values())
    except sqlite3.DatabaseError as exc:
        raise SchemaError("The Notes store could not be read as expected (%s: %s)." % (exc.__class__.__name__, exc)) from None
    finally:
        con.close()


def find_audio(db_path: str, media) -> str | None:
    if not media:
        return None
    mid, name = media
    if "/" in name or "/" in mid or name.startswith("."):
        return None
    base = os.path.join(glob.escape(os.path.dirname(os.path.abspath(db_path))), "Accounts", "*", "Media",
                        glob.escape(mid), "*", glob.escape(name))
    hits = sorted(glob.glob(base))
    return hits[0] if hits else None


def collect_notes(args, idx, counts, whisper_url):
    """-> (items, error_line or None). Prints a plain line and returns nothing when the store or
    the folder is absent: that is normal on a Mac that records no calls."""
    db = os.path.expanduser(str(_cfg("calls.notes_db", DEFAULT_NOTES_DB)))
    folder = args.notes_folder or str(_cfg("calls.notes_folder", DEFAULT_FOLDER))
    if not os.path.exists(db):
        print("Notes store not found, so Notes is skipped.")
        return [], None
    try:
        with notes_snapshot(db) as snap:
            found, notes = read_notes(snap, folder)
    except SchemaError as exc:
        return [], f"Notes: {exc}"
    except (sqlite3.Error, OSError) as exc:
        return [], ("Notes: the store could not be opened (%s). "
                    "Check that this python has Full Disk Access." % exc.__class__.__name__)
    if not found:
        print(f"No folder named \"{folder}\" in Notes yet, so nothing to pull.")
        return [], None
    items = []
    for n in notes:
        if n["locked"]:
            counts["locked"] += 1
            continue
        if n["deleted"]:
            counts["deleted"] += 1
            continue
        if args.cutoff and n["ts"] is not None and n["ts"] < args.cutoff:
            continue
        meta = {"duration": f"{int(n['duration'])} s" if n["duration"] else ""}
        item = Item("notes", n["ident"], n["ts"], "ok", title=n["title"], meta=meta, how="apple")
        if n["texts"]:
            item.text = "\n\n".join(n["texts"])
        elif n["needs"]:
            item.status = "pending"
        else:
            item.status = "no_transcript"
            if args.whisper and not args.dry_run:
                entry = idx.get(f"notes:{item.id}")
                if not (entry and entry.get("status") == "ok"):
                    _try_whisper(item, db, n["media"], whisper_url, counts)
        items.append(item)
    return items, None


def _try_whisper(item, db, media, whisper_url, counts):
    path = find_audio(db, media)
    if not path:
        counts["audio missing"] += 1
        return
    try:
        with open(path, "rb") as fh:
            audio = fh.read()
        text = whisper_post(whisper_url, audio, os.path.basename(path))
    except (ApiError, OSError):
        counts["whisper failed"] += 1
        return
    item.text, item.status, item.how = text, "ok", "whisper"
    counts["whisper"] += 1


# ---- Quo -------------------------------------------------------------------------------
CALL_ID = re.compile(r"^AC[A-Za-z0-9]+$")


def _quo_get(path: str, params: dict, key: str):
    url = QUO_BASE + path + "?" + urllib.parse.urlencode(params)
    status, body = http_get(url, {"Authorization": key, "Quo-Api-Version": QUO_VERSION}, 30)
    if status in (401, 403):
        raise ApiError(f"Quo answered HTTP {status}: the key was refused. Check calls.quo_key_command.")
    if status != HTTP_OK:
        raise ApiError(f"Quo answered HTTP {status}.")
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise ApiError("Quo sent an answer in a shape this tool does not know.")
    return body


def _quo_pages(path, params, key):
    cursor = None
    while True:
        p = dict(params)
        if cursor:
            p["after"] = cursor
        body = _quo_get(path, p, key)
        yield from (r for r in body["data"] if isinstance(r, dict))
        cursor = body.get("nextCursor")
        if not cursor:
            return


def _quo_item(call: dict, key: str) -> Item:
    cid = call["id"]
    ts = parse_iso(call.get("createdAt"))
    parts = [p.get("phoneNumber") for p in call.get("participants") or [] if isinstance(p, dict)]
    meta = {"direction": call.get("direction"), "participants": ", ".join(str(p) for p in parts if p),
            "duration": f"{call['duration']} s" if call.get("duration") else ""}
    item = Item("quo", cid, ts, "ok", meta=meta, how="quo")
    segs = sorted(_quo_pages(f"/calls/{cid}/transcripts", {"limit": 50}, key),
                  key=lambda s: str(s.get("startTime") or ""))
    states = [s.get("status") for s in segs]
    if not segs:
        fresh = ts is not None and time.time() - ts < TRANSCRIPT_WAIT_SECONDS
        item.status = "pending" if fresh else "no_transcript"
        item.text = "" if fresh else NO_TRANSCRIPT + " Quo has none for this call."
        return item
    if any(s not in ("completed", "absent", "failed") for s in states):   # in-progress or unknown
        item.status = "pending"
        return item
    done = [s for s in segs if s.get("status") == "completed"]
    if not done:
        item.status = "no_transcript"
        item.text = NO_TRANSCRIPT + " Quo status: " + ", ".join(sorted(set(states))) + "."
        return item
    out = []
    for i, s in enumerate(done, 1):
        if len(segs) > 1:
            out.append(f"--- part {i} of {len(segs)} ---")
        for d in s.get("dialogue") or []:
            if not isinstance(d, dict):
                continue
            who = d.get("identifier") or d.get("actorId") or "Unknown"
            start = int(d.get("start") or 0)
            out.append(f"[{start // 60:02d}:{start % 60:02d}] {who}: {str(d.get('content') or '').strip()}")
    missing = len(segs) - len(done)
    if missing:
        out.append(f"--- {missing} part(s) have no transcript ({', '.join(s for s in states if s != 'completed')}) ---")
    item.text = "\n".join(out)
    return item


def collect_quo(args, idx, counts):
    cmd = _cfg("calls.quo_key_command")
    if not cmd:
        print("Quo skipped: calls.quo_key_command is not set.")
        return [], None
    try:
        key = _run_key_cmd(cmd)
    except ApiError as exc:
        print(f"Quo skipped: key lookup failed ({exc}).")
        return [], None
    params = {"limit": 50}
    if args.cutoff:
        params["createdAt[gte]"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(args.cutoff))
    items = []
    try:
        for call in _quo_pages("/calls", params, key):
            cid = str(call.get("id") or "")
            if not CALL_ID.match(cid):
                counts["bad id"] += 1
                continue
            if not (call.get("answeredAt") and call.get("completedAt")):
                continue  # no conversation yet. Quo does not say which status word a finished call gets.
            entry = idx.get(f"quo:{cid}")
            if entry and entry.get("status") in ("ok", "no_transcript"):
                items.append(Item("quo", cid, None, "known"))
                continue
            items.append(_quo_item(call, key))
    except ApiError as exc:
        return items, f"Quo: {exc} Stopped; calls already read are kept."
    return items, None


# ---- litepipe --------------------------------------------------------------------------
def _apps(args) -> list:
    if args.apps:
        return [a.strip() for a in args.apps.split(",") if a.strip()]
    value = _cfg("calls.litepipe_apps", DEFAULT_APPS)
    if isinstance(value, str):
        value = value.split(",")
    return [str(a).strip() for a in value if str(a).strip()] or list(DEFAULT_APPS)


def collect_litepipe(args, idx, counts):
    path = os.path.expanduser(str(_cfg("calls.litepipe_db", "") or ""))
    if not path:
        print("litepipe skipped: calls.litepipe_db is empty.")
        return [], None
    if not os.path.exists(path):
        print("litepipe skipped: the database was not found.")
        return [], None
    apps = _apps(args)
    items = []
    try:
        con = sqlite3.connect("file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro", uri=True,
                              timeout=10)
        try:
            con.execute("PRAGMA query_only = ON")
            marks = ",".join("?" * len(apps))
            sql = (f"SELECT id, meeting_start, meeting_end, meeting_app, title FROM meetings "
                   f"WHERE meeting_app IN ({marks}) ORDER BY meeting_start")
            for mid, start, end, app, title in con.execute(sql, apps).fetchall():
                ts = parse_iso(start)
                if args.cutoff and ts is not None and ts < args.cutoff:
                    continue
                if f"litepipe:{mid}" in idx and idx[f"litepipe:{mid}"].get("status") in ("ok", "no_transcript"):
                    items.append(Item("litepipe", str(mid), ts, "known"))
                    continue
                item = Item("litepipe", str(mid), ts, "ok", title=title or "", how="litepipe",
                            meta={"app": app})
                if not end:
                    item.status = "pending"
                else:
                    segs = con.execute("SELECT speaker_name, speaker_id, transcript FROM meeting_transcript_segments "
                                       "WHERE meeting_id = ? ORDER BY captured_at, id", (mid,)).fetchall()
                    if not segs:
                        item.status = "no_transcript"
                    else:
                        item.text = "\n".join(
                            "%s: %s" % (name or (f"Speaker {sid}" if sid is not None else "Speaker"), str(t).strip())
                            for name, sid, t in segs)
                items.append(item)
        finally:
            con.close()
    except sqlite3.Error as exc:
        return items, "litepipe: the database could not be read (%s)." % exc.__class__.__name__
    return items, None


# ---- pull ------------------------------------------------------------------------------
def _lock():
    if fcntl is None:
        return None
    ensure_dir()
    fh = open(os.path.join(_calls_dir(), ".lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    return fh


def _summary(name, counts, dry):
    label = "would save" if dry else "saved"
    parts = [f"{label} {counts['saved']}"]
    for k in ("no transcript", "already saved", "not ready", "locked", "deleted", "whisper",
              "whisper failed", "audio missing", "bad id", "archive failed"):
        if counts[k]:
            parts.append(f"{k} {counts[k]}")
    print(f"{name}: " + ", ".join(parts))


def cmd_pull(args):
    args.cutoff = time.time() - args.since * 86400 if args.since and args.since > 0 else None
    args.dry_run = not args.yes
    whisper_url = None
    if args.whisper:
        whisper_url = str(_cfg("calls.whisper_url", "") or "").strip()
        if not whisper_url:
            die(2, "--whisper needs calls.whisper_url in the comms-kit config.")
        if urllib.parse.urlparse(whisper_url).scheme not in ("http", "https"):
            die(2, "calls.whisper_url must start with http:// or https://.")
    lock = None
    if args.yes:
        lock = _lock()
        if lock is False:
            print("Another pull is running, so this one does nothing.")
            return
    try:
        idx = load_index() if os.path.isdir(_calls_dir()) else {}
        if args.dry_run:
            print("DRY RUN, nothing written. Add --yes to save.")
        names = ["notes", "quo", "litepipe"] if args.source == "all" else [args.source]
        failed = False
        for name in names:
            counts = Counter()
            if name == "notes":
                items, err = collect_notes(args, idx, counts, whisper_url)
            elif name == "quo":
                items, err = collect_quo(args, idx, counts)
            else:
                items, err = collect_litepipe(args, idx, counts)
            for item in items:
                key = f"{item.source}:{item.id}"
                entry = idx.get(key)
                if item.status == "pending":
                    counts["not ready"] += 1
                    continue
                if item.status == "known" or (entry and entry.get("status") == "ok") or \
                        (entry and entry.get("status") == "no_transcript" and item.status == "no_transcript"):
                    counts["already saved"] += 1
                    continue
                if not entry and has_file(item):
                    counts["already saved"] += 1
                    continue
                if args.dry_run:
                    counts["saved" if item.status == "ok" else "no transcript"] += 1
                    continue
                try:
                    save_item(item, idx)
                except ArchiveError as exc:
                    counts["archive failed"] += 1
                    err = err or f"{name}: {exc}."
                    failed = True
                    continue
                counts["saved" if item.status == "ok" else "no transcript"] += 1
            _summary(name, counts, args.dry_run)
            if err:
                print(err)
                failed = True
        if failed:
            raise SystemExit(1)
    finally:
        if lock:
            lock.close()


# ---- list, show ------------------------------------------------------------------------
def _call_files() -> list:
    return sorted(glob.glob(os.path.join(glob.escape(_calls_dir()), "*.txt")), reverse=True)


def cmd_list(args):
    idx = load_index()
    files = _call_files()[: max(1, args.limit)]
    if not files:
        print("No calls saved yet.")
        return
    by_file = {v.get("file"): v.get("status") for v in idx.values() if isinstance(v, dict)}
    for p in files:
        name = os.path.basename(p)
        date, source, rest = name[:-4].split("_", 2)
        print(f"{date}  {source:9} {rest}  {os.path.getsize(p)} bytes  {by_file.get(name, 'ok')}")
    print(f"({len(files)} shown of {len(_call_files())})")


def cmd_show(args):
    ref = args.id.strip()
    if ref.endswith(".txt"):
        ref = ref[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", ref):
        die(1, "That is not a call id. Use an id, source_id, or the file name from `list`.")
    hits = [p for p in _call_files() if os.path.basename(p)[:-4] == ref or os.path.basename(p)[:-4].endswith("_" + ref)]
    if not hits:
        die(1, "No saved call matches that id.")
    if len(hits) > 1:
        die(1, "More than one call matches. Use the full name:\n" + "\n".join(os.path.basename(h)[:-4] for h in hits))
    with open(hits[0], encoding="utf-8") as fh:
        sys.stdout.write(fh.read())


# ---- doctor ----------------------------------------------------------------------------
def cmd_doctor(args):
    bad = 0

    def line(name, status, text):
        nonlocal bad
        print(f"{name:9} {status:6} {text}")
        bad += status == "CHECK"
    try:
        root = _archive_root()
        if not os.path.isdir(root) or not os.access(root, os.W_OK):
            raise OSError
        line("archive", "ok", f"writable, {len(_call_files())} calls saved")
    except OSError:
        line("archive", "CHECK", "the archive folder is not writable")
    db = os.path.expanduser(str(_cfg("calls.notes_db", DEFAULT_NOTES_DB)))
    name = str(_cfg("calls.notes_folder", DEFAULT_FOLDER))
    if not os.path.exists(db):
        line("notes", "ok", "no Notes store on this machine")
    else:
        try:
            with notes_snapshot(db) as snap:
                found, notes = read_notes(snap, name)
            line("notes", "ok", f"store readable; folder {'found, %d recordings' % len(notes) if found else 'not created yet'}")
        except (SchemaError, sqlite3.Error, OSError) as exc:
            line("notes", "CHECK", f"store not readable ({exc.__class__.__name__}); grant Full Disk Access")
    cmd = _cfg("calls.quo_key_command")
    if not cmd:
        line("quo", "off", "calls.quo_key_command is not set")
    else:
        try:
            key = _run_key_cmd(cmd)
            _quo_get("/calls", {"limit": 1}, key)
            line("quo", "ok", "key accepted")
        except ApiError as exc:
            line("quo", "CHECK", str(exc))
    lp = os.path.expanduser(str(_cfg("calls.litepipe_db", "") or ""))
    if not lp:
        line("litepipe", "off", "calls.litepipe_db is empty")
    elif not os.path.exists(lp):
        line("litepipe", "CHECK", "the database was not found")
    else:
        try:
            con = sqlite3.connect("file:" + urllib.parse.quote(os.path.abspath(lp)) + "?mode=ro", uri=True, timeout=10)
            apps = _apps(args)
            n = con.execute("SELECT count(*) FROM meetings WHERE meeting_app IN (%s)" % ",".join("?" * len(apps)),
                            apps).fetchone()[0]
            con.close()
            line("litepipe", "ok", f"readable, {n} meetings in {len(apps)} app(s)")
        except sqlite3.Error as exc:
            line("litepipe", "CHECK", f"not readable ({exc.__class__.__name__})")
    wurl = str(_cfg("calls.whisper_url", "") or "").strip()
    if not wurl:
        line("whisper", "off", "calls.whisper_url is not set")
    else:
        try:
            status, _ = http_get(wurl.rstrip("/") + "/healthz", {}, 10)
            line("whisper", "ok" if status == HTTP_OK else "CHECK", f"health answered HTTP {status}")
        except ApiError:
            line("whisper", "CHECK", "the whisper service did not answer")
    if bad:
        raise SystemExit(1)


# ---- CLI -------------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="calls.py", description="Pull call transcripts into the comms archive (read-only).")
    sub = p.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("pull", help="save new call transcripts (dry run unless --yes)")
    sp.add_argument("--source", choices=["notes", "quo", "litepipe", "all"], default="all")
    sp.add_argument("--since", type=int, default=30, help="days back (0 = everything); default 30")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--notes-folder", help="Notes folder name (default: calls.notes_folder)")
    sp.add_argument("--apps", help="litepipe apps, comma separated (default: calls.litepipe_apps)")
    sp.add_argument("--whisper", action="store_true",
                    help="send recordings with no transcript to the whisper service (calls.whisper_url)")
    sp.set_defaults(fn=cmd_pull)
    sp = sub.add_parser("list", help="saved calls, newest first")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_list)
    sp = sub.add_parser("show", help="print one saved transcript")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_show)
    sub.add_parser("doctor", help="check each source").set_defaults(fn=cmd_doctor, apps=None)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - one plain line, never a traceback with paths or text
        kc = sys.modules.get("kitconfig")
        if kc is not None and exc.__class__.__name__ == "ConfigError":
            die(2, str(exc))
        die(1, f"calls.py failed: {exc.__class__.__name__}.")


if __name__ == "__main__":
    main()
