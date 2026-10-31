#!/usr/bin/env python3
"""Read and reply on Instantly (cold email) from one CLI. Python stdlib only.

Verbs: workspaces, unread, thread, find, reply, mark-read.
API: https://api.instantly.ai/api/v2  (GET /emails, GET /emails/{id}, POST /emails/reply,
POST /emails/threads/{thread_id}/mark-as-read). None of these calls spends credits.

Keys are never stored. Config key `instantly.workspaces` maps a workspace name to a key command
(a list, run without a shell) whose stdout is the API key. INSTANTLY_API_KEY is used when
--workspace is omitted.

Exit codes: 0 ok, 1 refused or failed, 2 usage/key problem, 4 UNCERTAIN send (do not resend),
5 a newer message exists (--since), 6 money lock, 7 outbound gate refused.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as htmllib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.instantly.ai/api/v2"
UNCERTAIN_EXIT = 4
STALE_EXIT = 5
MONEY_LOCK_EXIT = 6
GATE_DENY_EXIT = 7
HTTP_OK = 2 * 100       # the one success status Instantly uses here
RECEIVED_UE_TYPE = 2     # ue_type 2 = a reply the lead sent us (1/3/4 are our own sends)


class ApiError(RuntimeError):
    """A failed call. The message never holds the key."""


class UncertainError(RuntimeError):
    """A POST that may or may not have been delivered. Never retry."""


# ---- config and keys ------------------------------------------------------------------------
def _find_dir(env_name: str, *candidates: str) -> str | None:
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


def _core_dirs():
    return list(_find_dir("COMMS_KIT_CORE", "../../core"))


def _comms_module(name: str):
    """core/<name>.py loaded by exact path; None when it cannot load."""
    for d in _core_dirs():
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


def load_workspaces() -> dict:
    kc = _comms_module("kitconfig")
    if kc is None:
        return {}
    value = kc.get("instantly.workspaces", {})
    return value if isinstance(value, dict) else {}


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


def resolve_key(workspace: str | None) -> str:
    if not workspace:
        key = os.environ.get("INSTANTLY_API_KEY", "").strip()
        if key:
            return key
        die(2, "no key: pass --workspace NAME, or set INSTANTLY_API_KEY.")
    spaces = load_workspaces()
    if workspace not in spaces:
        die(2, "unknown workspace (run `instantly.py workspaces`).")
    try:
        return _run_key_cmd(spaces[workspace])
    except ApiError as exc:
        die(2, f"key lookup failed: {exc}.")


def die(code: int, line: str):
    print(line, file=sys.stderr)
    raise SystemExit(code)


# ---- HTTP -----------------------------------------------------------------------------------
def _real_http_request(method, path, key, params=None, body=None, timeout=30):
    """(status, json). A POST that times out or drops raises UncertainError and is never retried.
    A GET transport failure raises ApiError. The key never enters a message."""
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json",
               "User-Agent": "comms-kit-instantly/1"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read() or b"", exc.code
    except Exception:  # noqa: BLE001 - timeout, reset, DNS: no key in the text
        if method == "POST":
            raise UncertainError("no answer from Instantly") from None
        raise ApiError("could not reach Instantly") from None
    try:
        return status, json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except ValueError:
        return status, {}


http_request = _real_http_request
RATE_LIMIT_WAITS = (15, 30)    # seconds before each repeat of a read that got a 429


def _get(key, path, params=None):
    # A read is safe to repeat. Instantly answers 429 when reads come fast.
    for wait in RATE_LIMIT_WAITS + (None,):
        try:
            status, body = http_request("GET", path, key, params=params)
        except (ApiError, UncertainError) as exc:
            die(1, f"FAILED: {exc}.")
        if status != 429:
            return status, body
        if wait is None:
            die(1, "FAILED: Instantly rate limit. Wait a minute, then run this again.")
        time.sleep(wait)


def _items(body) -> list:
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if isinstance(body, dict):
        for k in ("items", "data", "emails"):
            if isinstance(body.get(k), list):
                return [r for r in body[k] if isinstance(r, dict)]
    return []


# ---- email helpers --------------------------------------------------------------------------
def parse_ts(value) -> float | None:
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


def email_ts(e: dict) -> float | None:
    for f in ("timestamp_email", "timestamp_created", "created_at"):
        t = parse_ts(e.get(f))
        if t is not None:
            return t
    return None


def is_received(e: dict) -> bool:
    return e.get("ue_type") == RECEIVED_UE_TYPE


def body_text(e: dict) -> str:
    b = e.get("body")
    if isinstance(b, dict):
        return str(b.get("text") or re.sub(r"<[^>]+>", " ", str(b.get("html") or "")))
    return str(b or e.get("content_preview") or "")


def clean(s: str, limit: int = 600) -> str:
    s = re.sub(r"\s+", " ", htmllib.unescape(str(s or ""))).strip()
    return s if len(s) <= limit else s[: limit - 1] + "..."


def when(e: dict) -> str:
    t = email_ts(e)
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else "unknown-time"


def show(e: dict, full: bool = False) -> None:
    direction = "RECEIVED" if is_received(e) else "SENT"
    print(f"[{direction}] id={e.get('id')} thread={e.get('thread_id')} {when(e)}")
    print(f"  from {e.get('from_address_email')} via {e.get('eaccount')} | {clean(e.get('subject'), 120)}")
    print(f"  {clean(body_text(e), 2000 if full else 240)}")


def list_emails(key, params, want: int) -> list:
    out, cursor = [], None
    while len(out) < want:
        p = dict(params)
        p["limit"] = min(100, max(1, want - len(out)))
        if cursor:
            p["starting_after"] = cursor
        status, body = _get(key, "/emails", p)
        if status != HTTP_OK:
            die(1, f"FAILED: Instantly answered HTTP {status}.")
        page = _items(body)
        out += page
        cursor = body.get("next_starting_after") if isinstance(body, dict) else None
        if not cursor or not page:
            break
    return out[:want] if want else out


def fetch_thread(key, thread_id: str) -> list:
    rows = list_emails(key, {"search": f"thread:{thread_id}", "sort_order": "asc"}, 500)
    rows.sort(key=lambda e: (email_ts(e) is None, email_ts(e) or 0))
    return rows


# ---- read verbs -----------------------------------------------------------------------------
def cmd_workspaces(args):
    for name in sorted(load_workspaces()):
        print(name)


def cmd_unread(args):
    key = resolve_key(args.workspace)
    rows = list_emails(key, {"is_unread": "true", "email_type": "received", "sort_order": "desc"}, args.n)
    rows = [r for r in rows if r.get("ue_type") in (None, RECEIVED_UE_TYPE)]
    if not rows:
        print("No unread replies.")
    for r in rows:
        show(r)
    print(f"({len(rows)} unread)")


def cmd_thread(args):
    key = resolve_key(args.workspace)
    rows = fetch_thread(key, args.thread_id)
    if not rows:
        die(1, "No messages found for that thread.")
    for r in rows:
        show(r, full=True)


def cmd_find(args):
    key = resolve_key(args.workspace)
    text = args.text.strip()
    if "@" in text and " " not in text:
        rows = list_emails(key, {"search": text, "sort_order": "desc"}, args.n)
    else:
        needle = text.lower()
        pool = list_emails(key, {"sort_order": "desc"}, 300)
        rows = [r for r in pool if needle in " ".join(
            [str(r.get("subject") or ""), body_text(r), str(r.get("content_preview") or "")]).lower()][: args.n]
    for r in rows:
        show(r)
    print(f"({len(rows)} found)")


# ---- send safety ----------------------------------------------------------------------------
def _money_hits(text: str):
    """Money terms from the WhatsApp skill's draft_check, or None when it cannot run."""
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = [os.path.join(here, "..", "whatsapp"), os.path.expanduser("~/.claude/skills/whatsapp")]  # kit: code path
    for d in dirs:
        if not os.path.exists(os.path.join(d, "draft_check.py")):
            continue
        try:
            sys.path.insert(0, os.path.abspath(d))
            import draft_check  # noqa: PLC0415
            flat = draft_check.normalize_for_matching(str(text or ""))
            return [m.group(0).strip() for m in draft_check.MONEY.finditer(flat)]
        except Exception:  # noqa: BLE001 - a check that cannot run must lock, not pass
            return None
    return None


