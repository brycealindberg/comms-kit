#!/usr/bin/env python3
"""Read and send Instagram DMs through a Matrix server that runs the mautrix-instagram bridge.

The bridge (mautrix-instagram, part of github.com/mautrix/meta) logs in to Instagram as you and puts
each Instagram chat in a Matrix room (a "portal"). This tool talks only to your Matrix server.

Verbs: chats, unread, history, find, download, send, reply, send-media, react, edit, delete,
mark-read, status, login, setup, clear-hold.

Ban safety (Instagram does not allow unofficial clients; it can warn, restrict or close an account):
  - Every write with --yes first asks the bridge bot for the login state. Anything but CONNECTED or
    BACKFILLING stops the write (exit 3). A challenge or a restriction shows up as BAD_CREDENTIALS.
  - When Instagram refuses a send with words that point to a restriction ("feedback_required",
    "try again later", "spam", a challenge ...), the tool sets a SEND HOLD for 24 hours. Every write
    stops until the hold ends or you run `clear-hold --yes` after you checked the Instagram app.
  - A daily cap (instagram.daily_send_cap, default 40) and a minimum gap between writes
    (instagram.min_send_gap_seconds, default 20) stop an agent loop from sending like a bot.
  - Reads never send a read receipt or a typing notice. Only `mark-read --yes` shows "Seen".

Exit codes: 0 ok, 1 refused or failed, 2 usage/setup problem, 3 the bridge or a guard is not ready
(nothing sent), 4 UNCERTAIN send (do not resend), 5 a newer message exists (--since), 6 money lock,
7 outbound gate refused.
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import importlib.util
import inspect
import json
import mimetypes
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

NOT_READY_EXIT = 3
UNCERTAIN_EXIT = 4
STALE_EXIT = 5
MONEY_LOCK_EXIT = 6
GATE_DENY_EXIT = 7

MAX_ROWS = 200
RECENT_FOR_STALE_CHECK = 30
READ_TIMEOUT = 60
SEND_TIMEOUT = 45
POLL_S = 1.0               # time between polls for a bot reply or a send status
STATUS_WAIT_S = 30.0       # how long to wait for the bridge's send status
BOT_REPLY_WAIT_S = 20.0    # how long to wait for the bridge bot to answer a command
LOGIN_WAIT_S = 600.0       # one login step can wait for you to approve it in the Instagram app
HOLD_HOURS = 24
DEFAULT_DAILY_CAP = 40
DEFAULT_MIN_GAP_S = 20
BURST_WRITES = 3            # quick bubbles to ONE chat that the gap allows (a person sends 2-3 in a row)
BURST_WINDOW_S = 60
OK_STATES = ("CONNECTED", "BACKFILLING")
STATUS_TYPE = "com.beeper.message_send_status"
FINAL_STATUSES = ("SUCCESS", "FAIL_RETRIABLE", "FAIL_PERMANENT")
# Words in an Instagram refusal that point to an account restriction, not to one bad message.
HOLD_WORDS = re.compile(
    r"restrict|feedback_required|try again later|1357031|spam|checkpoint|challenge|suspend|disabled|"
    r"scraping|automated|unusual activity|rate.?limit|too many|action blocked|we limit how often|"
    r"community guidelines|login_required|consent", re.I)
# Lines from mautrix-go bridgev2/commands/login.go that end the login attempt.
LOGIN_FAIL = re.compile(r"(Login failed|Failed to |Invalid |This login flow requires|You already have an ongoing|"
                        r"Unsupported display type|No login flows)")
LOGIN_LINE = re.compile(r"^\*\s+`([^`]+)`\s+\((.*?)\)\s+-\s+`([A-Z_]+)`", re.M)
BRIDGE_STATE_TYPES = ("m.bridge", "uk.half-shot.bridge")


class ApiError(RuntimeError):
    """A call that clearly did not go through. The message never holds a secret."""


class UncertainError(RuntimeError):
    """A write that may or may not have reached the server. Never retry."""


# ---- core and the shared support module ----------------------------------------------------
def _core_dirs():
    """Core folders in the order CONTRIBUTING.md gives: COMMS_KIT_CORE, <file>/../../core, then
    ~/.claude/communications."""
    here = os.path.dirname(os.path.abspath(__file__))  # kit: code path
    paths = []
    if os.environ.get("COMMS_KIT_CORE"):
        paths.append(os.path.expanduser(os.environ["COMMS_KIT_CORE"]))
    paths += [os.path.join(here, "..", "..", "core"), os.path.expanduser("~/.claude/communications")]  # kit: code path
    return [p for p in (os.path.abspath(p) for p in paths) if os.path.isdir(p)]


def _bootstrap():
    """Load channel_support.py (the shared guards) by path. None when it cannot be found."""
    for d in _core_dirs():
        path = os.path.join(d, "channel_support.py")
        if not os.path.exists(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location("channel_support", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules["channel_support"] = module
            spec.loader.exec_module(module)
            return module
        except Exception:  # noqa: BLE001 - a broken copy is the same as a missing one
            sys.modules.pop("channel_support", None)
    return None


_CS = _bootstrap()


def _cs():
    if _CS is None:
        die(2, "channel_support.py was not found (looked in COMMS_KIT_CORE, ../../core and "
               "~/.claude/communications). Nothing was run.")
    return _CS


def _comms_module(name: str):
    return _cs().load_module(name, _core_dirs())


def _conf(key: str, default=None):
    """One setting from the kit config. Callers pass a literal dotted key."""
    kc = _comms_module("kitconfig")
    if kc is None:
        return default
    try:
        return kc.get(key, default)
    except Exception:  # noqa: BLE001 - a broken config file reads as "not set" here
        return default


def die(code: int, line: str):
    print(line, file=sys.stderr)
    raise SystemExit(code)


# ---- settings and local state -----------------------------------------------------------------
def _open_tunnel(url) -> str:
    """url while something listens on its port, else "". A local tunnel to the bridge box, for a network that drops
    or intercepts the direct route: it is used only while it is open."""
    parts = urllib.parse.urlsplit(str(url or ""))
    try:
        socket.create_connection((parts.hostname, parts.port), timeout=0.3).close()
    except (OSError, TypeError, ValueError):
        return ""
    return str(url).rstrip("/")


def _homeserver() -> str:
    tunnel = _open_tunnel(_conf("instagram.tunnel_homeserver", ""))
    if tunnel:
        return tunnel
    hs = str(_conf("instagram.homeserver", "") or "").strip().rstrip("/")
    if not hs:
        die(2, "instagram.homeserver is not set. Set it with: python3 core/kitconfig.py set "
               "instagram.homeserver https://matrix.example.com")
    return hs


def _bot_user() -> str:
    bot = str(_conf("instagram.bot_user", "") or "").strip()
    if not bot.startswith("@") or ":" not in bot:
        die(2, "instagram.bot_user is not set (the bridge bot's Matrix id, e.g. @instagrambot:example.com).")
    return bot


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _daily_cap() -> int:
    return _as_int(_conf("instagram.daily_send_cap", DEFAULT_DAILY_CAP), DEFAULT_DAILY_CAP)


def _min_gap() -> int:
    return _as_int(_conf("instagram.min_send_gap_seconds", DEFAULT_MIN_GAP_S), DEFAULT_MIN_GAP_S)


def _session_path() -> str:
    return os.path.expanduser(os.environ.get("INSTAGRAM_MAUTRIX_SESSION", "~/.config/instagram-mautrix/session.json"))


def _state_dir() -> str:
    d = os.path.expanduser(os.environ.get("INSTAGRAM_MAUTRIX_STATE", "~/.local/share/comms-kit/instagram"))
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_private(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


def _session() -> dict:
    s = _read_json(_session_path(), None)
    if not isinstance(s, dict) or not s.get("token"):
        die(2, f"No Matrix session. Run `instagram.py setup --token-file <file>` first "
               f"(it writes {_session_path()}).")
    return s


def _mgmt_room(session) -> str:
    room = session.get("mgmt_room")
    if not room:
        die(2, "The session has no bridge bot room yet. Run `instagram.py setup` again.")
    return room


# ---- the Matrix transport -------------------------------------------------------------------------
class Matrix:
    """The few Matrix client-server calls this tool needs. Room ids keep their case."""

    def __init__(self, homeserver: str, token: str):
        self.hs, self.token = homeserver, token

    def _req(self, method, path, body=None, params=None, *, write=False, raw=None, ctype=None,
             timeout=READ_TIMEOUT):
        url = self.hs + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            req.add_header("Content-Type", ctype or "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}")
            except ValueError:
                err = {}
            raise ApiError(f"Matrix said HTTP {e.code} {err.get('errcode', '')} {err.get('error', '')}".strip()) from None
        except urllib.error.URLError as e:
            refused = isinstance(e.reason, ConnectionRefusedError) or "refused" in str(e.reason).lower()
            if write and not refused:
                raise UncertainError("no answer from the Matrix server") from None
            raise ApiError(f"cannot reach the Matrix server ({e.reason})") from None
        except (socket.timeout, TimeoutError):
            if write:
                raise UncertainError("the Matrix server did not answer in time") from None
            raise ApiError("the Matrix server did not answer in time") from None
        return payload

    def _json(self, *a, **kw):
        payload = self._req(*a, **kw)
        try:
            return json.loads(payload or b"{}")
        except ValueError:
            raise ApiError("the Matrix server sent something that is not JSON") from None

    @staticmethod
    def _q(value: str) -> str:
        return urllib.parse.quote(value, safe="")

    def whoami(self):
        return self._json("GET", "/_matrix/client/v3/account/whoami")["user_id"]

    def sync(self, timeline_limit):
        flt = {"room": {"timeline": {"limit": timeline_limit}, "state": {"lazy_load_members": False}},
               "presence": {"types": []}, "account_data": {"types": []}}
        return self._json("GET", "/_matrix/client/v3/sync", params={"filter": json.dumps(flt), "timeout": "0"})

    def messages(self, room, limit):
        body = self._json("GET", f"/_matrix/client/v3/rooms/{self._q(room)}/messages",
                          params={"dir": "b", "limit": str(limit)})
        return body.get("chunk", [])

    def get_event(self, room, event_id):
        return self._json("GET", f"/_matrix/client/v3/rooms/{self._q(room)}/event/{self._q(event_id)}")

    def joined_members(self, room):
        body = self._json("GET", f"/_matrix/client/v3/rooms/{self._q(room)}/joined_members")
        return {uid: (v or {}).get("display_name") for uid, v in (body.get("joined") or {}).items()}

    def send_event(self, room, etype, content, txn):
        body = self._json("PUT", f"/_matrix/client/v3/rooms/{self._q(room)}/send/{self._q(etype)}/{self._q(txn)}",
                          content, write=True, timeout=SEND_TIMEOUT)
        return body.get("event_id")

    def redact(self, room, event_id, txn):
        body = self._json("PUT", f"/_matrix/client/v3/rooms/{self._q(room)}/redact/{self._q(event_id)}/{self._q(txn)}",
                          {}, write=True, timeout=SEND_TIMEOUT)
        return body.get("event_id")

    def receipt(self, room, event_id):
        self._json("POST", f"/_matrix/client/v3/rooms/{self._q(room)}/receipt/m.read/{self._q(event_id)}", {},
                   write=True)

    def join(self, room):
        self._json("POST", f"/_matrix/client/v3/rooms/{self._q(room)}/join", {}, write=True)

    def create_dm(self, user):
        body = self._json("POST", "/_matrix/client/v3/createRoom",
                          {"is_direct": True, "invite": [user], "preset": "trusted_private_chat"}, write=True)
        return body["room_id"]

    def upload(self, data, mimetype, filename):
        body = self._json("POST", "/_matrix/media/v3/upload", params={"filename": filename}, raw=data,
                          ctype=mimetype, write=True, timeout=180)
        return body["content_uri"]

    def download(self, mxc):
        m = re.match(r"^mxc://([^/]+)/([^/?#]+)$", mxc or "")
        if not m:
            raise ApiError("the message has no downloadable file")
        return self._req("GET", f"/_matrix/client/v1/media/download/{self._q(m.group(1))}/{self._q(m.group(2))}",
                         timeout=180)

    def close(self):
        pass


def make_client(token=None):
    return Matrix(_homeserver(), token or _session()["token"])


# ---- small helpers --------------------------------------------------------------------------------
def parse_ts(value):
    """Seconds since the epoch from ISO 8601, epoch seconds or epoch milliseconds; None if junk."""
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{9,13}(\.\d+)?", text):
        n = float(text)
        return n / 1000.0 if n > 1e11 else n
    try:
        d = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def when(ms) -> str:
    """A Matrix timestamp (ms) as local time with the zone label."""
    if not ms:
        return "?"
    return dt.datetime.fromtimestamp(ms / 1000.0).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def clean(s, limit=600) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def clamp(n, top=MAX_ROWS) -> int:
    return max(1, min(int(n), top))


def _domain(user_id: str) -> str:
    return user_id.split(":", 1)[1] if ":" in user_id else ""


def _txn() -> str:
    return "ig-" + uuid.uuid4().hex


def _event_text(ev) -> str:
    c = ev.get("content") or {}
    if not c:
        return "[deleted]"
    mt = c.get("msgtype", "")
    body = c.get("body") or ""
    if mt in ("m.image", "m.video", "m.audio", "m.file"):
        return f"[{mt[2:]}] {body}".strip()
    return body


# ---- money lock and gate (shared with the other plug-in channels) ----------------------------
def _money_hits(text: str):
    here = os.path.dirname(os.path.abspath(__file__))  # kit: code path
    return _cs().money_hits(text, here)  # kit: code path


def _gate_mode_fallback() -> str:
    return _cs().gate_mode_fallback()


def _gate_check(args, *, target, text, scope, thread, dry_run, source, labels, is_group=None):
    cs = _cs()
    kw = dict(target=target, text=text, scope=scope, thread=thread, dry_run=dry_run, source=source)
    params = inspect.signature(cs.gate_check).parameters
    if "labels" in params:          # an older channel_support.py has no labels / is_group yet
        kw["labels"] = labels
    if "is_group" in params:
        kw["is_group"] = is_group
    return cs.gate_check(_comms_module, _gate_mode_fallback, "instagram", args.money_ok, **kw)


def _gate_begin(ctx):
    return _cs().gate_begin(ctx)


def _gate_finish(ctx, op, status, receipts=()):
    _cs().gate_finish(ctx, op, status, receipts)


def _money_lock(args, text, what="the text"):
    hits = _money_hits(text)
    if hits is None:
        print(f"  MONEY LOCK: the money check could not run on {what}, so treat it as money. Show the exact "
              "text, get a fresh yes, then add --money-ok.")
    elif hits:
        print(f"  MONEY LOCK: {what} mentions {', '.join(sorted(set(hits)))}. Show the exact text, get a "
              "fresh yes, then add --money-ok.")
    locked = hits is None or bool(hits)
    if locked and args.yes and not args.money_ok:
        print("NOT SENT: money lock. Show the exact text, get a fresh yes, then add --money-ok.")
        raise SystemExit(MONEY_LOCK_EXIT)


# ---- the room view (portals) ------------------------------------------------------------------------
class View:
    """Joined rooms from one /sync, with state applied in order (state block, then timeline state)."""

    def __init__(self, mx, me, bot, mgmt, timeline_limit=5, accept_invites=True):
        data = mx.sync(timeline_limit)
        rooms = data.get("rooms") or {}
        self.me, self.bot, self.mgmt = me, bot, mgmt
        self.rooms = {}
        for room_id, rd in (rooms.get("join") or {}).items():
            state = {}
            timeline = (rd.get("timeline") or {}).get("events") or []
            for ev in list((rd.get("state") or {}).get("events") or []) + [e for e in timeline if "state_key" in e]:
                state[(ev.get("type"), ev.get("state_key"))] = ev
            self.rooms[room_id] = {"state": state, "timeline": timeline,
                                   "unread": (rd.get("unread_notifications") or {}).get("notification_count", 0)}
        self.joined_now = []
        if accept_invites:
            for room_id, rd in (rooms.get("invite") or {}).items():
                evs = (rd.get("invite_state") or {}).get("events") or []
                mine = [e for e in evs if e.get("type") == "m.room.member" and e.get("state_key") == me
                        and (e.get("content") or {}).get("membership") == "invite"]
                if mine and mine[-1].get("sender") == bot:
                    try:
                        mx.join(room_id)
                        self.joined_now.append(room_id)
                    except (ApiError, UncertainError):
                        continue

    def is_portal(self, room_id) -> bool:
        r = self.rooms.get(room_id)
        if r is None or room_id == self.mgmt:
            return False
        for (etype, _), ev in r["state"].items():
            if etype in BRIDGE_STATE_TYPES and ((ev.get("content") or {}).get("protocol") or {}).get("id") == "instagram":
                return True
        return False

    def members(self, room_id):
        r = self.rooms.get(room_id) or {"state": {}}
        out = {}
        for (etype, key), ev in r["state"].items():
            if etype == "m.room.member" and (ev.get("content") or {}).get("membership") == "join":
                out[key] = (ev.get("content") or {}).get("displayname") or key
        return out

    def ghosts(self, room_id, own_ghost=None):
        return [u for u in self.members(room_id) if u not in (self.me, self.bot, own_ghost)]

    def name(self, room_id) -> str:
        r = self.rooms.get(room_id) or {"state": {}}
        ev = r["state"].get(("m.room.name", ""))
        name = ((ev or {}).get("content") or {}).get("name")
        if name:
            return name
        others = [n for u, n in self.members(room_id).items() if u not in (self.me, self.bot)]
        return ", ".join(sorted(others)) or room_id

    def portals(self):
        return [r for r in self.rooms if self.is_portal(r)]

    def last_ts(self, room_id) -> int:
        tl = (self.rooms.get(room_id) or {}).get("timeline") or []
        return max([e.get("origin_server_ts", 0) for e in tl if e.get("type") == "m.room.message"] or [0])


def resolve_chat(view: View, text: str) -> str:
    """A portal room id from a room id or a chat name. Exits 1 (unknown / not a portal) or 2 (two match)."""
    text = (text or "").strip()
    if text.startswith("!"):
        if text in view.rooms and view.is_portal(text):
            return text
        if text in view.rooms:
            die(1, f"NOT SENT: {text} is not an Instagram chat (it is not a mautrix-instagram portal).")
        die(1, f"No joined room {text}. Run `chats` to see the Instagram chats.")
    portals = view.portals()
    low = text.lower()
    exact = [r for r in portals if view.name(r).lower() == low]
    if len(exact) == 1:
        return exact[0]
    part = exact or [r for r in portals if low and low in view.name(r).lower()]
    if len(part) == 1:
        return part[0]
    if not part:
        die(1, f"No Instagram chat matches '{text}'. Run `chats` to see them.")
    lines = "\n".join(f"  chat={r} | {view.name(r)}" for r in part)
    die(2, f"'{text}' matches {len(part)} chats. Use the chat id:\n{lines}")


def _is_group(c, room) -> bool:
    return len(c.view.ghosts(room, _own_ghost(c.bot))) > 1


# ---- the bridge bot: login state ----------------------------------------------------------------------
def _bot_replies_after(mx, mgmt, bot, event_id, wait_s):
    """Bot messages that came after event_id, oldest first. Waits up to wait_s for at least one."""
    deadline = time.monotonic() + wait_s
    while True:
        found = []
        for ev in mx.messages(mgmt, 20):           # newest first
            if ev.get("event_id") == event_id:
                break
            if ev.get("sender") == bot and ev.get("type") == "m.room.message":
                found.append(ev)
        if found or time.monotonic() >= deadline:
            return list(reversed(found))
        time.sleep(POLL_S)


def _bot_command(mx, mgmt, bot, command, wait_s=None):
    eid = mx.send_event(mgmt, "m.room.message", {"msgtype": "m.text", "body": command}, _txn())
    return _bot_replies_after(mx, mgmt, bot, eid, BOT_REPLY_WAIT_S if wait_s is None else wait_s)


def login_states(mx, mgmt, bot):
    """[(login_id, name, state)], [] when not logged in, None when the bot did not answer."""
    try:
        replies = _bot_command(mx, mgmt, bot, "list-logins")
    except (ApiError, UncertainError):
        return None
    if not replies:
        return None
    text = "\n".join((r.get("content") or {}).get("body", "") for r in replies)
    rows = LOGIN_LINE.findall(text)
    if rows:
        _write_private(os.path.join(_state_dir(), "login.json"),
                       {"login_id": rows[0][0], "name": rows[0][1], "state": rows[0][2], "checked": time.time()})
    return rows


def _cached_login_id():
    return (_read_json(os.path.join(_state_dir(), "login.json"), {}) or {}).get("login_id")


def _own_ghost(bot, login_id=None):
    lid = login_id or _cached_login_id()
    return f"@instagram_{lid}:{_domain(bot)}" if lid else None


ADVICE = {
    "BAD_CREDENTIALS": "Instagram ended the session or wants a check. Open the Instagram app on your phone, "
                       "clear any warning or security check, then run `instagram.py login`.",
    "LOGGED_OUT": "The bridge is logged out. Run `instagram.py login`.",
    "TRANSIENT_DISCONNECT": "The bridge lost its connection for a moment. Try again in a few minutes.",
    "CONNECTING": "The bridge is still connecting. Try again in a minute.",
    "UNKNOWN_ERROR": "The bridge has an error. Run `instagram.py status`, and look at the bridge log.",
}


def check_bridge(mx, mgmt, bot, *, required):
    """The login id when the login is healthy. When required, anything else exits 3 (nothing sent)."""
    rows = login_states(mx, mgmt, bot)
    if rows is None:
        msg = "the bridge bot did not answer `list-logins`. Is the bridge running?"
    elif not rows:
        msg = "there is no Instagram login on the bridge. Run `instagram.py login`."
    else:
        login_id, name, state = rows[0]
        if state in OK_STATES:
            print(f"  Bridge: logged in as {name} ({state}).")
            return login_id
        msg = f"the Instagram login is {state}. {ADVICE.get(state, '')}".strip()
    if required:
        print(f"NOT SENT: {msg}")
        raise SystemExit(NOT_READY_EXIT)
    print(f"  Bridge: {msg} (a real send would stop here.)")
    return rows[0][0] if rows else None


# ---- send hold and pacing ---------------------------------------------------------------------------------
def _hold_path():
    return os.path.join(_state_dir(), "hold.json")


def _hold():
    h = _read_json(_hold_path(), None)
    if isinstance(h, dict) and float(h.get("until", 0)) > time.time():
        return h
    return None


def _set_hold(reason):
    until = time.time() + HOLD_HOURS * 3600
    _write_private(_hold_path(), {"until": until, "reason": clean(reason, 300), "set": time.time()})
    return until


def _sends_path():
    return os.path.join(_state_dir(), "sends.jsonl")


def _send_rows(window_s=86400):
    """[(ts, chat)] for the writes in the window, oldest first."""
    now, out = time.time(), []
    try:
        with open(_sends_path(), encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    t = float(row.get("ts", 0))
                except (ValueError, AttributeError):
                    continue
                if now - t <= window_s:
                    out.append((t, row.get("chat")))
    except OSError:
        pass
    return sorted(out, key=lambda r: r[0])


def _recent_sends(window_s=86400):
    return [t for t, _ in _send_rows(window_s)]


def _record_send(verb, chat=None):
    with open(_sends_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), "verb": verb, "chat": chat}) + "\n")
    os.chmod(_sends_path(), 0o600)


def _in_burst(rows, chat) -> bool:
    """True when the last write went to this same chat and fewer than BURST_WRITES writes went to it
    in the last BURST_WINDOW_S: the next bubble of one reply, not a new send."""
    if not rows or not chat or rows[-1][1] != chat:
        return False
    recent = [t for t, c in rows if c == chat and time.time() - t <= BURST_WINDOW_S]
    return len(recent) < BURST_WRITES


def check_guards(*, required, paced=True, chat=None):
    """The hold, the daily cap and the minimum gap. When required, a hit exits 3. A few quick bubbles to
    the same chat do not wait for the gap (_in_burst)."""
    problems = []
    h = _hold()
    if h:
        problems.append(f"SEND HOLD until {when(h['until'] * 1000)}: Instagram refused a send with "
                        f"'{h.get('reason', '')}'. Open the Instagram app and check for a warning. When it is "
                        "clear, run `instagram.py clear-hold --yes`.")
    if paced:
        rows = _send_rows()
        sends = [t for t, _ in rows]
        cap = _daily_cap()
        gap = _min_gap()
        if len(sends) >= cap:
            problems.append(f"the daily cap is reached ({len(sends)} writes in 24 h, cap {cap}, "
                            "setting instagram.daily_send_cap).")
        if sends and gap > 0 and time.time() - max(sends) < gap and not _in_burst(rows, chat):
            wait = int(gap - (time.time() - max(sends))) + 1
            problems.append(f"the minimum gap between writes is {gap} s; wait {wait} s "
                            "(setting instagram.min_send_gap_seconds).")
    for p in problems:
        if required:
            print(f"NOT SENT: {p}")
        else:
            print(f"  Guard: {p} (a real send would stop here.)")
    if problems and required:
        raise SystemExit(NOT_READY_EXIT)


# ---- the write itself -----------------------------------------------------------------------------------
def wait_status(mx, room, event_id):
    """('SUCCESS'|'FAIL_*'|None, message) from the bridge's send status for event_id."""
    deadline = time.monotonic() + STATUS_WAIT_S
    while True:
        latest = None
        for ev in mx.messages(room, 30):          # newest first
            if ev.get("type") != STATUS_TYPE:
                continue
            c = ev.get("content") or {}
            if ((c.get("m.relates_to") or {}).get("event_id")) == event_id:
                latest = c
                break
        if latest and latest.get("status") in FINAL_STATUSES:
            return latest.get("status"), latest.get("message") or latest.get("error") or latest.get("reason") or ""
        if time.monotonic() >= deadline:
            return None, ""
        time.sleep(POLL_S)


