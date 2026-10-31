#!/usr/bin/env python3
"""Read and send on Google Chat through the `gws` CLI (version 0.22.5 was read for this).

MOCK ONLY. This code has never run against a real account. Tests use a stub script in place of
gws. The gws flags used here (--params, --json) and the Chat API fields come from `gws chat ...
--help` and Google's Chat API discovery document, not from a live run.

Verbs: spaces, history, find, send, reply, mark-read.

DANGER: running the real gws with a stripped environment or under a fake HOME DELETES the owner's
real Google login. So this tool never builds an environment (the full environment is passed on),
and it refuses to start the real gws when HOME is not the user's real home (exit 2, before any
subprocess). Tests set GCHAT_GWS_BIN to a stub, which is the only way past the check.

Exit codes: 0 ok, 1 refused or failed, 2 usage/setup problem (also: fake HOME), 3 the account
cannot use the Chat API (needs a Workspace login with the Chat scope), 4 UNCERTAIN send (do not
resend), 5 a newer message exists (--since), 6 money lock, 7 outbound gate refused.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import pwd
import re
import shutil
import subprocess
import sys

UNCERTAIN_EXIT = 4
STALE_EXIT = 5
MONEY_LOCK_EXIT = 6
GATE_DENY_EXIT = 7
PERMISSION_EXIT = 3
# Only these numeric answers mean "certainly not sent" (401/403 take the permission path first).
# 408, 409 and every 5xx can arrive after the work was done; a non-numeric code ("ABORTED") is unknown.
CERTAINLY_NOT_SENT = (400, 404, 422, 429)
READ_TIMEOUT = 60
SEND_TIMEOUT = 60
PAGE_SIZE = 100
MAX_PAGES = 20
MAX_ROWS = 200
ORDER_NEWEST_FIRST = "create_time DESC"      # the discovery document lists ASC and DESC; default is "create_time ASC"
MAX_TEXT = 4096                  # Google Chat's text limit for one message
SPACE_RE = re.compile(r"spaces/[A-Za-z0-9_-]{1,100}")
MESSAGE_RE = re.compile(r"(spaces/[A-Za-z0-9_-]{1,100})/messages/[A-Za-z0-9._-]{1,200}")
EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}@[A-Za-z0-9.\-]{1,200}\.[A-Za-z]{2,24}")

PERMISSION_LINE = ("Google Chat needs a Workspace login with the Chat scope. A personal Gmail account "
                   "cannot use the Chat API. Log in to gws with a Workspace account that has the Chat "
                   "scopes, then try again.")


class ApiError(RuntimeError):
    """A failed call that Google answered or that never started."""


class PermissionDenied(ApiError):
    """HTTP 401 or 403: this login cannot use the Chat API."""


class UncertainError(RuntimeError):
    """A write that may or may not have been delivered. Never retry."""


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


# ---- the gws command and the HOME guard -----------------------------------------------------
def _guard_home():
    """Refuse to start the real gws when HOME is not the user's real home folder."""
    real = pwd.getpwuid(os.getuid()).pw_dir
    home = os.environ.get("HOME")
    if not home or os.path.realpath(home) != os.path.realpath(real):
        die(2, "REFUSED: HOME is not this user's real home folder, and gws would not find (or could "
               "delete) the real Google login. Nothing was run. Run this tool with the normal HOME. "
               "Tests use GCHAT_GWS_BIN to point at a stub instead.")


def _find_gws_as() -> str:
    for d in _core_dirs():
        candidate = os.path.join(d, "bin", "gws-as")
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("gws-as") or os.path.expanduser("~/.local/bin/gws-as")
    if found and os.path.exists(found):
        return found
    die(2, "gchat.gws_account is set, but the gws-as wrapper was not found (it is in the kit's core/bin).")


def _gws_prefix() -> list:
    account = _conf("gchat.gws_account")
    if account:
        if not EMAIL_RE.fullmatch(str(account)):
            die(2, "setup: gchat.gws_account must be a plain email address.")
        wrapper = os.environ.get("GCHAT_GWS_AS_BIN")
        if not wrapper:
            _guard_home()               # gws-as runs the real gws: only a stub wrapper skips the check
            wrapper = _find_gws_as()
        return [wrapper, str(account), "chat"]
    binary = os.environ.get("GCHAT_GWS_BIN")
    if not binary:
        _guard_home()
        binary = "gws"
    return [binary, "chat"]