def _gate_mode_fallback() -> str:
    value = os.environ.get("GATE_MODE")
    if value is None:
        try:
            with open(os.path.expanduser("~/.claude/communications/gate_mode")) as fh:
                value = fh.read()
        except FileNotFoundError:
            value = "report"
        except OSError:
            value = "enforce"
    value = value.strip().lower()
    return value if value in ("report", "enforce") else "enforce"


def _gate_check(args, *, target, text, scope, thread, dry_run):
    G = _comms_module("outbound_gate")
    if G is None:
        if _gate_mode_fallback() == "enforce":
            print("NOT SENT: outbound gate (enforce): the gate module could not load.")
            raise SystemExit(GATE_DENY_EXIT)
        sys.stderr.write("outbound gate: module could not load; report mode, continuing\n")
        return None
    approvals = G.Approvals.from_env(money_ok=bool(args.money_ok))
    payload = G.OutboundPayload("instantly", target=target, texts=[text], recipients=[target],
                                scope=scope or "", thread=thread or "")
    verdict = G.evaluate(payload, approvals, dry_run=dry_run, source="instantly.py reply")
    if not verdict.allow:
        print(verdict.deny_line())
        raise SystemExit(GATE_DENY_EXIT)
    return (G, payload, approvals)


def _gate_begin(ctx):
    if ctx is None:
        return None
    G, payload, approvals = ctx
    try:
        return G.begin(payload, approvals)
    except G.GateDenied as exc:
        print(str(exc))
        raise SystemExit(GATE_DENY_EXIT)