def deliver(args, mx, ctx, room, verb, send_fn, *, text, archive_text):
    """Run one write and read the bridge status. Exits on every outcome but SUCCESS."""
    op = _gate_begin(ctx)
    try:
        eid = send_fn()
    except UncertainError:
        _gate_finish(ctx, op, "uncertain")
        _record_send(verb, room)
        print("UNCERTAIN SEND: the Matrix server did not answer, so it may have gone out. Do not resend. "
              "Open the chat (history) and check first.")
        raise SystemExit(UNCERTAIN_EXIT)
    except ApiError as exc:
        _gate_finish(ctx, op, "failed_before_send")
        print(f"NOT SENT: {exc}. Nothing was archived.")
        raise SystemExit(1)
    except BaseException:
        _gate_finish(ctx, op, "uncertain")
        raise
    _record_send(verb, room)
    status, message = wait_status(mx, room, eid)
    if status == "SUCCESS":
        _gate_finish(ctx, op, "sent", [eid] if eid else [])
        C = _comms_module("commslib")
        if C is not None:
            C.log_event("instagram", "send", room, text=archive_text, message_id=eid, scope=room, verb=verb,
                        hook_covers=False, command=" ".join(sys.argv))
        print(f"Sent. id={eid} chat={room}")
        return eid
    if status is None:
        _gate_finish(ctx, op, "uncertain")
        print(f"UNCERTAIN SEND: the bridge gave no delivery status in {int(STATUS_WAIT_S)} s (id={eid}). "
              "Do not resend. Check the chat with `history` first.")
        raise SystemExit(UNCERTAIN_EXIT)
    _gate_finish(ctx, op, "failed_before_send")
    print(f"NOT SENT: Instagram refused it ({status}): {message or 'no reason given'}. Nothing was archived.")
    if HOLD_WORDS.search(message or ""):
        until = _set_hold(message)
        print(f"SEND HOLD set until {when(until * 1000)}: that refusal points to an account limit. Every "
              "write stops until then. Open the Instagram app, check for a warning, then "
              "`clear-hold --yes` if all is fine.")
    raise SystemExit(1)


