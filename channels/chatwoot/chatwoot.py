#!/usr/bin/env python3
"""Read and reply on Chatwoot (Application API over HTTPS). Python stdlib only.

MOCK ONLY. This code has never run against a real Chatwoot. Tests run it against a local fake
HTTP server on 127.0.0.1. Paths and fields were read in Chatwoot's published Swagger file
(application_swagger.json), see README.md for what was and was not checked.

Verbs: conversations, unread, thread, find, reply, note, resolve, mark-read.
API (base_url + /api/v1/accounts/{account_id}/...):
  GET  conversations?status=&assignee_type=&q=&page=      list, paged by `page`
  GET  conversations/{id}                                 details (meta.sender, can_reply)
  GET  conversations/{id}/messages[?before=ID]            latest 20 oldest-first; `before` pages back
  POST conversations/{id}/messages                        {content, message_type, private}
  POST conversations/{id}/toggle_status                   {status}
  POST conversations/{id}/update_last_seen                mark as read

The token goes in the `api_access_token` header. It comes from `chatwoot.token_command` (a list,
run without a shell). Nothing stores it. Redirects are never followed, so the token cannot be
sent to another host. Plain http is accepted only for 127.0.0.1 / localhost.

Exit codes: 0 ok, 1 refused or failed, 2 usage/setup problem (including a refused token),
4 UNCERTAIN write (do not resend), 5 a newer message exists (--since), 6 money lock,
7 outbound gate refused.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

UNCERTAIN_EXIT = 4
STALE_EXIT = 5
MONEY_LOCK_EXIT = 6
GATE_DENY_EXIT = 7
READ_TIMEOUT = 30
WRITE_TIMEOUT = 45
MAX_PAGES = 20            # list pages and thread pages, per command
MAX_ROWS = 200
STATUSES = ("all", "open", "resolved", "pending", "snoozed")
ASSIGNEES = ("me", "unassigned", "all", "assigned")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
INCOMING, OUTGOING, ACTIVITY, TEMPLATE = 0, 1, 2, 3     # message_type in Chatwoot's schema


class ApiError(RuntimeError):
    """A failed call. The message never holds the token."""


class UncertainError(RuntimeError):
    """A write that may or may not have been done. Never retry."""


class Refused(ApiError):
    """Chatwoot answered 401 or 403: the token is not accepted."""


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
    """Run the token command with no shell. Returns the token; raises ApiError with a token-free line."""
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
        raise ApiError("the token command must be a list of strings")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, shell=False)
    except FileNotFoundError:
        raise ApiError("the token command was not found") from None
    except (subprocess.TimeoutExpired, OSError):
        raise ApiError("the token command did not finish") from None
    token = (proc.stdout or "").strip()
    if proc.returncode != 0 or not token:
        raise ApiError(f"the token command failed (exit {proc.returncode})")
    return token


_run_key_cmd = _real_run_key_cmd


def base_url_ok(url) -> bool:
    """https with a host, or http only to this machine. The token never goes out in clear text."""
    try:
        p = urllib.parse.urlparse(str(url))
        host = p.hostname
    except ValueError:
        return False
    if not host:
        return False
    return p.scheme == "https" or (p.scheme == "http" and host in LOOPBACK)


def settings():
    """(base_url, account_id, token). Exits 2 with one plain line when anything is wrong."""
    base = _conf("chatwoot.base_url")
    account = _conf("chatwoot.account_id")
    command = _conf("chatwoot.token_command")
    for key, value in (("chatwoot.base_url", base), ("chatwoot.account_id", account),
                       ("chatwoot.token_command", command)):
        if not value:
            die(2, f"setup: set {key} in the comms-kit config (see config.example.json).")
    base = str(base).rstrip("/")
    if not base_url_ok(base):
        die(2, "setup: chatwoot.base_url must start with https:// (plain http works only for 127.0.0.1).")
    account = str(account).strip()
    if not re.fullmatch(r"\d{1,12}", account):
        die(2, "setup: chatwoot.account_id must be the number from your Chatwoot URL.")
    try:
        token = _run_key_cmd(command)
    except ApiError as exc:
        die(2, f"setup: token lookup failed: {exc}.")
    return base, account, token


def need_id(value) -> str:
    if not re.fullmatch(r"\d{1,12}", str(value)):
        die(2, "a conversation id is a number (copy it from the `conversations` verb).")
    return str(value)


# ---- HTTP -----------------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None          # a 3xx becomes an HTTPError: the token is never re-sent elsewhere


def _opener(url: str):
    handlers = [_NoRedirect()]
    if urllib.parse.urlparse(url).hostname in LOOPBACK:
        handlers.append(urllib.request.ProxyHandler({}))      # loopback never goes through a proxy
    return urllib.request.build_opener(*handlers)


def http_request(method, url, token, params=None, body=None, timeout=READ_TIMEOUT):
    """(status, json). A POST with an unknown result raises UncertainError; a GET failure raises ApiError.
    The token never enters a message."""
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    headers = {"api_access_token": token, "Accept": "application/json", "User-Agent": "comms-kit-chatwoot/1"}
    if method == "POST":
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _opener(url).open(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw, status = exc.read() or b"", exc.code
        except Exception:  # noqa: BLE001
            raw, status = b"", exc.code
    except Exception:  # noqa: BLE001 - timeout, reset, DNS: no token in the text
        if method == "POST":
            raise UncertainError("no answer from Chatwoot") from None
        raise ApiError("could not reach Chatwoot") from None
    try:
        return status, json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except ValueError:
        return status, {}


class Client:
    def __init__(self):
        self.base, self.account, self.token = settings()
        self.root = f"{self.base}/api/v1/accounts/{self.account}"

    def get(self, path, params=None):
        """Body of a 200 answer. Any failure ends the run with one plain line."""
        try:
            status, body = http_request("GET", self.root + path, self.token, params, timeout=READ_TIMEOUT)
        except ApiError as exc:
            die(1, f"FAILED: {exc}.")
        if status in (401, 403):
            die(2, f"Chatwoot refused the token (HTTP {status}). Check chatwoot.token_command and that "
                   "the token belongs to an agent of this account.")
        if status == 404:
            die(1, "FAILED: Chatwoot did not find that (HTTP 404).")
        if status == 429:
            die(1, "FAILED: Chatwoot rate limit (HTTP 429). Wait a minute, then run this again.")
        if status != 200 or not isinstance(body, (dict, list)):
            die(1, f"FAILED: Chatwoot answered HTTP {status}.")
        return body

    def post(self, path, body):
        """(status, json) for a write. 4xx raises ApiError (Chatwoot said no, nothing was done);
        401/403 raises Refused; 5xx, 3xx and transport failures raise UncertainError."""
        status, data = http_request("POST", self.root + path, self.token, body=body, timeout=WRITE_TIMEOUT)
        if status in (401, 403):
            raise Refused(f"Chatwoot refused the token (HTTP {status}). Check chatwoot.token_command")
        if 400 <= status < 500:
            raise ApiError(f"Chatwoot answered HTTP {status}")
        if not 200 <= status < 300:
            raise UncertainError(f"Chatwoot answered HTTP {status}")
        return status, data


# ---- helpers --------------------------------------------------------------------------------
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


def clamp(n, top=MAX_ROWS) -> int:
    return max(1, min(int(n), top))


def label(m: dict) -> str:
    if m.get("private"):
        return "NOTE"
    return {INCOMING: "IN", OUTGOING: "OUT", ACTIVITY: "ACTIVITY", TEMPLATE: "TEMPLATE"}.get(
        m.get("message_type"), "?")


def show_message(m: dict, full: bool = False) -> None:
    ts = parse_ts(m.get("created_at"))
    sender = (m.get("sender") or {}).get("name") or ""
    reply = (m.get("content_attributes") or {}).get("in_reply_to")
    extra = f" in_reply_to={reply}" if reply else ""
    print(f"[{label(m)}] id={m.get('id')} time={int(ts) if ts is not None else 'unknown'} {when(ts)}{extra}")
    print(f"  {clean(sender, 80)}: {clean(m.get('content'), 2000 if full else 240)}")


def show_conversation(c: dict) -> None:
    sender = ((c.get("meta") or {}).get("sender") or {}).get("name") or ""
    last = c.get("last_non_activity_message") or ((c.get("messages") or [{}])[-1])
    print(f"conv={c.get('id')} status={c.get('status')} unread={c.get('unread_count', 0)} "
          f"inbox={c.get('inbox_id')} | {clean(sender, 60)} | last: {clean((last or {}).get('content'), 100)}")


def payload_rows(body) -> list:
    """The conversation list lives at data.payload; messages at payload."""
    if isinstance(body, dict):
        node = body.get("data") if isinstance(body.get("data"), dict) else body
        rows = node.get("payload")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


def list_conversations(client, params: dict, want: int, keep=None) -> list:
    out = []
    for page in range(1, MAX_PAGES + 1):
        rows = payload_rows(client.get("/conversations", dict(params, page=page)))
        if not rows:
            break
        out += [r for r in rows if keep is None or keep(r)]
        if len(out) >= want:
            break
    return out[:want]


def latest_messages(client, cid: str) -> list:
    return payload_rows(client.get(f"/conversations/{cid}/messages"))


# ---- read verbs -----------------------------------------------------------------------------
def cmd_conversations(args):
    client = Client()
    rows = list_conversations(client, {"status": args.status, "assignee_type": args.assignee}, args.n)
    for c in rows:
        show_conversation(c)
    print(f"({len(rows)} conversations)")


def cmd_unread(args):
    client = Client()
    rows = list_conversations(client, {"status": "open", "assignee_type": "all"}, args.n,
                              keep=lambda c: (c.get("unread_count") or 0) > 0)
    if not rows:
        print("No unread conversations.")
    for c in rows:
        show_conversation(c)


def cmd_find(args):
    text = args.text.strip()
    if not text:
        die(2, "give some text to search for.")
    client = Client()
    rows = list_conversations(client, {"q": text, "status": "all", "assignee_type": "all"}, args.n)
    for c in rows:
        show_conversation(c)
    print(f"({len(rows)} found)")


def cmd_thread(args):
    cid = need_id(args.conversation)
    client = Client()
    rows = {m["id"]: m for m in latest_messages(client, cid) if "id" in m}
    if args.all:
        for _ in range(MAX_PAGES):
            if not rows:
                break
            older = payload_rows(client.get(f"/conversations/{cid}/messages", {"before": min(rows)}))
            fresh = [m for m in older if "id" in m and m["id"] not in rows]
            if not fresh:
                break
            rows.update({m["id"]: m for m in fresh})
    if not rows:
        print("No messages in that conversation.")
        return
    far = float("inf")
    for m in sorted(rows.values(), key=lambda m: (parse_ts(m.get("created_at")) or far, m["id"])):
        show_message(m, full=True)


# ---- send safety ----------------------------------------------------------------------------
def _money_hits(text: str):
    """Money terms from the WhatsApp skill's draft_check, or None when it cannot run (lock)."""
    here = os.path.dirname(os.path.abspath(__file__))  # kit: code path
    return _cs().money_hits(text, here)  # kit: code path


