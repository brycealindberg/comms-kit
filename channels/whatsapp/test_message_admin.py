"""reply / edit / delete / mark-read / find: message-level WhatsApp verbs. Network mocked.

The ways each can go wrong, each pinned below:
  reply      - sends without --since or on a stale chat; the in-reply-to points at nothing or at the
               wrong message; the preview drifts from `About to send to` (two parsers read it);
               the gate and recipient guard are skipped; the archive never sees the send.
  edit       - edits someone else's message, or his phone's (the bridge cannot); its preview starts
               `About to send to` (it is not a send); the replace points at an edit event instead of
               the original (a SECOND edit then fails); a refused edit is reported as done; a money
               message is changed unseen; an image is "edited"; `--yes` with no --since.
  delete     - deletes someone else's message; a repeat delete says "Deleted"; a refused redaction is
               reported as done; a money message goes unseen; a PUT with no --yes.
  mark-read  - a receipt without --yes; a receipt on an event that is not in the chat; a receipt the
               homeserver never stored.
  find       - an empty term; edits shown as extra messages; the search scope; the limit hidden.

Run: python3 -m pytest -q test_message_admin.py   (from this directory)
"""
import argparse
import contextlib
import io
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Hermetic config: fake values only, never the real kit config.
import atexit  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402
_CFG_FD, _CFG_PATH = tempfile.mkstemp(suffix=".json")
with os.fdopen(_CFG_FD, "w") as _cfg_file:
    json.dump({"whatsapp": {"matrix_user": "owner", "owner_lid": "4440100",
                            "self_number": "12065550100", "bot_user": "@whatsappbot:x",
                            "homeserver": "https://matrix.example.com"}}, _cfg_file)
atexit.register(os.unlink, _CFG_PATH)
os.environ["COMMS_KIT_CONFIG"] = _CFG_PATH
import whatsapp_mautrix as W  # noqa: E402


class _StubCheck:
    """Stands in for the owner's private draft_check.py, which is not in the kit: just
    enough of its MONEY list for the lock logic under test."""
    MONEY = re.compile(r"\$\s?\d|deposit|invoic|retainer|refund|price|quote|\bfee\b", re.I)

    @staticmethod
    def normalize_for_matching(text):
        return text


def setUpModule():
    if W._load_draft_check() is None:
        patcher = mock.patch.object(W, "_load_draft_check", return_value=_StubCheck)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)

ROOM = "!room:x"
TOOL = "@owner:x"                             # the owner through this CLI
PHONE = "@whatsapp_lid-4440100:x"      # the owner on their phone
THEM = "@whatsapp_lid-231339907412126:x"     # the other person
BOT = "@whatsappbot:x"
NOW_MS = 1790190700000


def msg(eid, ts, sender=THEM, body="Thanks so much!", msgtype="m.text", extra=None):
    content = {"msgtype": msgtype, "body": body}
    content.update(extra or {})
    return {"type": "m.room.message", "event_id": eid, "origin_server_ts": ts,
            "sender": sender, "room_id": ROOM, "content": content}


def replace_event(eid, ts, target, new_body, sender=TOOL):
    return msg(eid, ts, sender=sender, body="* " + new_body, extra={
        "m.new_content": {"msgtype": "m.text", "body": new_body},
        "m.relates_to": {"rel_type": "m.replace", "event_id": target}})