def _parse_json(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in text.splitlines():            # --page-all style output, one object per line
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


class Result:
    def __init__(self, rc, out, err):
        self.rc = rc
        self.data = _parse_json(out)
        self.error = None
        for blob in (self.data, _parse_json(err)):
            if isinstance(blob, dict) and isinstance(blob.get("error"), dict):
                self.error = blob["error"]
                break

    @property
    def code(self):
        return self.error.get("code") if self.error else None

    @property
    def code_int(self):
        """The HTTP code as a number, or None when it is missing or not numeric ("ABORTED")."""
        try:
            return int(self.code)
        except (TypeError, ValueError):
            return None


def _run(words, params=None, body=None, timeout=READ_TIMEOUT) -> Result:
    """Run `gws chat <words>`. The environment is passed on whole: never stripped, never replaced."""
    argv = _gws_prefix() + list(words)
    if params is not None:
        argv += ["--params", json.dumps(params)]
    if body is not None:
        argv += ["--json", json.dumps(body)]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, shell=False)
    return Result(proc.returncode, proc.stdout, proc.stderr)


def _is_permission(res: Result) -> bool:
    if not res.error:
        return False
    return res.code_int in (401, 403) or str(res.error.get("status", "")).upper() in (
        "PERMISSION_DENIED", "UNAUTHENTICATED")


def _error_line(res: Result) -> str:
    if res.error:
        msg = re.sub(r"\s+", " ", str(res.error.get("message") or "")).strip()[:200]
        return f"Google Chat answered HTTP {res.code}: {msg}"
    return f"gws exited {res.rc}"


def gws_read(words, params=None):
    """A read. Any failure ends the run with one plain line."""
    try:
        res = _run(words, params)
    except FileNotFoundError:
        die(2, "gws was not found. Install the Google Workspace CLI, or check GCHAT_GWS_BIN.")
    except subprocess.TimeoutExpired:
        die(1, "FAILED: gws did not answer in time.")
    if _is_permission(res):
        die(PERMISSION_EXIT, PERMISSION_LINE)
    if res.rc != 0:
        die(1, f"FAILED: {_error_line(res)}.")
    if not isinstance(res.data, dict):
        die(1, "FAILED: gws gave an answer this tool cannot read.")
    return res.data


def gws_write(words, params, body):
    """A write. Returns the answer, raises ApiError (certainly not done) or UncertainError (unknown)."""
    try:
        res = _run(words, params, body, timeout=SEND_TIMEOUT)
    except FileNotFoundError:
        raise ApiError("gws was not found. Nothing was run") from None
    except subprocess.TimeoutExpired:
        raise UncertainError("gws did not answer in time") from None
    if _is_permission(res):
        raise PermissionDenied(PERMISSION_LINE)
    if res.rc != 0:
        if res.code_int in CERTAINLY_NOT_SENT:
            raise ApiError(_error_line(res))          # Google answered a clear no: it was not done
        raise UncertainError("gws failed without a clear answer")
    if not isinstance(res.data, dict) or not res.data:
        raise UncertainError("gws gave an empty or unreadable answer")
    return res.data


# ---- time and text helpers ------------------------------------------------------------------
_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$")