class Ctx:
    """What every verb needs: the client, ids, and the room view."""

    def __init__(self, args, invites=True, timeline_limit=5):
        self.session = _session()
        self.mx = make_client(self.session["token"])
        self.bot = _bot_user()
        self.mgmt = _mgmt_room(self.session)
        self.me = self.mx.whoami()
        self.view = View(self.mx, self.me, self.bot, self.mgmt, timeline_limit=timeline_limit,
                         accept_invites=invites)


def _preview(args, verb, view, room, lines):
    print(f"{'About to ' + verb if args.yes else 'DRY RUN, nothing sent'}. {verb} on Instagram:")
    print(f"  Chat:    {view.name(room)} (chat={room})")
    for line in lines:
        print(f"  {line}")


def _since(args, text_bearing=True):
    if not text_bearing:
        return None
    if args.yes and not args.since:
        die(2, "NOT SENT: --since is required with --yes (the time of the newest message you read; "
               "`history` prints it).")
    if args.since:
        t = parse_ts(args.since)
        if t is None:
            die(2, "NOT SENT: --since is not a time (use ISO 8601, epoch seconds or epoch ms).")
        return t
    return None


def _stale_check(mx, room, since, mine):
    if since is None:
        return
    for ev in mx.messages(room, RECENT_FOR_STALE_CHECK):
        if ev.get("type") != "m.room.message" or ev.get("sender") in mine:
            continue
        ts = ev.get("origin_server_ts")
        if ts is None or ts / 1000.0 > since:
            print(f"NOT SENT: the chat has a message newer than --since ({when(ts)}): "
                  f"\"{clean(_event_text(ev), 120)}\". Read it again, then redo the send.")
            raise SystemExit(STALE_EXIT)