def _gate_mode_fallback() -> str:
    return _cs().gate_mode_fallback()


def _gate_check(args, *, target, text, scope, thread, dry_run, source):
    return _cs().gate_check(_comms_module, _gate_mode_fallback, "chatwoot", args.money_ok, target=target,
                            text=text, scope=scope, thread=thread, dry_run=dry_run, source=source)


def _gate_begin(ctx):
    return _cs().gate_begin(ctx)


def _gate_finish(ctx, op, status, receipts=()):
    _cs().gate_finish(ctx, op, status, receipts)


# ---- write verbs ----------------------------------------------------------------------------
def cmd_reply(args):
    text = args.text
    cid = need_id(args.conversation)
    if not text.strip():
        die(1, "NOT SENT: the reply is empty.")
    if args.yes and not args.since:
        die(2, "NOT SENT: --since is required with --yes (the time of the newest message you "
               "read: the `time=` number from `thread`, or ISO 8601).")
    since = None
    if args.since:
        since = parse_ts(args.since)
        if since is None:
            die(2, "NOT SENT: --since is not a time (use epoch seconds or ISO 8601).")
    client = Client()

    conv = client.get(f"/conversations/{cid}")
    conv = conv if isinstance(conv, dict) else {}
    if conv.get("can_reply") is False:
        print("NOT SENT: Chatwoot says this conversation cannot be replied to (can_reply is false).")
        raise SystemExit(1)
    contact = (conv.get("meta") or {}).get("sender") or {}
    channel = (conv.get("meta") or {}).get("channel") or "unknown channel"
    target = contact.get("email") or contact.get("phone_number") or f"chatwoot:{client.account}:{cid}"

    if since is not None:
        # The newest message is last on the latest page, so one page finds any newer message.
        # A customer message, a reply to an old one and a teammate's PRIVATE NOTE all block: a note
        # can say "do not reply". Chatwoot's own activity rows (assignment, label, status changes)
        # are not something a person said, so they never block.
        for m in latest_messages(client, cid):
            if m.get("message_type") == ACTIVITY:
                continue
            t = parse_ts(m.get("created_at"))
            if t is None or t > since:
                print(f"NOT SENT: the conversation has a message newer than --since ({when(t)}). "
                      "Read it again, then redo the reply.")
                raise SystemExit(STALE_EXIT)

    print(f"{'About to send' if args.yes else 'DRY RUN, nothing sent'}. Reply on Chatwoot (this reaches the customer):")
    print(f"  To:           {clean(contact.get('name'), 60)} <{target}>")
    print(f"  Channel:      {channel}")
    print(f"  Conversation: {cid}")
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

    ctx = _gate_check(args, target=target, text=text, scope=client.account, thread=cid,
                      dry_run=not args.yes, source="chatwoot.py reply")
    if not args.yes:
        print("Add --yes to send (with --since, the time of the newest message you read).")
        return

    op = _gate_begin(ctx)
    try:
        _, resp = client.post(f"/conversations/{cid}/messages",
                              {"content": text, "message_type": "outgoing", "private": False})
        mid = resp.get("id") if isinstance(resp, dict) else None
        if not mid:
            raise UncertainError("Chatwoot answered without a message id")
    except UncertainError:
        _gate_finish(ctx, op, "uncertain")
        print("UNCERTAIN SEND: Chatwoot did not give a clear answer, so the reply may have been delivered. "
              "Do not resend. Open the conversation in Chatwoot and check first.")
        raise SystemExit(UNCERTAIN_EXIT)
    except Refused as exc:
        _gate_finish(ctx, op, "failed_before_send")
        print(f"NOT SENT: {exc}.")
        raise SystemExit(2)
    except ApiError as exc:
        _gate_finish(ctx, op, "failed_before_send")
        print(f"NOT SENT: {exc}. Nothing was archived.")
        raise SystemExit(1)
    except BaseException:
        _gate_finish(ctx, op, "uncertain")
        raise
    _gate_finish(ctx, op, "sent", [str(mid)])
    C = _comms_module("commslib")
    if C is not None:
        C.log_event("chatwoot", "send", target, text=text, message_id=str(mid), scope=client.account,
                    verb="reply", hook_covers=False, command=" ".join(sys.argv))
    print(f"Sent. id={mid} conversation={cid}")