def parse_dt(value):
    """An aware datetime. Reads Google's 3-to-9 digit fractions; None for anything unreadable."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or re.fullmatch(r"\d+(\.\d+)?", str(value).strip()):
        number = float(value)
        number = number / 1000 if number > 1e11 else number
        return dt.datetime.fromtimestamp(number, dt.timezone.utc)
    m = _TS.match(str(value).strip())
    if not m:
        return None
    base, frac, zone = m.groups()
    frac = ((frac or "") + "000000")[:6]
    zone = "+00:00" if zone in (None, "Z") else zone
    try:
        return dt.datetime.fromisoformat(f"{base}.{frac}{zone}")
    except ValueError:
        return None


def parse_ts(value):
    d = parse_dt(value)
    return d.timestamp() if d else None


def stamp(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def clean(s, limit: int = 600) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= limit else s[: limit - 1] + "..."


def show(m: dict, full: bool = False) -> None:
    sender = m.get("sender") or {}
    thread = (m.get("thread") or {}).get("name", "")
    reply = " (thread reply)" if m.get("threadReply") else ""
    print(f"[{sender.get('type', '?')}] name={m.get('name')} thread={thread}{reply} {m.get('createTime', 'unknown-time')}")
    print(f"  {clean(sender.get('displayName') or sender.get('name'), 80)}: "
          f"{clean(m.get('text'), 2000 if full else 240)}")


def clamp(n, top=MAX_ROWS) -> int:
    return max(1, min(int(n), top))


def need_space(value: str) -> str:
    if not SPACE_RE.fullmatch(str(value)):
        die(2, "a space name looks like spaces/AAAAxxxx (copy it from the `spaces` verb).")
    return value


# ---- reading --------------------------------------------------------------------------------
def list_messages(space: str, after: dt.datetime | None = None, max_pages: int | None = None,
                  want: int | None = None):
    """(messages, truncated). Newest first (orderBy), so a cut answer still holds the newest ones.
    `after` becomes the documented create_time filter. `truncated` is True only when the page
    limit stopped the read while Google still had more; stopping because `want` was reached is not."""
    max_pages = MAX_PAGES if max_pages is None else max_pages
    params = {"parent": space, "pageSize": PAGE_SIZE, "orderBy": ORDER_NEWEST_FIRST}
    if after is not None:
        params["filter"] = f'create_time > "{stamp(after)}"'
    rows, token = [], None
    for page_no in range(max_pages):
        page = dict(params)
        if token:
            page["pageToken"] = token
        data = gws_read(["spaces", "messages", "list"], page)
        got = [m for m in data.get("messages", []) if isinstance(m, dict)]
        rows += got
        token = data.get("nextPageToken")
        if page_no == 0 and token:
            _check_newest_first(got)
        if not token or (want is not None and len(rows) >= want):
            token = token if (want is None or len(rows) < want) else None
            break
    return rows, bool(token)


def _check_newest_first(rows):
    """A cut answer is only safe when Google really sent the newest messages first."""
    times = [t for t in (parse_dt(m.get("createTime")) for m in rows) if t is not None]
    if len(times) >= 2 and times[0] < times[-1]:
        die(1, "FAILED: Google Chat did not return the newest messages first, so a cut answer cannot "
               "be trusted. Nothing was sent.")


def by_time(rows):
    far = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    return sorted(rows, key=lambda m: parse_dt(m.get("createTime")) or far)


def cmd_spaces(args):
    token, shown = None, 0
    for _ in range(MAX_PAGES):
        params = {"pageSize": PAGE_SIZE}
        if token:
            params["pageToken"] = token
        data = gws_read(["spaces", "list"], params)
        for s in data.get("spaces", []):
            if shown >= args.n:
                break
            shown += 1
            print(f"{s.get('name')} | {s.get('spaceType', '?')} | {clean(s.get('displayName') or '(no name)', 80)}"
                  f" | last active {s.get('lastActiveTime', 'unknown')}")
        token = data.get("nextPageToken")
        if not token or shown >= args.n:
            break
    print(f"({shown} spaces)")


def cmd_history(args):
    space = need_space(args.space)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    rows, truncated = list_messages(space, after=since, want=args.n)
    rows = by_time(rows)[-args.n:]           # the newest n, shown oldest first
    for m in rows:
        show(m, full=True)
    note = (" (the window was cut at the page limit; older messages in it were not read, use a smaller "
            "--days)" if truncated else "")
    print(f"({len(rows)} messages){note}")


def all_space_names():
    """(names, more). Pages through the space list; `more` is True when the page limit cut it."""
    names, token = [], None
    for _ in range(MAX_PAGES):
        params = {"pageSize": PAGE_SIZE}
        if token:
            params["pageToken"] = token
        data = gws_read(["spaces", "list"], params)
        names += [s["name"] for s in data.get("spaces", [])
                  if isinstance(s, dict) and SPACE_RE.fullmatch(str(s.get("name", "")))]
        token = data.get("nextPageToken")
        if not token:
            break
    return names, bool(token)


def cmd_find(args):
    needle = args.text.strip().lower()
    if not needle:
        die(2, "give some text to search for.")
    more = False
    if args.space:
        names = [need_space(args.space)]
    else:
        names, more = all_space_names()
    spaces = names[: args.max_spaces]
    not_searched = len(names) - len(spaces)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    hits, cut = [], 0
    for space in spaces:
        rows, truncated = list_messages(space, after=since)
        cut += 1 if truncated else 0
        hits += [m for m in rows if needle in str(m.get("text") or "").lower()]    # client side
    hits = by_time(hits)[-args.n:]
    for m in hits:
        show(m)
    partial = []
    if not_searched or more:
        partial.append(f"{not_searched}{'+' if more else ''} spaces not searched (limit {args.max_spaces})")
    if cut:
        partial.append(f"{cut} searched spaces were cut at the page limit, so older matches may be missing")
    if partial:
        print(f"({len(hits)} found in {len(spaces)} spaces. PARTIAL: {'; '.join(partial)})")
    else:
        print(f"({len(hits)} found in {len(spaces)} spaces, last {args.days} days)")


# ---- send safety ----------------------------------------------------------------------------
def _money_hits(text: str):
    """Money terms from the WhatsApp skill's draft_check, or None when it cannot run (lock)."""
    here = os.path.dirname(os.path.abspath(__file__))  # kit: code path
    return _cs().money_hits(text, here)  # kit: code path