def _target_event(mx, room, event_id):
    try:
        ev = mx.get_event(room, event_id)
    except ApiError:
        die(1, f"NOT SENT: message {event_id} is not in that chat.")
    if ev.get("room_id") not in (None, room):
        die(1, f"NOT SENT: message {event_id} is not in that chat.")
    return ev


def _write_preflight(args, c: Ctx, room, *, paced=True):
    """Guards for a write. Returns the set of our own ids (this Matrix user + our Instagram ghost)."""
    login_id = check_bridge(c.mx, c.mgmt, c.bot, required=args.yes)
    check_guards(required=args.yes, paced=paced, chat=room)
    return {c.me} | ({_own_ghost(c.bot, login_id)} - {None})


def _text_write(args, verb, content_fn, *, reply_to=None):
    text = args.text
    if not text.strip():
        die(1, "NOT SENT: the message is empty.")
    since = _since(args)
    c = Ctx(args)
    room = resolve_chat(c.view, args.chat)
    if reply_to:
        _target_event(c.mx, room, reply_to)
    _preview(args, verb, c.view, room, ([f"Reply to: {reply_to}"] if reply_to else []) + ["Words:"] +
             [f"  {line}" for line in text.strip().splitlines()])
    mine = _write_preflight(args, c, room)
    _stale_check(c.mx, room, since, mine)
    _money_lock(args, text)
    ctx = _gate_check(args, target=room, text=text, scope=room, thread=str(reply_to or ""),
                      dry_run=not args.yes, source=f"instagram.py {verb}",
                      labels=[c.view.name(room), args.chat],
                      is_group=_is_group(c, room))
    if not args.yes:
        print("Add --yes to send (with --since, the time of the newest message you read).")
        return
    content = content_fn(text)
    deliver(args, c.mx, ctx, room, verb, lambda: c.mx.send_event(room, "m.room.message", content, _txn()),
            text=text, archive_text=text)


