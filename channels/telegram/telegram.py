#!/usr/bin/env python3
"""Read and send on Telegram (a user account, MTProto) from one CLI.

MOCK ONLY. This code has never run against a real account. The Telethon wrapper below is written
from Telethon's documentation and is tested only through a fake client.

Verbs: chats, unread, history, find, send, reply, mark-read, login, status.
Telethon is an optional dependency. It is imported only when a verb needs it. When it is missing,
the tool prints how to install it and exits 2. The kit never ships it.

Secrets are never stored in the config. `telegram.api_id_command` and `telegram.api_hash_command`
are commands (lists, run without a shell) that print the value. `telegram.session_path` is the
session file Telethon keeps (treat that file as a password).

Exit codes: 0 ok, 1 refused or failed, 2 usage/setup problem, 4 UNCERTAIN send (do not resend),
5 a newer message exists (--since), 6 money lock, 7 outbound gate refused.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import os
import re
import subprocess
import sys

UNCERTAIN_EXIT = 4
STALE_EXIT = 5
MONEY_LOCK_EXIT = 6
GATE_DENY_EXIT = 7
RECENT_FOR_STALE_CHECK = 20     # newest messages re-read before a send
MAX_ROWS = 200
SEND_TIMEOUT = 45
READ_TIMEOUT = 60


class ApiError(RuntimeError):
    """A failed call. The message never holds a secret."""


class UncertainError(RuntimeError):
    """A send that may or may not have been delivered. Never retry."""


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
    """The support module. Without it the guards (money lock, gate) cannot run, so we stop."""
    if _CS is None:
        die(2, "channel_support.py was not found (looked in COMMS_KIT_CORE, ../../core and "
               "~/.claude/communications). Nothing was run.")
    return _CS


def _comms_module(name: str):
    """core/<name>.py loaded by exact path; None when it cannot load."""
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


def _real_run_key_cmd(cmd) -> str:
    """Run a secret command with no shell. Returns its output; raises ApiError with a secret-free line."""
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
        raise ApiError("the command must be a list of strings")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, shell=False)
    except FileNotFoundError:
        raise ApiError("the command was not found") from None
    except (subprocess.TimeoutExpired, OSError):
        raise ApiError("the command did not finish") from None
    value = (proc.stdout or "").strip()
    if proc.returncode != 0 or not value:
        raise ApiError(f"the command failed (exit {proc.returncode})")
    return value


_run_key_cmd = _real_run_key_cmd


def _read_credentials():
    """(api_id int, api_hash, session_path). Exits 2 with one plain line when anything is wrong."""
    id_cmd = _conf("telegram.api_id_command")
    hash_cmd = _conf("telegram.api_hash_command")
    session = _conf("telegram.session_path")
    for key, value in (("telegram.api_id_command", id_cmd), ("telegram.api_hash_command", hash_cmd),
                       ("telegram.session_path", session)):
        if not value:
            die(2, f"setup: set {key} in the comms-kit config (see config.example.json). "
                   "Run `telegram.py login` for the steps.")
    try:
        raw_id = _run_key_cmd(id_cmd)
        api_hash = _run_key_cmd(hash_cmd)
    except ApiError as exc:
        die(2, f"setup: key lookup failed: {exc}.")
    if not re.fullmatch(r"\d{1,12}", raw_id.strip()):
        die(2, "setup: the api_id command must print only the number from my.telegram.org.")
    return int(raw_id.strip()), api_hash, os.path.expanduser(str(session))


# ---- time and text helpers ------------------------------------------------------------------
def parse_ts(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 1e11 else float(value)
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return parse_ts(float(text))
    try:
        d = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def when(ts) -> str:
    if ts is None:
        return "unknown-time"
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(s, limit: int = 600) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= limit else s[: limit - 1] + "..."


def show(m: dict, full: bool = False) -> None:
    direction = "OUT" if m.get("out") else "IN"
    reply = f" reply_to={m['reply_to']}" if m.get("reply_to") else ""
    print(f"[{direction}] id={m.get('id')} chat={chat_label(m.get('chat'))} {when(m.get('ts'))}{reply}")
    print(f"  {clean(m.get('sender'), 80)}: {clean(m.get('text'), 2000 if full else 240)}")


def clamp(n, top=MAX_ROWS) -> int:
    return max(1, min(int(n), top))


# ---- the client interface (tests inject a fake of this) -------------------------------------
# A client has these methods. Each raises ApiError for a clean failure. `send` and `mark_read`
# raise UncertainError when the request may have left but no answer came back.
#   dialogs(limit)                    -> [{"id", "title", "unread": int, "ts": float|None}]
#   messages(chat, limit, search=None) -> [message dict], newest first. chat None = all chats.
#   resolve(chat)                     -> {"id", "title"}. A read step that runs BEFORE any send or
#                                        mark-read. It raises ApiError for a chat that does not exist,
#                                        so a mistyped name is "not sent", never "uncertain".
#   send(chat, text, reply_to=None)   -> {"id": int, "ts": float|None}
#   mark_read(chat)                   -> None
#   close()
# A message dict: {"id", "chat", "chat_title", "sender", "out", "ts", "text", "reply_to"}.

CHAT_HELP = ("write a chat as +12065550100 (a phone number), id:12345 or -100123 (a chat id from "
             "`chats`), @username, or me")


def parse_chat(text) -> str:
    """Check and normalise a chat name typed by the owner. Never guesses: bare digits are refused,
    because a phone number typed without + would otherwise go out as a chat id."""
    raw = str(text or "").strip()
    if raw == "me":
        return raw
    if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", raw):
        return raw
    if re.fullmatch(r"id:\d{1,20}", raw) or re.fullmatch(r"-\d{1,20}", raw):
        return raw
    if raw.startswith("+"):
        digits = re.sub(r"[ \-().]", "", raw[1:])
        if re.fullmatch(r"\d{7,15}", digits):
            return "+" + digits
    if re.fullmatch(r"\d+", raw):
        raise ValueError(f"'{raw}' is only digits, so it could be a phone number or a chat id: {CHAT_HELP}")
    raise ValueError(f"'{raw}' is not a chat name: {CHAT_HELP}")


def chat_ref(chat: str):
    """What Telethon gets for a chat that parse_chat accepted: an int for an id, text otherwise."""
    text = str(chat)
    if text.startswith("id:"):
        return int(text[3:])
    if re.fullmatch(r"-\d+", text):
        return int(text)
    return text


def chat_label(raw) -> str:
    """A chat id as Telegram reports it, written the way parse_chat accepts it."""
    text = str(raw)
    return "id:" + text if re.fullmatch(r"\d+", text) else text


def _person_name(sender) -> str:
    if sender is None:
        return ""
    parts = [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
    name = " ".join(p for p in parts if p)
    return name or getattr(sender, "title", None) or getattr(sender, "username", None) or ""


def message_to_dict(m, title=None) -> dict:
    """A Telethon message (or anything shaped like one) as our plain dict."""
    date = getattr(m, "date", None)
    ts = None
    if date is not None:
        if date.tzinfo is None:
            date = date.replace(tzinfo=dt.timezone.utc)
        ts = date.timestamp()
    reply = getattr(m, "reply_to", None)
    chat_obj = getattr(m, "chat", None)
    chat_title = title or getattr(chat_obj, "title", None) or _person_name(chat_obj) or ""
    return {"id": getattr(m, "id", None), "chat": str(getattr(m, "chat_id", "")),
            "chat_title": chat_title, "sender": _person_name(getattr(m, "sender", None)),
            "out": bool(getattr(m, "out", False)), "ts": ts,
            "text": getattr(m, "message", None) or "",
            "reply_to": getattr(reply, "reply_to_msg_id", None) if reply is not None else None}


class TelethonClient:
    """Synchronous wrapper over Telethon. NEVER RUN against a real account by the kit's tests."""

    def __init__(self, telethon, api_id: int, api_hash: str, session_path: str):
        import asyncio
        self._asyncio = asyncio
        self._errors = telethon.errors
        self._loop = asyncio.new_event_loop()
        self._client = telethon.TelegramClient(session_path, api_id, api_hash, loop=self._loop)
        self._entities = {}
        self._call(self._connect(), write=False, timeout=READ_TIMEOUT)

    async def _connect(self):
        # connect() never asks for a phone or a code. A missing login is an error, not a prompt.
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise ApiError("this session is not logged in. Run `telegram.py login` for the steps")

    def _call(self, coro, write: bool, timeout: float):
        try:
            return self._loop.run_until_complete(self._asyncio.wait_for(coro, timeout))
        except ApiError:
            raise
        except self._errors.RPCError as exc:        # Telegram answered with a refusal
            wait = getattr(exc, "seconds", None)
            extra = f"; wait {wait} seconds" if wait else ""
            raise ApiError(f"Telegram refused ({type(exc).__name__}{extra})") from None
        except Exception as exc:  # noqa: BLE001 - timeout, drop, anything else
            if write:
                raise UncertainError("no answer from Telegram") from None
            raise ApiError(f"could not reach Telegram ({type(exc).__name__})") from None

    def dialogs(self, limit):
        async def go():
            return [{"id": str(d.id), "title": d.name or "", "unread": int(d.unread_count or 0),
                     "ts": d.date.timestamp() if getattr(d, "date", None) else None}
                    for d in await self._client.get_dialogs(limit=limit)]
        return self._call(go(), False, READ_TIMEOUT)

    def messages(self, chat, limit, search=None):
        async def go():
            entity = chat_ref(chat) if chat is not None else None
            rows = await self._client.get_messages(entity, limit=limit, search=search)
            return [message_to_dict(m) for m in rows]
        return self._call(go(), False, READ_TIMEOUT)

    def resolve(self, chat):
        """Look the chat up. A READ: a name Telethon cannot find (ValueError, before any request
        leaves) is a clean ApiError here, so it can never read as an uncertain send."""
        async def go():
            return await self._client.get_entity(chat_ref(chat))
        try:
            entity = self._call(go(), False, READ_TIMEOUT)
        except ApiError as exc:
            raise ApiError(f"Telegram has no chat called {chat} ({exc})") from None
        self._entities[chat] = entity
        title = getattr(entity, "title", None) or _person_name(entity)
        return {"id": str(getattr(entity, "id", "")), "title": title}

    def _entity(self, chat):
        if chat not in self._entities:
            self.resolve(chat)
        return self._entities[chat]

    def send(self, chat, text, reply_to=None):
        entity = self._entity(chat)               # a read; fails cleanly before anything is sent

        async def go():
            sent = await self._client.send_message(entity, text, reply_to=reply_to)
            return {"id": sent.id, "ts": sent.date.timestamp() if getattr(sent, "date", None) else None}
        return self._call(go(), True, SEND_TIMEOUT)

    def mark_read(self, chat):
        entity = self._entity(chat)

        async def go():
            await self._client.send_read_acknowledge(entity)
        return self._call(go(), True, SEND_TIMEOUT)

    def close(self):
        try:
            self._loop.run_until_complete(self._client.disconnect())
        except Exception:  # noqa: BLE001 - closing must never hide the real result
            pass
        self._loop.close()