def _gate_mode_fallback() -> str:
    return _cs().gate_mode_fallback()


def _gate_check(args, *, target, text, scope, thread, dry_run, source):
    return _cs().gate_check(_comms_module, _gate_mode_fallback, "gchat", args.money_ok, target=target,
                            text=text, scope=scope, thread=thread, dry_run=dry_run, source=source)


def _gate_begin(ctx):
    return _cs().gate_begin(ctx)


def _gate_finish(ctx, op, status, receipts=()):
    _cs().gate_finish(ctx, op, status, receipts)


# ---- send and reply -------------------------------------------------------------------------
def _send_flow(args, verb: str, space: str, thread_name):
    text = args.text
    if not text.strip():
        die(1, "NOT SENT: the message is empty.")
    if len(text) > MAX_TEXT:
        die(1, f"NOT SENT: Google Chat takes at most {MAX_TEXT} characters in one message.")
    if args.yes and not args.since:
        die(2, "NOT SENT: --since is required with --yes (the createTime of the newest message you "
               "read, exactly as `history` printed it).")
    since = None
    if args.since:
        since = parse_dt(args.since)
        if since is None:
            die(2, "NOT SENT: --since is not a time (use the createTime from `history`, or ISO 8601).")

    if since is not None:
        # The whole space is re-read live: a thread reply anywhere counts as newer.
        newer, truncated = list_messages(space, after=since)
        for m in newer:
            t = parse_dt(m.get("createTime"))
            if t is None or t > since or truncated:
                print(f"NOT SENT: the space has a message newer than --since ({m.get('createTime', 'no time')}). "
                      "Read it again, then redo the send.")
                raise SystemExit(STALE_EXIT)
        if truncated:
            print("NOT SENT: too many newer messages to check. Read the space again, then redo the send.")
            raise SystemExit(STALE_EXIT)

    print(f"{'About to send' if args.yes else 'DRY RUN, nothing sent'}. {verb} on Google Chat:")
    print(f"  Space:   {space}")
    if thread_name:
        print(f"  Thread:  {thread_name}")
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

    account = _conf("gchat.gws_account") or ""
    ctx = _gate_check(args, target=space, text=text, scope=account, thread=thread_name or "",
                      dry_run=not args.yes, source=f"gchat.py {verb}")
    if not args.yes:
        print("Add --yes to send (with --since, the createTime of the newest message you read).")
        return

    params, body = {"parent": space}, {"text": text}
    if thread_name:
        # OR_FAIL: if the thread is gone, fail. The other option would start a new thread.
        params["messageReplyOption"] = "REPLY_MESSAGE_OR_FAIL"
        body["thread"] = {"name": thread_name}
    op = _gate_begin(ctx)
    try:
        result = gws_write(["spaces", "messages", "create"], params, body)
    except UncertainError:
        _gate_finish(ctx, op, "uncertain")
        print("UNCERTAIN SEND: Google Chat did not give a clear answer, so the message may have been "
              "delivered. Do not resend. Open the space and check first.")
        raise SystemExit(UNCERTAIN_EXIT)
    except PermissionDenied:
        _gate_finish(ctx, op, "failed_before_send")
        print(f"NOT SENT: {PERMISSION_LINE}")
        raise SystemExit(PERMISSION_EXIT)
    except ApiError as exc:
        _gate_finish(ctx, op, "failed_before_send")
        print(f"NOT SENT: {exc}. Nothing was archived.")
        raise SystemExit(1)
    except BaseException:
        _gate_finish(ctx, op, "uncertain")
        raise
    mid = result.get("name")
    _gate_finish(ctx, op, "sent", [str(mid)] if mid else [])
    C = _comms_module("commslib")
    if C is not None:
        C.log_event("gchat", "send", space, text=text, message_id=str(mid) if mid else None,
                    scope=account or None, verb=verb, hook_covers=False, command=" ".join(sys.argv))
    print(f"Sent. name={mid or 'unknown'} space={space}")