# ---- write verbs --------------------------------------------------------------------------------------------
def cmd_send(args):
    _text_write(args, "send", lambda text: {"msgtype": "m.text", "body": text})


def cmd_reply(args):
    _text_write(args, "reply", lambda text: {"msgtype": "m.text", "body": text,
                                             "m.relates_to": {"m.in_reply_to": {"event_id": args.event_id}}},
                reply_to=args.event_id)


def cmd_send_media(args):
    path = os.path.abspath(os.path.expanduser(args.file))
    if not os.path.isfile(path):
        die(1, f"NOT SENT: no file at {path}.")
    since = _since(args)
    c = Ctx(args)
    room = resolve_chat(c.view, args.chat)
    name = os.path.basename(path)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    size = os.path.getsize(path)
    msgtype = {"image": "m.image", "video": "m.video", "audio": "m.audio"}.get(mime.split("/")[0], "m.file")
    caption = (args.caption or "").strip()
    _preview(args, "send-media", c.view, room, [f"File:    {path} ({mime}, {size} bytes, {msgtype})"] +
             ([f"Caption: {caption}"] if caption else []))
    mine = _write_preflight(args, c, room)
    _stale_check(c.mx, room, since, mine)
    gate_text = caption or f"[file] {name}"
    _money_lock(args, gate_text)
    ctx = _gate_check(args, target=room, text=gate_text, scope=room, thread="", dry_run=not args.yes,
                      source="instagram.py send-media", labels=[c.view.name(room), args.chat],
                      is_group=_is_group(c, room))
    if not args.yes:
        print("Add --yes to send (with --since).")
        return
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        mxc = c.mx.upload(data, mime, name)
    except (ApiError, UncertainError) as exc:
        die(1, f"NOT SENT: the upload failed ({exc}). Nothing was sent to Instagram.")
    content = {"msgtype": msgtype, "body": caption or name, "filename": name, "url": mxc,
               "info": {"mimetype": mime, "size": size}}
    deliver(args, c.mx, ctx, room, "send-media", lambda: c.mx.send_event(room, "m.room.message", content, _txn()),
            text=gate_text, archive_text=gate_text)