def _simple_write(args, what: str, cid: str, path: str, body, dry_line: str, done_line: str, preview=()):
    """A write that reaches no customer: dry run unless --yes; an unknown result exits 4.
    Settings load and the conversation is read first, in a dry run too, so a dry run never
    promises something that cannot happen."""
    client = Client()
    conv = client.get(f"/conversations/{cid}")
    contact = (((conv if isinstance(conv, dict) else {}).get("meta") or {}).get("sender") or {}).get("name")
    who = f" ({clean(contact, 60)})" if contact else ""
    if not args.yes:
        print(f"DRY RUN, nothing changed. {dry_line.format(who=who)} Add --yes.")
        for line in preview:
            print(line)
        return
    try:
        client.post(path, body)
    except UncertainError:
        print(f"UNCERTAIN: Chatwoot did not give a clear answer. Check the conversation; "
              f"do not assume the {what} happened.")
        raise SystemExit(UNCERTAIN_EXIT)
    except Refused as exc:
        die(2, f"FAILED: {exc}.")
    except ApiError as exc:
        die(1, f"FAILED: {exc}.")
    print(done_line)


def cmd_note(args):
    cid = need_id(args.conversation)
    if not args.text.strip():
        die(1, "NOT SENT: the note is empty.")
    words = ["    " + line for line in args.text.strip().splitlines()]
    _simple_write(args, "note", cid, f"/conversations/{cid}/messages",
                  {"content": args.text, "message_type": "outgoing", "private": True},
                  f"Would add this private note to conversation {cid}{{who}} (the customer cannot see it):",
                  f"Note added to conversation {cid}.", preview=words)