INSTALL_LINE = ("Telethon is not installed. Install it with: pip install telethon "
                "(the kit does not ship it). Then run `telegram.py login` for the setup steps.")


def make_client():
    """The real client. Imports Telethon only here, so every other path works without it."""
    try:
        import telethon  # noqa: PLC0415
    except ImportError:
        die(2, INSTALL_LINE)
    api_id, api_hash, session = _read_credentials()
    try:
        return TelethonClient(telethon, api_id, api_hash, session)
    except ApiError as exc:
        die(1, f"FAILED: {exc}.")


# ---- send safety ----------------------------------------------------------------------------
def _money_hits(text: str):
    """Money terms from the WhatsApp skill's draft_check, or None when it cannot run (lock)."""
    here = os.path.dirname(os.path.abspath(__file__))  # kit: code path
    return _cs().money_hits(text, here)  # kit: code path


def _gate_mode_fallback() -> str:
    return _cs().gate_mode_fallback()


def _gate_check(args, *, target, text, scope, thread, dry_run, source):
    return _cs().gate_check(_comms_module, _gate_mode_fallback, "telegram", args.money_ok, target=target,
                            text=text, scope=scope, thread=thread, dry_run=dry_run, source=source)


def _gate_begin(ctx):
    return _cs().gate_begin(ctx)