def cmd_react(args):
    c = Ctx(args)
    room = resolve_chat(c.view, args.chat)
    target = _target_event(c.mx, room, args.event_id)
    _preview(args, "react", c.view, room, [f"Message: {clean(_event_text(target), 120)} ({args.event_id})",
                                            f"Emoji:   {args.emoji}"])
    _write_preflight(args, c, room)
    _money_lock(args, _event_text(target), "the message you react to")
    ctx = _gate_check(args, target=room, text=args.emoji, scope=room, thread=args.event_id, dry_run=not args.yes,
                      source="instagram.py react", labels=[c.view.name(room), args.chat],
                      is_group=_is_group(c, room))
    if not args.yes:
        print("Add --yes to react.")
        return
    content = {"m.relates_to": {"rel_type": "m.annotation", "event_id": args.event_id, "key": args.emoji}}
    deliver(args, c.mx, ctx, room, "react", lambda: c.mx.send_event(room, "m.reaction", content, _txn()),
            text=args.emoji, archive_text=f"[reaction {args.emoji}] to {args.event_id}")


def _own_target(c, room, event_id):
    target = _target_event(c.mx, room, event_id)
    if target.get("sender") != c.me:
        die(1, f"NOT SENT: message {event_id} is not yours (sent by {target.get('sender')}). "
               "Only messages sent from this tool can be edited or deleted here.")
    return target


def cmd_edit(args):
    if not args.text.strip():
        die(1, "NOT SENT: the new text is empty.")
    c = Ctx(args)
    room = resolve_chat(c.view, args.chat)
    target = _own_target(c, room, args.event_id)
    _preview(args, "edit", c.view, room, [f"Old:     {clean(_event_text(target), 200)}", f"New:     {args.text}"])
    _write_preflight(args, c, room)
    _money_lock(args, args.text)
    ctx = _gate_check(args, target=room, text=args.text, scope=room, thread=args.event_id, dry_run=not args.yes,
                      source="instagram.py edit", labels=[c.view.name(room), args.chat],
                      is_group=_is_group(c, room))
    if not args.yes:
        print("Add --yes to edit.")
        return
    content = {"msgtype": "m.text", "body": "* " + args.text,
               "m.new_content": {"msgtype": "m.text", "body": args.text},
               "m.relates_to": {"rel_type": "m.replace", "event_id": args.event_id}}
    deliver(args, c.mx, ctx, room, "edit", lambda: c.mx.send_event(room, "m.room.message", content, _txn()),
            text=args.text, archive_text=args.text)


def cmd_delete(args):
    c = Ctx(args)
    room = resolve_chat(c.view, args.chat)
    target = _own_target(c, room, args.event_id)
    _preview(args, "delete (unsend)", c.view, room, [f"Message: {clean(_event_text(target), 200)}"])
    _write_preflight(args, c, room)
    if not args.yes:
        print("Add --yes to unsend it for everyone in the chat.")
        return
    deliver(args, c.mx, None, room, "delete", lambda: c.mx.redact(room, args.event_id, _txn()),
            text="", archive_text=f"[deleted {args.event_id}]")


def cmd_mark_read(args):
    c = Ctx(args)
    room = resolve_chat(c.view, args.chat)
    newest = next((e for e in c.mx.messages(room, 20) if e.get("type") == "m.room.message"), None)
    if newest is None:
        print("Nothing to mark read.")
        return
    if not args.yes:
        print(f"DRY RUN, nothing changed. Would mark {c.view.name(room)} (chat={room}) read up to "
              f"{newest['event_id']}. Instagram then shows 'Seen' to the other person. Add --yes.")
        return
    check_bridge(c.mx, c.mgmt, c.bot, required=True)
    check_guards(required=True, paced=False)
    try:
        c.mx.receipt(room, newest["event_id"])
    except UncertainError:
        print("UNCERTAIN: the Matrix server did not answer. Marking read is safe to redo.")
        raise SystemExit(UNCERTAIN_EXIT)
    except ApiError as exc:
        die(1, f"FAILED: {exc}.")
    print(f"Marked {c.view.name(room)} (chat={room}) read.")


def cmd_clear_hold(args):
    h = _hold()
    if not h:
        print("No send hold is set.")
        return
    if not args.yes:
        print(f"DRY RUN. The hold runs until {when(h['until'] * 1000)} (reason: {h.get('reason')}). Check the "
              "Instagram app for a warning first. Add --yes to clear it.")
        return
    os.remove(_hold_path())
    print("Send hold cleared.")


# ---- read verbs ----------------------------------------------------------------------------------------------
def _names_for(c, room):
    try:
        names = c.mx.joined_members(room)
    except ApiError:
        names = {}
    names[c.me] = "me"
    ghost = _own_ghost(c.bot)
    if ghost:
        names[ghost] = "me (phone)"
    return names


def _render(events, names):
    """m.room.message events (oldest first) with edits folded in."""
    edits = {}
    for ev in events:
        rel = ((ev.get("content") or {}).get("m.relates_to") or {})
        if rel.get("rel_type") == "m.replace":
            edits[rel.get("event_id")] = (ev.get("content") or {}).get("m.new_content") or {}
    rows = []
    for ev in events:
        if ev.get("type") != "m.room.message":
            continue
        c = ev.get("content") or {}
        if (c.get("m.relates_to") or {}).get("rel_type") == "m.replace":
            continue
        text = _event_text(ev)
        if ev.get("event_id") in edits:
            text = (edits[ev["event_id"]].get("body") or text) + " (edited)"
        reply = ((c.get("m.relates_to") or {}).get("m.in_reply_to") or {}).get("event_id")
        who = names.get(ev.get("sender"), ev.get("sender"))
        rows.append((ev.get("origin_server_ts", 0), f"[{when(ev.get('origin_server_ts'))}] {who}: {clean(text)}"
                     f"{'  (reply to ' + reply + ')' if reply else ''}  (id={ev.get('event_id')})"))
    return rows