def _gate_finish(ctx, op, status, receipts=()):
    if ctx is not None and op:
        ctx[0].finish(op, status, [r for r in receipts if r])


# ---- reply ----------------------------------------------------------------------------------
def _html_body(text: str) -> str:
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join("<p>" + htmllib.escape(p).replace("\n", "<br>") + "</p>" for p in paras)


def cmd_reply(args):
    text = args.text
    if not text.strip():
        die(1, "NOT SENT: the reply is empty.")
    if args.yes and not args.since:
        die(2, "NOT SENT: --since is required with --yes (the time of the message you read, "
               "ISO 8601 or epoch seconds).")
    since = None
    if args.since:
        since = parse_ts(args.since)
        if since is None:
            die(2, "NOT SENT: --since is not a time (use ISO 8601 or epoch seconds).")
    key = resolve_key(args.workspace)

    status, email = _get(key, f"/emails/{urllib.parse.quote(args.email_id, safe='')}")
    if status != HTTP_OK or not isinstance(email, dict) or not email.get("id"):
        print("NOT SENT: Instantly did not return that email.")
        raise SystemExit(1)
    if not is_received(email):
        print("NOT SENT: that email is not a received reply. Reply only to a lead's message.")
        raise SystemExit(1)
    lead, inbox = email.get("from_address_email"), email.get("eaccount")
    if not lead or not inbox:
        print("NOT SENT: the email has no sender or sending inbox.")
        raise SystemExit(1)
    thread_id = str(email.get("thread_id") or "")

    if since is not None:
        thread = fetch_thread(key, thread_id) if thread_id else [email]
        for e in thread:
            t = email_ts(e)
            if t is None or t > since:
                print(f"NOT SENT: the thread has a message newer than --since ({when(e)}). "
                      "Read it again, then redo the reply.")
                raise SystemExit(STALE_EXIT)

    subject = str(email.get("subject") or "").strip()
    if not re.match(r"(?i)^re:", subject):
        subject = "Re: " + subject
    workspace = args.workspace or ""

    print(f"{'About to send' if args.yes else 'DRY RUN, nothing sent'}. Reply on Instantly:")
    print(f"  To:      {lead}")
    print(f"  From:    {inbox}")
    print(f"  Subject: {subject}")
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

    ctx = _gate_check(args, target=lead, text=text, scope=workspace, thread=thread_id,
                      dry_run=not args.yes)
    if not args.yes:
        print("Add --yes to send (with --since, the time of the message you read).")
        return

    body = {"reply_to_uuid": args.email_id, "eaccount": inbox, "subject": subject,
            "body": {"text": text, "html": _html_body(text)}}
    op = _gate_begin(ctx)
    try:
        status, resp = http_request("POST", "/emails/reply", key, body=body, timeout=45)
    except (UncertainError, ApiError):
        _gate_finish(ctx, op, "uncertain")
        print("UNCERTAIN SEND: Instantly did not answer, so the reply may have been delivered. "
              "Do not resend. Check the thread in Instantly first.")
        raise SystemExit(UNCERTAIN_EXIT)
    except BaseException:
        _gate_finish(ctx, op, "uncertain")
        raise
    if status != HTTP_OK:
        _gate_finish(ctx, op, "uncertain" if status >= 500 else "failed_before_send")
        extra = " Check the thread in Instantly before trying again." if status >= 500 else ""
        print(f"FAILED: Instantly answered HTTP {status}. Nothing was archived.{extra}")
        raise SystemExit(1)
    resp = resp if isinstance(resp, dict) else {}
    mid = resp.get("id") or resp.get("uuid")
    _gate_finish(ctx, op, "sent", [str(mid)] if mid else [])
    C = _comms_module("commslib")
    if C is not None:
        C.log_event("instantly", "send", lead, text=text, message_id=str(mid) if mid else None,
                    scope=workspace or None, verb="reply", hook_covers=False, command=" ".join(sys.argv))
    print(f"Sent. id={mid or 'unknown'} thread={resp.get('thread_id') or thread_id}")