def _gate_finish(ctx, op, status, receipts=()):
    _cs().gate_finish(ctx, op, status, receipts)


# ---- read verbs -----------------------------------------------------------------------------
def cmd_chats(args, client):
    rows = _read(lambda: client.dialogs(args.n))
    for d in rows:
        print(f"chat={chat_label(d.get('id'))} unread={d.get('unread', 0)} last={when(d.get('ts'))} | "
              f"{clean(d.get('title'), 80)}")
    print(f"({len(rows)} chats)")


def cmd_unread(args, client):
    shown = 0
    for d in _read(lambda: client.dialogs(200)):
        if not d.get("unread"):
            continue
        if shown >= args.n:
            break
        shown += 1
        print(f"== {clean(d.get('title'), 80)} (chat={chat_label(d.get('id'))}, {d['unread']} unread)")
        # our own messages sit between the unread ones, so read more than the count
        rows = _read(lambda: client.messages(chat_label(d["id"]), 50))
        incoming = [r for r in rows if not r.get("out")][: d["unread"]]
        for m in reversed(incoming):
            show(m)
    if not shown:
        print("No unread chats.")


def cmd_history(args, client):
    rows = _read(lambda: client.messages(args.chat, args.n))
    for m in reversed(rows):
        show(m, full=True)
    print(f"({len(rows)} messages)")