def _message_rows(events, names, mine):
    """m.room.message events (oldest first) as dicts, edits folded in: id, ts (ms), me, who, text, reply_to."""
    edits = {}
    for ev in events:
        rel = (ev.get("content") or {}).get("m.relates_to") or {}
        if rel.get("rel_type") == "m.replace":
            edits[rel.get("event_id")] = ((ev.get("content") or {}).get("m.new_content") or {}).get("body")
    out = []
    for ev in events:
        content = ev.get("content") or {}
        if ev.get("type") != "m.room.message" or (content.get("m.relates_to") or {}).get("rel_type") == "m.replace":
            continue
        text = edits.get(ev.get("event_id")) or _event_text(ev)
        out.append({"id": ev.get("event_id"), "ts": ev.get("origin_server_ts") or 0,
                    "me": ev.get("sender") in mine, "who": names.get(ev.get("sender"), ev.get("sender")),
                    "text": text,
                    "reply_to": ((content.get("m.relates_to") or {}).get("m.in_reply_to") or {}).get("event_id")})
    return out


def cmd_chats(args):
    c = Ctx(args)
    ghost = _own_ghost(c.bot)
    rows = sorted(c.view.portals(), key=c.view.last_ts, reverse=True)[: args.n]
    if args.json:
        print(json.dumps([{"chat": r, "name": c.view.name(r), "unread": c.view.rooms[r]["unread"],
                           "last_ts": c.view.last_ts(r), "group": len(c.view.ghosts(r, ghost)) > 1}
                          for r in rows], ensure_ascii=False))
        return
    for r in rows:
        kind = "group" if len(c.view.ghosts(r, ghost)) > 1 else "dm"
        print(f"chat={r} unread={c.view.rooms[r]['unread']} last={when(c.view.last_ts(r))} {kind} | "
              f"{clean(c.view.name(r), 80)}")
    if c.view.joined_now:
        print(f"(joined {len(c.view.joined_now)} new chat(s) the bridge invited us to)")
    print(f"({len(rows)} Instagram chats)")


def cmd_unread(args):
    c = Ctx(args)
    shown = 0
    for r in sorted(c.view.portals(), key=c.view.last_ts, reverse=True):
        count = c.view.rooms[r]["unread"]
        if not count:
            continue
        shown += 1
        if shown > args.n:
            break
        print(f"== {clean(c.view.name(r), 80)} (chat={r}, {count} unread)")
        names = _names_for(c, r)
        events = list(reversed(c.mx.messages(r, max(count, 1) + 10)))
        mine = {c.me, _own_ghost(c.bot)}
        incoming = [e for e in events if e.get("type") == "m.room.message" and e.get("sender") not in mine]
        for _, line in _render(incoming[-count:], names):
            print("  " + line)
    if not shown:
        print("No unread Instagram chats.")


def cmd_history(args):
    c = Ctx(args)
    room = resolve_chat(c.view, args.chat)
    events = list(reversed(c.mx.messages(room, args.n)))
    names = _names_for(c, room)
    if args.json:
        mine = {c.me, _own_ghost(c.bot)} - {None}
        print(json.dumps(_message_rows(events, names, mine), ensure_ascii=False))
        return
    rows = _render(events, names)
    print(f"== {c.view.name(room)} (chat={room})")
    for _, line in rows:
        print(line)
    newest = max((ts for ts, _ in rows), default=0)
    print(f"({len(rows)} messages)" + (f" Newest: {when(newest)}. For a send use --since {newest}"
                                       if newest else ""))


def cmd_find(args):
    c = Ctx(args)
    rooms = [resolve_chat(c.view, args.chat)] if args.chat else c.view.portals()
    needle = args.text.lower()
    hits = 0
    for r in rooms:
        events = list(reversed(c.mx.messages(r, args.n)))
        matched = [e for e in events if e.get("type") == "m.room.message" and needle in _event_text(e).lower()]
        if not matched:
            continue
        print(f"== {clean(c.view.name(r), 80)} (chat={r})")
        for _, line in _render(matched, _names_for(c, r)):
            hits += 1
            print("  " + line)
    print(f"({hits} found)")


def cmd_download(args):
    c = Ctx(args)
    room = resolve_chat(c.view, args.chat)
    ev = _target_event(c.mx, room, args.event_id)
    content = ev.get("content") or {}
    url = content.get("url")
    if not url:
        die(1, "That message has no file.")
    name = os.path.basename(content.get("filename") or content.get("body") or "instagram-file") or "instagram-file"
    name = re.sub(r"[^\w.\- ]", "_", name)
    out_dir = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    try:
        data = c.mx.download(url)
    except ApiError as exc:
        die(1, f"FAILED: {exc}.")
    with open(path, "wb") as fh:
        fh.write(data)
    print(f"Saved {len(data)} bytes to {path}")


# ---- setup, status, login -------------------------------------------------------------------------------
def cmd_setup(args):
    """Write the session: a Matrix token (from a file, never argv) and the bridge bot room."""
    raw = open(os.path.expanduser(args.token_file), encoding="utf-8").read().strip()
    try:
        token = json.loads(raw).get("token") or json.loads(raw).get("access_token")
    except (ValueError, AttributeError):
        token = raw
    if not token:
        die(2, "No token in that file.")
    mx = make_client(token)
    bot = _bot_user()
    me = mx.whoami()
    old = _read_json(_session_path(), {}) or {}
    room = old.get("mgmt_room")
    if not room:
        room = mx.create_dm(bot)
    _write_private(_session_path(), {"token": token, "mgmt_room": room, "user": me})
    print(f"Session saved for {me} in {_session_path()} (mode 600). Bridge bot room: {room}")


def cmd_status(args):
    s = _read_json(_session_path(), None)
    tunnel = _open_tunnel(_conf("instagram.tunnel_homeserver", ""))
    print(f"homeserver: {tunnel + ' (tunnel open)' if tunnel else _conf('instagram.homeserver') or 'NOT SET'}")
    print(f"bot_user:   {_conf('instagram.bot_user') or 'NOT SET'}")
    print(f"session:    {'found' if s and s.get('token') else 'NOT FOUND (run setup)'} ({_session_path()})")
    h = _hold()
    print(f"send hold:  {'until ' + when(h['until'] * 1000) + ' - ' + str(h.get('reason')) if h else 'none'}")
    print(f"writes in the last 24 h: {len(_recent_sends())} (cap "
          f"{_daily_cap()})")
    if s and s.get("token") and s.get("mgmt_room"):
        mx = make_client(s["token"])
        rows = login_states(mx, s["mgmt_room"], _bot_user())
        if rows is None:
            print("bridge:     the bot did not answer (is the bridge running?)")
        elif not rows:
            print("bridge:     no Instagram login (run login)")
        for lid, name, state in rows or []:
            print(f"bridge:     login {lid} ({name}) {state}")


def _ask(prompt):
    return input(prompt + " ").strip()


def _ask_hidden(prompt):
    return getpass.getpass(prompt + " (hidden) ")