def cmd_mark_read(args):
    key = resolve_key(args.workspace)
    path = f"/emails/threads/{urllib.parse.quote(args.thread_id, safe='')}/mark-as-read"
    if not args.yes:
        print(f"DRY RUN, nothing changed. Would mark thread {args.thread_id} as read. Add --yes.")
        return
    try:
        status, _ = http_request("POST", path, key, timeout=30)
    except (UncertainError, ApiError):
        print("UNCERTAIN: Instantly did not answer. Check the thread; marking read is safe to redo.")
        raise SystemExit(UNCERTAIN_EXIT)
    if status != HTTP_OK:
        die(1, f"FAILED: Instantly answered HTTP {status}.")
    print(f"Marked thread {args.thread_id} as read.")


# ---- CLI ------------------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="instantly.py", description="Read and reply on Instantly.")
    sub = p.add_subparsers(dest="command", required=True)

    def ws(sp):
        sp.add_argument("--workspace", help="name from `workspaces` (omit to use INSTANTLY_API_KEY)")

    sub.add_parser("workspaces", help="list configured workspace names").set_defaults(fn=cmd_workspaces)
    sp = sub.add_parser("unread", help="newest unread lead replies"); ws(sp)
    sp.add_argument("-n", type=int, default=10); sp.set_defaults(fn=cmd_unread)
    sp = sub.add_parser("thread", help="one thread, oldest first"); ws(sp)
    sp.add_argument("thread_id"); sp.set_defaults(fn=cmd_thread)
    sp = sub.add_parser("find", help="search by lead address or text"); ws(sp)
    sp.add_argument("text"); sp.add_argument("-n", type=int, default=10); sp.set_defaults(fn=cmd_find)
    sp = sub.add_parser("reply", help="reply to a lead's email (dry run unless --yes)"); ws(sp)
    sp.add_argument("email_id"); sp.add_argument("text")
    sp.add_argument("--since", help="time of the newest message you read (ISO 8601 or epoch)")
    sp.add_argument("--yes", action="store_true"); sp.add_argument("--money-ok", action="store_true")
    sp.set_defaults(fn=cmd_reply)
    sp = sub.add_parser("mark-read", help="mark a thread read (dry run unless --yes)"); ws(sp)
    sp.add_argument("thread_id"); sp.add_argument("--yes", action="store_true")
    sp.set_defaults(fn=cmd_mark_read)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, "n", None) is not None:
        args.n = max(1, min(args.n, 100))
    args.fn(args)


if __name__ == "__main__":
    main()