class FakeHS:
    """Just enough homeserver for the message verbs. Every call is recorded."""

    def __init__(self, events=None):
        self.events = list(events or [])          # room timeline, any order
        self.replaces = {}                        # target id -> [replace events]
        self.calls = []
        self.after_write = []                     # events that appear once a PUT happened
        self.redacted = set()                     # ids whose content the server blanked
        self.receipts = []                        # m.receipt ephemeral events for /sync
        self.search_results = []
        self.put_event_id = "$new"
        self.fail_put = None

    def written(self):
        return any(m in ("PUT", "POST") and "/search" not in p for m, p, _b in self.calls)

    def writes(self):
        return [(m, p, b) for m, p, b in self.calls
                if m in ("PUT", "POST") and "/search" not in p and "/createRoom" not in p]

    def lookup(self, eid):
        for e in self.events + self.after_write:
            if e["event_id"] == eid:
                if eid in self.redacted:
                    return {**e, "content": {}, "unsigned": {"redacted_because": {"type": "m.room.redaction"}}}
                return e
        return None

    def request(self, method, path, token=None, body=None, params=None):
        self.calls.append((method, path, body))
        unq = W.urllib.parse.unquote
        if method == "GET" and "/relations/" in path:
            target = unq(path.split("/relations/")[1].split("/")[0])
            return {"chunk": self.replaces.get(target, [])}
        if method == "GET" and "/event/" in path:
            ev = self.lookup(unq(path.rsplit("/", 1)[1]))
            if ev is None:
                if unq(path.rsplit("/", 1)[1]) == self.put_event_id:
                    return {"event_id": self.put_event_id, "origin_server_ts": NOW_MS + 100}
                raise W.MatrixError(404, "M_NOT_FOUND")
            return ev
        if method == "GET" and path.endswith("/messages"):
            live = self.events + (self.after_write if self.written() else [])
            return {"chunk": sorted(live, key=lambda e: -e["origin_server_ts"]), "end": "t2"}
        if method == "GET" and path.endswith("/joined_members"):
            return {"joined": {}}
        if method == "GET" and path.endswith("/account/whoami"):
            return {"user_id": TOOL}
        if method == "GET" and path.endswith("/sync"):
            return {"rooms": {"join": {ROOM: {"ephemeral": {"events": self.receipts}}}}}
        if method == "POST" and path.endswith("/search"):
            return {"search_categories": {"room_events": {"count": len(self.search_results),
                                                          "results": self.search_results}}}
        if method in ("PUT", "POST"):
            if self.fail_put:
                raise W.MatrixError(*self.fail_put)
            if "/redact/" in path:
                self.redacted.add(unq(path.split("/redact/")[1].split("/")[0]))
            return {"event_id": self.put_event_id} if method == "PUT" else {}
        raise AssertionError(f"unexpected {method} {path}")


def run(fn, hs, args, chat=None, extra_patches=()):
    """Run a cmd_* function with the network faked. Returns (stdout, exit code)."""
    chat = chat or {"portal_id": ROOM, "is_group": False, "label": "Alex Doe"}
    out, code = io.StringIO(), 0
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(W, "_require_session", return_value=("tok", "!mgmt:x")))
        stack.enter_context(mock.patch.object(W, "_resolve_chat", return_value=chat))
        stack.enter_context(mock.patch.object(W, "_request", side_effect=hs.request))
        stack.enter_context(mock.patch.object(W, "_read_self_number_cache", return_value=""))
        stack.enter_context(mock.patch.object(W.time, "sleep"))
        stack.enter_context(mock.patch.object(W, "_log_send"))
        stack.enter_context(mock.patch.object(W, "_gate_check", return_value=None))
        for p in extra_patches:
            stack.enter_context(p)
        stack.enter_context(contextlib.redirect_stdout(out))
        try:
            fn(args)
        except SystemExit as e:
            code = e.code
    return out.getvalue(), code or 0


def ns(**kw):
    base = dict(identifier="Alex Doe", event="$mine", message=["hello"], yes=False, since=None,
                money_ok=False, override_check=None, resend_ok=False, group_ok=False, exclude=None)
    base.update(kw)
    return argparse.Namespace(**base)


def put_content(hs):
    [(_m, _p, body)] = [w for w in hs.writes() if "/send/m.room.message/" in w[1]]
    return body


def timeline(*extra):
    return [msg("$theirs", NOW_MS - 5000, body="Can you confirm?"),
            msg("$mine", NOW_MS - 3000, sender=TOOL, body="Sure thing"),
            *extra]