def cmd_resolve(args):
    cid = need_id(args.conversation)
    _simple_write(args, "status change", cid, f"/conversations/{cid}/toggle_status", {"status": "resolved"},
                  f"Would mark conversation {cid}{{who}} as resolved.", f"Conversation {cid} marked as resolved.")


def cmd_mark_read(args):
    cid = need_id(args.conversation)
    _simple_write(args, "mark-read", cid, f"/conversations/{cid}/update_last_seen", None,
                  f"Would mark conversation {cid}{{who}} as read.", f"Conversation {cid} marked as read.")


# ---- CLI ------------------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="chatwoot.py", description="Read and reply on Chatwoot (mock only).")
    sub = p.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("conversations", help="list conversations")
    sp.add_argument("--status", choices=STATUSES, default="open")
    sp.add_argument("--assignee", choices=ASSIGNEES, default="all")
    sp.add_argument("-n", type=int, default=25); sp.set_defaults(fn=cmd_conversations)
    sp = sub.add_parser("unread", help="open conversations with unread messages")
    sp.add_argument("-n", type=int, default=25); sp.set_defaults(fn=cmd_unread)
    sp = sub.add_parser("thread", help="one conversation, oldest first"); sp.add_argument("conversation")
    sp.add_argument("--all", action="store_true", help="page back through the whole conversation")
    sp.set_defaults(fn=cmd_thread)
    sp = sub.add_parser("find", help="search message text (Chatwoot searches)"); sp.add_argument("text")
    sp.add_argument("-n", type=int, default=25); sp.set_defaults(fn=cmd_find)
    sp = sub.add_parser("reply", help="reply to the customer (dry run unless --yes)")
    sp.add_argument("conversation"); sp.add_argument("text")
    sp.add_argument("--since", help="time of the newest message you read (epoch seconds or ISO 8601)")
    sp.add_argument("--yes", action="store_true"); sp.add_argument("--money-ok", action="store_true")
    sp.set_defaults(fn=cmd_reply)
    sp = sub.add_parser("note", help="private note, reaches no customer (dry run unless --yes)")
    sp.add_argument("conversation"); sp.add_argument("text"); sp.add_argument("--yes", action="store_true")
    sp.set_defaults(fn=cmd_note)
    sp = sub.add_parser("resolve", help="mark a conversation resolved (dry run unless --yes)")
    sp.add_argument("conversation"); sp.add_argument("--yes", action="store_true"); sp.set_defaults(fn=cmd_resolve)
    sp = sub.add_parser("mark-read", help="mark a conversation read (dry run unless --yes)")
    sp.add_argument("conversation"); sp.add_argument("--yes", action="store_true"); sp.set_defaults(fn=cmd_mark_read)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    _cs()                      # no shared guards, no run: one line, exit 2
    if getattr(args, "n", None) is not None:
        args.n = clamp(args.n)
    args.fn(args)


if __name__ == "__main__":
    main()