def cmd_send(args):
    _send_flow(args, "send", need_space(args.space), None)


def cmd_reply(args):
    m = MESSAGE_RE.fullmatch(args.message)
    if not m:
        die(2, "a message name looks like spaces/AAAAxxxx/messages/yyyy (copy it from `history`).")
    space = m.group(1)
    if not args.text.strip():
        die(1, "NOT SENT: the message is empty.")
    if args.yes and not args.since:
        die(2, "NOT SENT: --since is required with --yes (the createTime of the newest message you "
               "read, exactly as `history` printed it).")
    original = gws_read(["spaces", "messages", "get"], {"name": args.message})
    thread_name = (original.get("thread") or {}).get("name")
    if not thread_name:
        print("NOT SENT: that message has no thread to reply into.")
        raise SystemExit(1)
    _send_flow(args, "reply", space, thread_name)


def cmd_mark_read(args):
    space = need_space(args.space)
    if not args.yes:
        print(f"DRY RUN, nothing changed. Would mark {space} as read. Add --yes.")
        return
    params = {"name": f"users/me/{space}/spaceReadState", "updateMask": "last_read_time"}
    body = {"lastReadTime": stamp(dt.datetime.now(dt.timezone.utc))}
    try:
        gws_write(["users", "spaces", "updateSpaceReadState"], params, body)
    except UncertainError:
        print("UNCERTAIN: Google Chat did not give a clear answer. Check the space; marking read is safe to redo.")
        raise SystemExit(UNCERTAIN_EXIT)
    except PermissionDenied:
        die(PERMISSION_EXIT, PERMISSION_LINE)
    except ApiError as exc:
        die(1, f"FAILED: {exc}.")
    print(f"Marked {space} as read.")


# ---- CLI ------------------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="gchat.py", description="Read and send on Google Chat through gws (mock only).")
    sub = p.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("spaces", help="list spaces"); sp.add_argument("-n", type=int, default=50)
    sp.set_defaults(fn=cmd_spaces)
    sp = sub.add_parser("history", help="one space, oldest first"); sp.add_argument("space")
    sp.add_argument("-n", type=int, default=20); sp.add_argument("--days", type=int, default=7)
    sp.set_defaults(fn=cmd_history)
    sp = sub.add_parser("find", help="search message text on the client side"); sp.add_argument("text")
    sp.add_argument("--space"); sp.add_argument("-n", type=int, default=10)
    sp.add_argument("--days", type=int, default=30); sp.add_argument("--max-spaces", type=int, default=10)
    sp.set_defaults(fn=cmd_find)

    def sendopts(sp):
        sp.add_argument("--since", help="createTime of the newest message you read")
        sp.add_argument("--yes", action="store_true"); sp.add_argument("--money-ok", action="store_true")

    sp = sub.add_parser("send", help="send to a space (dry run unless --yes)"); sp.add_argument("space")
    sp.add_argument("text"); sendopts(sp); sp.set_defaults(fn=cmd_send)
    sp = sub.add_parser("reply", help="reply into a message's thread (dry run unless --yes)")
    sp.add_argument("message"); sp.add_argument("text"); sendopts(sp); sp.set_defaults(fn=cmd_reply)
    sp = sub.add_parser("mark-read", help="mark a space read (dry run unless --yes)"); sp.add_argument("space")
    sp.add_argument("--yes", action="store_true"); sp.set_defaults(fn=cmd_mark_read)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    _cs()                      # no shared guards, no run: one line, exit 2
    for name in ("n", "days", "max_spaces"):
        if getattr(args, name, None) is not None:
            setattr(args, name, clamp(getattr(args, name), 3650 if name == "days" else MAX_ROWS))
    args.fn(args)


if __name__ == "__main__":
    main()