def cmd_find(args, client):
    rows = _read(lambda: client.messages(args.chat, args.n, search=args.text))
    for m in rows:
        show(m)
    print(f"({len(rows)} found)")


def _read(fn):
    try:
        return fn()
    except (ApiError, UncertainError) as exc:
        die(1, f"FAILED: {exc}.")


# ---- send verbs -----------------------------------------------------------------------------
def _send_flow(args, client, verb: str, reply_to):
    text = args.text
    if not text.strip():
        die(1, "NOT SENT: the message is empty.")
    if args.yes and not args.since:
        die(2, "NOT SENT: --since is required with --yes (the time of the newest message you "
               "read, ISO 8601 or epoch seconds).")
    since = None
    if args.since:
        since = parse_ts(args.since)
        if since is None:
            die(2, "NOT SENT: --since is not a time (use ISO 8601 or epoch seconds).")

    try:
        client.resolve(args.chat)
    except ApiError as exc:
        print(f"NOT SENT: {exc}. Nothing was sent.")
        raise SystemExit(1)

    if since is not None:
        recent = _read(lambda: client.messages(args.chat, RECENT_FOR_STALE_CHECK))
        for m in recent:
            t = m.get("ts")
            if t is None or t > since:
                print(f"NOT SENT: the chat has a message newer than --since ({when(t)}). "
                      "Read it again, then redo the send.")
                raise SystemExit(STALE_EXIT)

    print(f"{'About to send' if args.yes else 'DRY RUN, nothing sent'}. {verb} on Telegram:")
    print(f"  To:      {args.chat}")
    if reply_to:
        print(f"  Reply to message: {reply_to}")
    print("  Words:")
    for line in text.strip().splitlines():
        print(f"    {line}")

    hits = _money_hits(text)
    if hits is None:
        print("  MONEY LOCK: the money check could not run, so treat this as money. Show the exact "
              "text, get a fresh yes, then add --money-ok.")
    elif hits:
        print(f"  MONEY LOCK: the text mentions {', '.join(sorted(set(hits)))}. Show the exact text, "
              "get a fresh yes, then add --money-ok.")
    locked = hits is None or bool(hits)
    if locked and args.yes and not args.money_ok:
        print("NOT SENT: money lock. Show the exact text, get a fresh yes, then add --money-ok.")
        raise SystemExit(MONEY_LOCK_EXIT)

    ctx = _gate_check(args, target=str(args.chat), text=text, scope="", thread=str(reply_to or ""),
                      dry_run=not args.yes, source=f"telegram.py {verb}")
    if not args.yes:
        print("Add --yes to send (with --since, the time of the newest message you read).")
        return

    op = _gate_begin(ctx)
    try:
        result = client.send(args.chat, text, reply_to=reply_to)
    except UncertainError:
        _gate_finish(ctx, op, "uncertain")
        print("UNCERTAIN SEND: Telegram did not answer, so the message may have been delivered. "
              "Do not resend. Open the chat and check first.")
        raise SystemExit(UNCERTAIN_EXIT)
    except ApiError as exc:
        _gate_finish(ctx, op, "failed_before_send")
        print(f"NOT SENT: {exc}. Nothing was archived.")
        raise SystemExit(1)
    except BaseException:
        _gate_finish(ctx, op, "uncertain")
        raise
    mid = (result or {}).get("id")
    _gate_finish(ctx, op, "sent", [str(mid)] if mid else [])
    C = _comms_module("commslib")
    if C is not None:
        C.log_event("telegram", "send", str(args.chat), text=text, message_id=str(mid) if mid else None,
                    scope=str(args.chat), verb=verb, hook_covers=False, command=" ".join(sys.argv))
    print(f"Sent. id={mid or 'unknown'} chat={args.chat}")