# ------------------------------------------------------------------------------------- reply
class Reply(unittest.TestCase):
    def setUp(self):
        os.environ.pop("OUTBOUND_GUARDS", None)
        self.hs = FakeHS(timeline())

    def go(self, **kw):
        kw.setdefault("event", "$theirs")
        kw.setdefault("message", ["On it"])
        return run(W.cmd_reply, self.hs, ns(**kw))

    def test_dry_run_uses_the_exact_send_preview_line_and_writes_nothing(self):
        out, code = self.go()
        self.assertEqual(code, 0)
        self.assertEqual(self.hs.writes(), [])
        first = [l for l in out.splitlines() if l.startswith("About to send to")]
        self.assertEqual(first, [f'About to send to Alex Doe [room {ROOM}]: "On it"'])
        self.assertIn("Dry run", out)

    def test_group_preview_is_labelled_group(self):
        out, _ = run(W.cmd_reply, self.hs, ns(event="$theirs", message=["On it"]),
                     chat={"portal_id": ROOM, "is_group": True, "label": "Team"})
        self.assertIn(f'About to send to GROUP "Team" [room {ROOM}]: "On it"', out)

    def test_yes_without_since_refuses_and_writes_nothing(self):
        out, code = self.go(yes=True)
        self.assertEqual(code, 2)
        self.assertIn("NOT SENT", out)
        self.assertEqual(self.hs.writes(), [])

    def test_stale_since_refuses_with_exit_5(self):
        out, code = self.go(yes=True, since=(NOW_MS - 9000) / 1000.0)
        self.assertEqual(code, W.STALE_EXIT)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("NOT SENT", out)

    def test_yes_sends_one_event_with_in_reply_to_and_the_send_receipt(self):
        out, code = self.go(yes=True, since=(NOW_MS - 3000) / 1000.0)
        self.assertEqual(code, 0, out)
        self.assertEqual(put_content(self.hs), {
            "msgtype": "m.text", "body": "On it",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$theirs"}}})
        self.assertIn("Sent. event_id=$new", out)

    def test_unknown_event_refuses_before_any_write(self):
        out, code = self.go(event="$nope", yes=True, since=(NOW_MS - 3000) / 1000.0)
        self.assertEqual(code, 1)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("NOT SENT", out)

    def test_a_deleted_message_cannot_be_replied_to(self):
        self.hs.redacted.add("$theirs")
        out, code = self.go()
        self.assertEqual(code, 1)
        self.assertIn("deleted", out)

    def test_empty_message_refused(self):
        out, code = self.go(message=["  "])
        self.assertEqual(code, 1)
        self.assertEqual(self.hs.calls, [])

    def test_gate_sees_the_final_text_and_dry_run_flag(self):
        gate = mock.Mock(return_value=None)
        run(W.cmd_reply, self.hs, ns(event="$theirs", message=["On it"]),
            extra_patches=[mock.patch.object(W, "_gate_check", gate)])
        kwargs = gate.call_args.kwargs
        self.assertEqual(kwargs["texts"], ["On it"])
        self.assertTrue(kwargs["dry_run"])
        self.assertEqual(kwargs["target"], ROOM)

    def test_recipient_guard_runs_when_flag_on(self):
        guard = mock.Mock()
        with mock.patch.dict(os.environ, {"OUTBOUND_GUARDS": "1"}):
            run(W.cmd_reply, self.hs, ns(event="$theirs"),
                extra_patches=[mock.patch.object(W, "_apply_recipient_guard", guard)])
        guard.assert_called_once()

    def test_the_send_is_archived_with_its_event_id(self):
        log = mock.Mock()
        run(W.cmd_reply, self.hs, ns(event="$theirs", message=["On it"], yes=True,
                                     since=(NOW_MS - 3000) / 1000.0),
            extra_patches=[mock.patch.object(W, "_log_send", log)])
        args = log.call_args.args
        self.assertEqual(args[1], "On it")
        self.assertEqual(args[2], "$new")
        self.assertEqual(args[3], "reply")


# -------------------------------------------------------------------------------------- edit
class Edit(unittest.TestCase):
    def setUp(self):
        os.environ.pop("OUTBOUND_GUARDS", None)
        self.hs = FakeHS(timeline())
        self.since = (NOW_MS - 3000) / 1000.0

    def go(self, **kw):
        kw.setdefault("message", ["Sure, will do"])
        return run(W.cmd_edit, self.hs, ns(**kw), extra_patches=[
            mock.patch.object(W.time, "time", return_value=NOW_MS / 1000.0)])

    def test_preview_never_starts_with_about_to_send_to(self):
        for kw in ({}, {"event": "$theirs"}, {"yes": True}, {"event": "$nope"}):
            out, _ = self.go(**kw)
            for line in out.splitlines():
                self.assertFalse(line.startswith("About to send to"), (kw, line))
        out, _ = self.go()
        self.assertIn("About to edit your message", out)
        self.assertIn('"Sure thing" -> "Sure, will do"', out)

    def test_dry_run_writes_nothing(self):
        out, code = self.go()
        self.assertEqual(code, 0)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("Dry run", out)

    def _edit_with_fake_archive(self, **kw):
        comms = mock.Mock()
        kw.setdefault("message", ["Sure, will do"])
        out, code = run(W.cmd_edit, self.hs, ns(**kw), extra_patches=[
            mock.patch.object(W.time, "time", return_value=NOW_MS / 1000.0),
            mock.patch.object(W, "_comms_module",
                              side_effect=lambda name: comms if name == "commslib" else None)])
        return comms, out, code

    def test_a_sent_edit_is_archived_once_as_an_edit(self):
        comms, out, code = self._edit_with_fake_archive(yes=True, since=self.since)
        self.assertEqual(code, 0, out)
        comms.log_event.assert_called_once()
        call = comms.log_event.call_args
        self.assertEqual(call.args[:3], ("whatsapp", "send", "Alex Doe"))
        self.assertEqual(call.kwargs["text"], "Sure, will do")
        self.assertEqual(call.kwargs["verb"], "edit")
        # The hook learns receipts only from `Sent. event_id=`, which an edit never prints. A row
        # keyed on an id would be a second row in a hook runtime, so the CLI row carries none.
        self.assertIsNone(call.kwargs.get("message_id"))

    def test_a_dry_run_or_refused_edit_is_not_archived(self):
        for kw in ({}, {"event": "$theirs", "yes": True, "since": self.since}):
            comms, _, _ = self._edit_with_fake_archive(**kw)
            comms.log_event.assert_not_called()

    def test_someone_elses_message_is_refused(self):
        out, code = self.go(event="$theirs", yes=True, since=self.since)
        self.assertEqual(code, 1)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("NOT EDITED", out)
        self.assertIn("did not send", out)

    def test_his_phone_message_is_refused_because_the_bridge_cannot_edit_it(self):
        self.hs.events.append(msg("$phone", NOW_MS - 1000, sender=PHONE, body="from my phone"))
        out, code = self.go(event="$phone", yes=True, since=(NOW_MS - 1000) / 1000.0)
        self.assertEqual(code, 1)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("phone", out)

    def test_yes_without_since_refuses(self):
        out, code = self.go(yes=True)
        self.assertEqual(code, 2)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("--since", out)

    def test_stale_chat_refuses_with_exit_5(self):
        out, code = self.go(yes=True, since=(NOW_MS - 9000) / 1000.0)
        self.assertEqual(code, W.STALE_EXIT)
        self.assertEqual(self.hs.writes(), [])

    def test_yes_sends_one_m_replace_pointing_at_the_original(self):
        out, code = self.go(yes=True, since=self.since)
        self.assertEqual(code, 0, out)
        self.assertEqual(put_content(self.hs), {
            "msgtype": "m.text", "body": "* Sure, will do",
            "m.new_content": {"msgtype": "m.text", "body": "Sure, will do"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$mine"}})
        self.assertIn("Edited. event_id=$new", out)

    def test_second_edit_shows_the_latest_text_and_targets_the_original(self):
        self.hs.events.append(replace_event("$edit1", NOW_MS - 2000, "$mine", "First edit"))
        self.hs.replaces["$mine"] = [self.hs.events[-1]]
        out, _ = self.go(yes=False)
        self.assertIn('"First edit" -> "Sure, will do"', out)
        out, code = self.go(yes=True, since=(NOW_MS - 2000) / 1000.0)
        self.assertEqual(code, 0, out)
        self.assertEqual(put_content(self.hs)["m.relates_to"]["event_id"], "$mine")

    def test_passing_an_edit_event_id_is_resolved_to_the_original(self):
        self.hs.events.append(replace_event("$edit1", NOW_MS - 2000, "$mine", "First edit"))
        self.hs.replaces["$mine"] = [self.hs.events[-1]]
        out, code = self.go(event="$edit1", yes=True, since=(NOW_MS - 2000) / 1000.0)
        self.assertEqual(code, 0, out)
        self.assertEqual(put_content(self.hs)["m.relates_to"]["event_id"], "$mine")

    def test_bridge_refusal_is_a_failure_not_a_success(self):
        self.hs.after_write = [msg("$botnote", NOW_MS + 500, sender=BOT, msgtype="m.notice",
                                   body="⚠️ Your edit was not bridged: too old")]
        out, code = self.go(yes=True, since=self.since)
        self.assertEqual(code, 4)
        self.assertIn("NOT EDITED", out)
        self.assertNotIn("Edited. event_id", out)

    def test_server_error_is_a_failure(self):
        self.hs.fail_put = (403, "M_FORBIDDEN")
        out, code = self.go(yes=True, since=self.since)
        self.assertNotEqual(code, 0)
        self.assertNotIn("Edited. event_id", out)

    def test_money_lock_on_the_old_text_and_on_the_new_text(self):
        self.hs.events[1] = msg("$mine", NOW_MS - 3000, sender=TOOL, body="The fee is $500")
        out, code = self.go(yes=True, since=self.since)
        self.assertEqual(code, W.MONEY_LOCK_EXIT)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("MONEY LOCK", out)
        out, code = self.go(yes=True, since=self.since, money_ok=True)
        self.assertEqual(code, 0, out)
        # new text carries the price while the old one did not
        self.hs = FakeHS(timeline())
        out, code = self.go(message=["Make it $900 then"], yes=True, since=self.since)
        self.assertEqual(code, W.MONEY_LOCK_EXIT)
        self.assertEqual(self.hs.writes(), [])

    def test_media_message_cannot_be_edited(self):
        self.hs.events[1] = msg("$mine", NOW_MS - 3000, sender=TOOL, body="pic.jpg", msgtype="m.image")
        out, code = self.go(yes=True, since=self.since)
        self.assertEqual(code, 1)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("text", out)

    def test_same_text_is_a_no_op(self):
        out, code = self.go(message=["Sure thing"], yes=True, since=self.since)
        self.assertEqual(code, 0)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("same", out)

    def test_deleted_message_cannot_be_edited(self):
        self.hs.redacted.add("$mine")
        out, code = self.go(yes=True, since=self.since)
        self.assertEqual(code, 1)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("deleted", out)

    def test_empty_new_text_refused_before_any_call(self):
        out, code = self.go(message=[" "])
        self.assertEqual(code, 1)
        self.assertEqual(self.hs.calls, [])

    def test_old_message_gets_the_fifteen_minute_warning(self):
        self.hs.events[1] = msg("$mine", NOW_MS - 20 * 60 * 1000, sender=TOOL, body="Sure thing")
        out, _ = self.go()
        self.assertIn("15 minutes", out)

    def test_gate_and_recipient_guard_run(self):
        gate, guard = mock.Mock(return_value=None), mock.Mock()
        with mock.patch.dict(os.environ, {"OUTBOUND_GUARDS": "1"}):
            run(W.cmd_edit, self.hs, ns(message=["Sure, will do"]), extra_patches=[
                mock.patch.object(W, "_gate_check", gate),
                mock.patch.object(W, "_apply_recipient_guard", guard)])
        self.assertEqual(gate.call_args.kwargs["texts"], ["Sure, will do"])
        guard.assert_called_once()


# ------------------------------------------------------------------------------------ delete
class Delete(unittest.TestCase):
    def setUp(self):
        self.hs = FakeHS(timeline())

    def go(self, **kw):
        return run(W.cmd_delete, self.hs, ns(**kw))

    def test_dry_run_previews_and_writes_nothing(self):
        out, code = self.go()
        self.assertEqual(code, 0)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("About to delete your message", out)
        self.assertNotIn("About to send to", out)
        self.assertIn("Dry run", out)

    def test_someone_elses_message_is_refused(self):
        out, code = self.go(event="$theirs", yes=True)
        self.assertEqual(code, 1)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("NOT DELETED", out)

    def test_his_phone_message_is_refused(self):
        self.hs.events.append(msg("$phone", NOW_MS - 1000, sender=PHONE, body="from my phone"))
        out, code = self.go(event="$phone", yes=True)
        self.assertEqual(code, 1)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("phone", out)

    def test_yes_redacts_the_original_and_says_deleted(self):
        out, code = self.go(yes=True)
        self.assertEqual(code, 0, out)
        [(m, path, _b)] = self.hs.writes()
        self.assertEqual(m, "PUT")
        self.assertIn("/redact/%24mine/", path)
        self.assertIn("Deleted. event_id=$new", out)

    def test_deleting_an_edit_event_id_redacts_the_original(self):
        self.hs.events.append(replace_event("$edit1", NOW_MS - 2000, "$mine", "First edit"))
        out, code = self.go(event="$edit1", yes=True)
        self.assertEqual(code, 0, out)
        [(_m, path, _b)] = self.hs.writes()
        self.assertIn("/redact/%24mine/", path)

    def test_already_deleted_does_not_report_success(self):
        self.hs.redacted.add("$mine")
        out, code = self.go(yes=True)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("already deleted", out)
        self.assertNotIn("Deleted. event_id", out)
        self.assertEqual(code, 0)

    def test_bridge_refusal_is_a_failure(self):
        self.hs.after_write = [msg("$botnote", NOW_MS + 500, sender=BOT, msgtype="m.notice",
                                   body="⚠️ Failed to delete the message on WhatsApp")]
        out, code = self.go(yes=True)
        self.assertEqual(code, 4)
        self.assertIn("NOT DELETED", out)
        self.assertNotIn("Deleted. event_id", out)

    def test_redaction_that_did_not_take_is_a_failure(self):
        # the server answers with an id but the message is still readable
        orig = self.hs.request

        def keep_alive(method, path, **kw):
            res = orig(method, path, **kw)
            self.hs.redacted.clear()
            return res
        out, code = run(W.cmd_delete, self.hs, ns(yes=True),
                        extra_patches=[mock.patch.object(W, "_request", side_effect=keep_alive)])
        self.assertEqual(code, 4)
        self.assertNotIn("Deleted. event_id", out)

    def test_money_lock_applies_to_deleting_a_price(self):
        self.hs.events[1] = msg("$mine", NOW_MS - 3000, sender=TOOL, body="Total is $500")
        out, code = self.go(yes=True)
        self.assertEqual(code, W.MONEY_LOCK_EXIT)
        self.assertEqual(self.hs.writes(), [])
        out, code = self.go(yes=True, money_ok=True)
        self.assertEqual(code, 0, out)

    def test_unknown_event_refuses(self):
        out, code = self.go(event="$nope", yes=True)
        self.assertEqual(code, 1)
        self.assertEqual(self.hs.writes(), [])


# --------------------------------------------------------------------------------- mark-read
class MarkRead(unittest.TestCase):
    def setUp(self):
        self.hs = FakeHS(timeline())

    def go(self, **kw):
        base = dict(identifier="Alex Doe", event=None, yes=False)
        base.update(kw)
        return run(W.cmd_mark_read, self.hs, argparse.Namespace(**base))

    def test_dry_run_posts_nothing_and_names_the_newest_message(self):
        out, code = self.go()
        self.assertEqual(code, 0)
        self.assertEqual(self.hs.writes(), [])
        self.assertIn("About to mark read", out)
        self.assertNotIn("About to send to", out)
        self.assertIn("$mine", out)

    def test_yes_posts_one_read_receipt_on_the_newest_message(self):
        self.hs.receipts = [{"type": "m.receipt", "content": {"$mine": {"m.read": {TOOL: {"ts": 1}}}}}]
        out, code = self.go(yes=True)
        self.assertEqual(code, 0, out)
        [(m, path, _b)] = self.hs.writes()
        self.assertEqual(m, "POST")
        self.assertTrue(path.endswith("/receipt/m.read/%24mine"), path)
        self.assertIn("Marked read", out)

    def test_event_pin_wins(self):
        self.hs.receipts = [{"type": "m.receipt", "content": {"$theirs": {"m.read": {TOOL: {"ts": 1}}}}}]
        out, code = self.go(event="$theirs", yes=True)
        self.assertEqual(code, 0, out)
        self.assertTrue(self.hs.writes()[0][1].endswith("/receipt/m.read/%24theirs"))

    def test_event_not_in_this_chat_refuses(self):
        out, code = self.go(event="$nope", yes=True)
        self.assertEqual(code, 1)
        self.assertEqual(self.hs.writes(), [])

    def test_empty_chat_refuses(self):
        self.hs.events = []
        out, code = self.go(yes=True)
        self.assertEqual(code, 1)
        self.assertEqual(self.hs.writes(), [])

    def test_receipt_the_homeserver_never_stored_is_not_reported_as_done(self):
        self.hs.receipts = []
        out, code = self.go(yes=True)
        self.assertEqual(code, 4)
        self.assertNotIn("Marked read", out)


# ------------------------------------------------------------------------------------- find
class Find(unittest.TestCase):
    def setUp(self):
        self.hs = FakeHS()
        self.hs.search_results = [
            {"result": msg("$a", NOW_MS - 1000, body="invoice sent", sender=THEM) | {"room_id": ROOM}},
            {"result": replace_event("$e", NOW_MS - 500, "$a", "invoice sent today", sender=THEM)
                       | {"room_id": ROOM}},
            {"result": msg("$b", NOW_MS - 4000, body="old invoice", sender=THEM) | {"room_id": ROOM}},
            {"result": msg("$c", NOW_MS - 100, body="invoice bot", sender=BOT) | {"room_id": ROOM}},
        ]

    def go(self, **kw):
        base = dict(text="invoice", chat=None, number=10)
        base.update(kw)
        return run(W.cmd_find, self.hs, argparse.Namespace(**base), extra_patches=[
            mock.patch.object(W, "_room_name", return_value="Alex Doe")])

    def test_it_never_writes(self):
        self.go()
        self.assertEqual(self.hs.writes(), [])

    def test_edit_collapses_into_its_original_and_bot_rows_are_dropped(self):
        out, code = self.go()
        self.assertEqual(code, 0)
        self.assertEqual(out.count("$a"), 1)
        self.assertIn("invoice sent today", out)
        self.assertIn("(edited)", out)
        self.assertNotIn("invoice bot", out)

    def test_with_two_edits_the_newest_text_wins_whatever_order_the_server_returns(self):
        first = replace_event("$e1", NOW_MS - 900, "$a", "first edit", sender=THEM) | {"room_id": ROOM}
        second = replace_event("$e2", NOW_MS - 300, "$a", "second edit", sender=THEM) | {"room_id": ROOM}
        orig = msg("$a", NOW_MS - 1000, body="invoice v0", sender=THEM) | {"room_id": ROOM}
        for order in ([second, first, orig], [orig, first, second], [first, second, orig]):
            self.hs.search_results = [{"result": r} for r in order]
            out, _ = self.go()
            self.assertIn("second edit", out, order)
            self.assertNotIn("first edit", out)
            self.assertNotIn("invoice v0", out)

    def test_limit_is_applied_and_stated(self):
        out, _ = self.go(number=1)
        self.assertEqual(sum(1 for l in out.splitlines() if "[$" in l), 1)
        self.assertIn("whole words", out)
        self.assertIn("limit 1", out)

    def test_empty_term_refused_before_any_call(self):
        out, code = self.go(text="  ")
        self.assertEqual(code, 1)
        self.assertEqual(self.hs.calls, [])

    def test_chat_scope_is_sent_as_a_room_filter(self):
        self.go(chat="Alex Doe")
        [(_m, _p, body)] = [c for c in self.hs.calls if c[1].endswith("/search")]
        self.assertEqual(body["search_categories"]["room_events"]["filter"]["rooms"], [ROOM])

    def test_chat_scope_falls_back_to_a_stated_substring_scan_when_the_server_finds_none(self):
        self.hs.search_results = []
        self.hs.events = [msg("$s", NOW_MS - 100, body="the invoices are ready")]
        out, _ = self.go(chat="Alex Doe", text="invoic")
        self.assertIn("$s", out)
        self.assertIn("scanned the last", out)

    def test_no_result_says_so(self):
        self.hs.search_results = []
        out, code = self.go()
        self.assertEqual(code, 0)
        self.assertIn("No messages", out)


def _use_config(testcase, data):
    """Point the kit config at a temp file holding `data` for one test."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    testcase.addCleanup(os.unlink, path)
    patcher = mock.patch.dict(os.environ, {"COMMS_KIT_CONFIG": path})
    patcher.start()
    testcase.addCleanup(patcher.stop)


class CoreSearchOrder(unittest.TestCase):
    """Core is found by COMMS_KIT_CORE, then <file>/../../core, then the live folder.
    A wrong order loads the wrong gate, or none, and a send goes out unchecked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.abspath(self.tmp.name)

    def _core(self, *parts):
        folder = os.path.join(self.root, *parts)
        os.makedirs(folder)
        with open(os.path.join(folder, "commslib.py"), "w") as fh:
            fh.write("")
        return folder

    def _find(self, file_path, env):
        with mock.patch.dict(os.environ, env), mock.patch.object(W, "__file__", file_path):
            if "COMMS_KIT_CORE" not in env:
                os.environ.pop("COMMS_KIT_CORE", None)
            return W._core_dir()

    def test_the_env_folder_wins_over_the_repo_folder(self):
        env_core = self._core("env-core")
        self._core("repo", "core")
        found = self._find(os.path.join(self.root, "repo", "channels", "whatsapp", "w.py"),
                           {"COMMS_KIT_CORE": env_core})
        self.assertEqual(found, env_core)

    def test_the_repo_folder_is_used_without_the_env(self):
        repo_core = self._core("repo", "core")
        found = self._find(os.path.join(self.root, "repo", "channels", "whatsapp", "w.py"), {})
        self.assertEqual(found, repo_core)

    def test_an_env_folder_without_the_module_falls_through(self):
        empty = os.path.join(self.root, "empty")
        os.makedirs(empty)
        repo_core = self._core("repo", "core")
        found = self._find(os.path.join(self.root, "repo", "channels", "whatsapp", "w.py"),
                           {"COMMS_KIT_CORE": empty})
        self.assertEqual(found, repo_core)

    def test_the_live_folder_is_the_last_stop(self):
        found = self._find(os.path.join(self.root, "lone", "w.py"), {})
        self.assertEqual(found, os.path.abspath(os.path.expanduser("~/.claude/communications")))


class RecipientGuardFailsClosed(unittest.TestCase):
    """OUTBOUND_GUARDS=1 with no guard folder must refuse the send (exit 3), never pass."""

    ARGS = argparse.Namespace(identifier="Alex Doe", group_ok=False, exclude=[])
    CHAT = {"portal_id": ROOM, "is_group": False, "label": "Alex Doe"}

    def _guard(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            W._apply_recipient_guard("tok", self.CHAT, ROOM, self.ARGS)
        return caught.exception.code, out.getvalue()

    def test_an_unset_key_refuses(self):
        _use_config(self, {"whatsapp": {"matrix_user": "owner"}})
        code, out = self._guard()
        self.assertEqual(code, 3)
        self.assertIn("whatsapp.recipient_guard_dir", out)

    def test_a_folder_without_the_module_refuses(self):
        with tempfile.TemporaryDirectory() as empty:
            _use_config(self, {"whatsapp": {"recipient_guard_dir": empty}})
            with mock.patch.dict(sys.modules):
                sys.modules.pop("outbound_guards", None)
                code, _ = self._guard()
        self.assertEqual(code, 3)

    def test_the_configured_folder_is_the_one_imported(self):
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "outbound_guards.py"), "w") as fh:
                fh.write("def evaluate_recipient(audience, group_intended=False, exclude=()):\n"
                         "    return {'allow': False, 'reason': 'blocked by the configured guard'}\n"
                         "def describe_audience(audience):\n    return ','.join(audience)\n")
            _use_config(self, {"whatsapp": {"recipient_guard_dir": folder}})
            with mock.patch.dict(sys.modules), mock.patch.object(sys, "path", list(sys.path)):
                sys.modules.pop("outbound_guards", None)
                code, out = self._guard()
        self.assertEqual(code, 2)
        self.assertIn("blocked by the configured guard", out)


class OwnerChecksFailClosed(unittest.TestCase):
    """The owner check compares against the configured Matrix user. With none set it must
    stop, because an empty prefix would call every sender the owner."""

    def test_no_matrix_user_stops_the_check(self):
        _use_config(self, {"whatsapp": {}})
        with self.assertRaises(W.kitconfig.ConfigError):
            W._is_owner("@whatsapp_lid-1:x")

    def test_the_owner_and_a_stranger(self):
        _use_config(self, {"whatsapp": {"matrix_user": "owner"}})
        self.assertTrue(W._is_owner("@owner:x"))
        self.assertFalse(W._is_owner("@whatsapp_lid-1:x"))


class VoiceSettingsMissing(unittest.TestCase):
    """A missing voice setting ends in one clear line and exit 2, with no traceback."""

    def test_missing_voice_id_names_the_setting(self):
        _use_config(self, {"whatsapp": {"voice_id_var": "WA_TEST_VOICE_ID"}})
        with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "k"}), \
             mock.patch.object(W, "VOICE_ENV_FILE", ""):
            os.environ.pop("WA_TEST_VOICE_ID", None)
            with self.assertRaises(W.VoiceConfigError) as caught:
                W._eleven_credentials()
        self.assertIn("WA_TEST_VOICE_ID", str(caught.exception))

    def test_a_voice_config_error_ends_the_command_with_one_line_and_exit_2(self):
        out = io.StringIO()
        boom = W.VoiceConfigError("ElevenLabs voice settings are missing: X.")
        with mock.patch.object(sys, "argv", ["wa", "send-voice", "12065550100", "hello"]), \
             mock.patch.object(W, "_run_logged", side_effect=boom), \
             contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            W.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(len(out.getvalue().strip().splitlines()), 1)

    def test_other_voice_failures_still_exit_1(self):
        with mock.patch.object(sys, "argv", ["wa", "send-voice", "12065550100", "hello"]), \
             mock.patch.object(W, "_run_logged", side_effect=W.VoiceMemoError("x")), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            W.main()
        self.assertEqual(caught.exception.code, 1)

    def test_a_voice_id_in_config_is_used(self):
        _use_config(self, {"whatsapp": {"voice_id": "A" * 20}})
        with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": "k"}), \
             mock.patch.object(W, "VOICE_ENV_FILE", ""):
            os.environ.pop("WHATSAPP_VOICE_ID", None)
            self.assertEqual(W._eleven_credentials(), ("k", "A" * 20))


if __name__ == "__main__":
    unittest.main()