def cmd_login(args):
    """Log in through the bridge bot. Only you type the answers; a password is never shown or saved,
    and the bridge deletes the message that holds it."""
    s = _session()
    mx = make_client(s["token"])
    bot, mgmt = _bot_user(), _mgmt_room(s)
    print(f"Logging in to Instagram through the bridge ({args.flow} flow). Type `cancel` at any prompt to stop.")
    last = mx.send_event(mgmt, "m.room.message", {"msgtype": "m.text", "body": f"login {args.flow}"}, _txn())
    while True:
        replies = _bot_replies_after(mx, mgmt, bot, last, LOGIN_WAIT_S)
        if not replies:
            print(f"No answer from the bridge in {int(LOGIN_WAIT_S // 60)} min. Run `status`. If it is not "
                  "logged in, run `login` again.")
            raise SystemExit(1)
        answer_for = None
        for ev in replies:
            body = (ev.get("content") or {}).get("body", "")
            print(f"bridge: {body}")
            if re.search(r"Logged in as|Successfully logged in", body):
                print("Logged in. Next: `instagram.py status`, then `chats`.")
                return
            if LOGIN_FAIL.match(body):
                mx.send_event(mgmt, "m.room.message", {"msgtype": "m.text", "body": "cancel"}, _txn())
                print("Login stopped. Nothing is saved. Fix what the bridge said, then run `login` again.")
                raise SystemExit(1)
            if re.search(r"^Login URL:", body, re.M):
                print("Instagram wants a browser check this tool cannot finish. Open the Instagram app on your "
                      "phone, approve the new login (\"This was me\") or clear the check, wait 10 minutes, then "
                      "run `login` again.")
                mx.send_event(mgmt, "m.room.message", {"msgtype": "m.text", "body": "cancel"}, _txn())
                raise SystemExit(1)
            m = re.search(r"^Please enter your (.+)", body, re.M)
            if m:
                answer_for = m.group(1).splitlines()[0]
        if answer_for is None:
            last = replies[-1]["event_id"]
            continue
        secret = re.search(r"password|passcode|token|pin\b|secret", answer_for, re.I)
        value = (_ask_hidden if secret else _ask)(f"{answer_for}:")
        if value.strip().lower() == "cancel":
            mx.send_event(mgmt, "m.room.message", {"msgtype": "m.text", "body": "cancel"}, _txn())
            print("Cancelled.")
            raise SystemExit(1)
        last = mx.send_event(mgmt, "m.room.message", {"msgtype": "m.text", "body": value}, _txn())


# ---- CLI ------------------------------------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="instagram.py",
                                description="Read and send Instagram DMs through a mautrix-instagram Matrix bridge.")
    sub = p.add_subparsers(dest="command", required=True)

    def n_opt(sp, default):
        sp.add_argument("-n", type=int, default=default)

    def write_opts(sp, since=True):
        if since:
            sp.add_argument("--since", help="time of the newest message you read (ISO 8601, epoch s or ms)")
        sp.add_argument("--yes", action="store_true")
        sp.add_argument("--money-ok", action="store_true")

    sp = sub.add_parser("chats", help="list Instagram chats, newest first"); n_opt(sp, 30)
    sp.add_argument("--json", action="store_true"); sp.set_defaults(fn=cmd_chats)
    sp = sub.add_parser("unread", help="unread incoming messages, per chat"); n_opt(sp, 10); sp.set_defaults(fn=cmd_unread)
    sp = sub.add_parser("history", help="one chat, oldest first (sends no read receipt)"); sp.add_argument("chat")
    n_opt(sp, 30); sp.add_argument("--json", action="store_true"); sp.set_defaults(fn=cmd_history)
    sp = sub.add_parser("find", help="search recent messages"); sp.add_argument("text"); sp.add_argument("--chat")
    n_opt(sp, 100); sp.set_defaults(fn=cmd_find)
    sp = sub.add_parser("download", help="save a message's file"); sp.add_argument("chat"); sp.add_argument("event_id")
    sp.add_argument("--out", default="~/Downloads"); sp.set_defaults(fn=cmd_download)

    sp = sub.add_parser("send", help="send a text (dry run unless --yes)"); sp.add_argument("chat"); sp.add_argument("text")
    write_opts(sp); sp.set_defaults(fn=cmd_send)
    sp = sub.add_parser("reply", help="reply to one message (dry run unless --yes)"); sp.add_argument("chat")
    sp.add_argument("event_id"); sp.add_argument("text"); write_opts(sp); sp.set_defaults(fn=cmd_reply)
    sp = sub.add_parser("send-media", help="send a photo, video, voice or file (dry run unless --yes)")
    sp.add_argument("chat"); sp.add_argument("file"); sp.add_argument("--caption"); write_opts(sp)
    sp.set_defaults(fn=cmd_send_media)
    sp = sub.add_parser("react", help="react to one message (dry run unless --yes)"); sp.add_argument("chat")
    sp.add_argument("event_id"); sp.add_argument("emoji"); write_opts(sp, since=False); sp.set_defaults(fn=cmd_react)
    sp = sub.add_parser("edit", help="edit your own message (dry run unless --yes)"); sp.add_argument("chat")
    sp.add_argument("event_id"); sp.add_argument("text"); write_opts(sp, since=False); sp.set_defaults(fn=cmd_edit)
    sp = sub.add_parser("delete", help="unsend your own message (dry run unless --yes)"); sp.add_argument("chat")
    sp.add_argument("event_id"); sp.add_argument("--yes", action="store_true"); sp.set_defaults(fn=cmd_delete, money_ok=False)
    sp = sub.add_parser("mark-read", help="mark a chat read; Instagram shows Seen (dry run unless --yes)")
    sp.add_argument("chat"); sp.add_argument("--yes", action="store_true"); sp.set_defaults(fn=cmd_mark_read)

    sp = sub.add_parser("clear-hold", help="clear the send hold after you checked the Instagram app")
    sp.add_argument("--yes", action="store_true"); sp.set_defaults(fn=cmd_clear_hold)
    sp = sub.add_parser("status", help="settings, send hold, writes today, bridge login state")
    sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("setup", help="save the Matrix session (token from a file) and the bot room")
    sp.add_argument("--token-file", required=True); sp.set_defaults(fn=cmd_setup)
    sp = sub.add_parser("login", help="log in to Instagram through the bridge bot (interactive)")
    sp.add_argument("--flow", default="android", choices=["android", "instagram-password", "instagram"])
    sp.set_defaults(fn=cmd_login)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "n", None) is not None:
        args.n = clamp(args.n)
    if args.command not in ("status", "clear-hold"):
        _cs()
    try:
        args.fn(args)
    except (ApiError, UncertainError) as exc:
        die(1, f"FAILED: {exc}.")


if __name__ == "__main__":
    main()