def cmd_send(args, client):
    _send_flow(args, client, "send", None)


def cmd_reply(args, client):
    _send_flow(args, client, "reply", args.message_id)


def cmd_mark_read(args, client):
    if not args.yes:
        print(f"DRY RUN, nothing changed. Would mark chat {args.chat} as read. Add --yes.")
        return
    try:
        client.resolve(args.chat)
    except ApiError as exc:
        die(1, f"FAILED: {exc}.")
    try:
        client.mark_read(args.chat)
    except UncertainError:
        print("UNCERTAIN: Telegram did not answer. Check the chat; marking read is safe to redo.")
        raise SystemExit(UNCERTAIN_EXIT)
    except ApiError as exc:
        die(1, f"FAILED: {exc}.")
    print(f"Marked chat {args.chat} as read.")


# ---- setup verbs (no client, no network) ----------------------------------------------------
def cmd_login(args, client=None):
    print("""Telegram setup. This command only prints the steps. It logs in to nothing.

1. Install the library:   pip install telethon
2. Get an api_id and an api_hash. Open https://my.telegram.org in a browser, log in with your
   phone number, choose "API development tools", and create an app. Copy the two values.
3. Keep both values in your secret store. Do not paste them into the config file.
   In the comms-kit config set three keys (see config.example.json):
     telegram.api_id_command    a command that prints the api_id
     telegram.api_hash_command  a command that prints the api_hash
     telegram.session_path      where Telethon keeps its login file (treat it like a password)
4. Log in once, by hand, in a terminal. Telegram sends a code to your Telegram app (and asks for
   your two-step password if you set one):
     python3 -c "from telethon.sync import TelegramClient; \\
TelegramClient('<session_path>', <api_id>, '<api_hash>').start()"
   Type the phone number, then the code. The session file now exists.
5. Check the setup:   python3 telegram.py status   then   python3 telegram.py chats

Risk: Telegram can limit or ban an account that is used for automation, even through its own API.
Use an account you can afford to lose, and keep volume low.""")


def cmd_status(args, client=None):
    try:
        import telethon  # noqa: PLC0415,F401
        have = "installed"
    except ImportError:
        have = "missing (pip install telethon)"
    print(f"telethon: {have}")
    for key, value in (("telegram.api_id_command", _conf("telegram.api_id_command")),
                       ("telegram.api_hash_command", _conf("telegram.api_hash_command")),
                       ("telegram.session_path", _conf("telegram.session_path"))):
        print(f"{key}: {'set' if value else 'NOT SET'}")
    session = _conf("telegram.session_path")
    if session:
        found = any(os.path.exists(os.path.expanduser(str(session)) + ext) for ext in ("", ".session"))
        print(f"session file: {'found' if found else 'not found (run `telegram.py login`)'}")


NEEDS_CLIENT = {"chats", "unread", "history", "find", "send", "reply", "mark-read"}


# ---- CLI ------------------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="telegram.py", description="Read and send on Telegram (mock only).")
    sub = p.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("chats", help="list chats with unread counts"); sp.add_argument("-n", type=int, default=20)
    sp.set_defaults(fn=cmd_chats)
    sp = sub.add_parser("unread", help="unread incoming messages, per chat"); sp.add_argument("-n", type=int, default=10)
    sp.set_defaults(fn=cmd_unread)
    sp = sub.add_parser("history", help="one chat, oldest first"); sp.add_argument("chat")
    sp.add_argument("-n", type=int, default=20); sp.set_defaults(fn=cmd_history)
    sp = sub.add_parser("find", help="search messages in one chat, or in all chats"); sp.add_argument("text")
    sp.add_argument("--chat"); sp.add_argument("-n", type=int, default=10); sp.set_defaults(fn=cmd_find)

    def sendopts(sp):
        sp.add_argument("--since", help="time of the newest message you read (ISO 8601 or epoch)")
        sp.add_argument("--yes", action="store_true"); sp.add_argument("--money-ok", action="store_true")

    sp = sub.add_parser("send", help="send a message (dry run unless --yes)"); sp.add_argument("chat")
    sp.add_argument("text"); sendopts(sp); sp.set_defaults(fn=cmd_send)
    sp = sub.add_parser("reply", help="reply to one message (dry run unless --yes)"); sp.add_argument("chat")
    sp.add_argument("message_id", type=int); sp.add_argument("text"); sendopts(sp); sp.set_defaults(fn=cmd_reply)
    sp = sub.add_parser("mark-read", help="mark a chat read (dry run unless --yes)"); sp.add_argument("chat")
    sp.add_argument("--yes", action="store_true"); sp.set_defaults(fn=cmd_mark_read)
    sub.add_parser("login", help="print the setup steps (logs in to nothing)").set_defaults(fn=cmd_login)
    sub.add_parser("status", help="show what is set up (no network)").set_defaults(fn=cmd_status)
    return p


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:                     # argparse exits 2 on bad usage; keep that
        raise SystemExit(exc.code)
    if getattr(args, "n", None) is not None:
        args.n = clamp(args.n)
    if args.command not in NEEDS_CLIENT:
        args.fn(args)
        return
    _cs()                      # no shared guards, no run: one line, exit 2
    for name in ("chat",):
        if getattr(args, name, None) is not None:
            try:
                setattr(args, name, parse_chat(getattr(args, name)))
            except ValueError as exc:
                die(2, f"NOT SENT: {exc}." if args.command in ("send", "reply") else f"{exc}.")
    client = make_client()
    try:
        args.fn(args, client)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
