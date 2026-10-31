#!/usr/bin/env python3
"""Tests for instantly.py with a fake HTTP layer. No vault, no network.

Failure modes covered (written before the code):
  1. a POST runs without --yes
  2. --yes without --since still sends
  3. a thread with a newer message still sends
  4. a reply goes to something that is not a received reply
  5. the money lock is skipped
  6. a refusing outbound gate still lets the send through
  7. a 500 exits 0 or archives a row
  8. a timeout is retried, or not called "uncertain"
  9. a key leaks into output or an error
 10. a good send archives zero or two rows, or hook_covers is not False
 11. a missing key lookup exits with the wrong code
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import instantly as I  # noqa: E402

SECRET = "-".join(["tok", "FAKE", "KEY", "not", "real"])
T0 = "2026-10-30T10:00:00.000Z"
T1 = "2026-10-30T11:00:00.000Z"
T2 = "2026-10-30T12:00:00.000Z"
SINCE_OK = T1   # the received reply's own time


def received(**kw):
    row = {"id": "e-recv", "thread_id": "t-1", "ue_type": 2, "is_unread": 1,
           "from_address_email": "lead@example.com", "eaccount": "sender@example.org",
           "to_address_email_list": "sender@example.org", "subject": "Quick question",
           "timestamp_email": T1, "body": {"text": "Sounds good, tell me more."},
           "content_preview": "Sounds good"}
    row.update(kw)
    return row


def sent(**kw):
    row = received(id="e-sent", ue_type=1, is_unread=0, from_address_email="sender@example.org",
                   eaccount="sender@example.org", to_address_email_list="lead@example.com",
                   timestamp_email=T0, body={"text": "Hi there"})
    row.update(kw)
    return row


class FakeHTTP:
    """Replaces instantly.http_request. Records every call."""

    def __init__(self, emails=None, thread=None, post=None, post_exc=None):
        self.calls = []
        self.emails = emails if emails is not None else {"e-recv": received()}
        self.thread = thread if thread is not None else [sent(), received()]
        self.post = post if post is not None else (I.HTTP_OK, {"id": "new-1", "thread_id": "t-1"})
        self.post_exc = post_exc

    def __call__(self, method, path, key, params=None, body=None, timeout=30):
        self.calls.append((method, path, dict(params or {}), body, key))
        if method == "POST":
            if self.post_exc:
                raise self.post_exc
            return self.post
        if path.startswith("/emails/") and path != "/emails/":
            row = self.emails.get(path.split("/")[2])
            return (I.HTTP_OK, row) if row else (404, {"message": "nope"})
        if path == "/emails":
            search = (params or {}).get("search", "")
            if search.startswith("thread:"):
                return I.HTTP_OK, {"items": self.thread}
            return I.HTTP_OK, {"items": list(self.emails.values())}
        return 404, {}

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


class FakeGate:
    """Stands in for outbound_gate."""
    class GateDenied(Exception):
        pass

    class Approvals:
        @classmethod
        def from_env(cls, **kw):
            return cls()

    class OutboundPayload:
        def __init__(self, channel, **kw):
            self.channel, self.kw = channel, kw

    def __init__(self, allow=True):
        self.allow, self.finished, self.begun = allow, [], 0

    def evaluate(self, payload, approvals, dry_run=False, source=""):
        gate = self

        class V:
            allow = gate.allow

            def deny_line(self):
                return "NOT SENT: outbound gate refused."
        return V()

    def begin(self, payload, approvals):
        self.begun += 1
        return "op-1"

    def finish(self, op, status, receipts=()):
        self.finished.append((op, status, list(receipts)))


class FakeComms:
    def __init__(self):
        self.rows = []

    def log_event(self, *a, **kw):
        self.rows.append((a, kw))
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.http = FakeHTTP()
        self.gate = FakeGate()
        self.comms = FakeComms()
        self.money = []   # returned by the money check
        mods = {"outbound_gate": self.gate, "commslib": self.comms}
        env = {k: v for k, v in os.environ.items() if k != "INSTANTLY_API_KEY"}
        patches = [
            mock.patch.object(I, "http_request", self.http),
            mock.patch.object(I, "_comms_module", lambda name: mods.get(name)),
            mock.patch.object(I, "_money_hits", lambda text: list(self.money)),
            mock.patch.object(I, "load_workspaces", lambda: {"acme": ["fake-key-cmd"]}),
            mock.patch.object(I, "_run_key_cmd", lambda cmd: SECRET),
            mock.patch.dict(os.environ, env, clear=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                I.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
                if isinstance(e.code, str):
                    err.write(e.code)
        return code, out.getvalue(), err.getvalue()

    def reply(self, *extra, text="Happy to help, here is more.", since=SINCE_OK):
        args = ["reply", "e-recv", text, "--workspace", "acme"]
        if since:
            args += ["--since", since]
        return self.run_cli(*args, *extra)


class ReplyGuards(Base):
    def test_dry_run_makes_no_post_and_shows_recipient_inbox_words(self):
        code, out, _ = self.reply()
        self.assertEqual(code, 0)
        self.assertEqual(self.http.posts(), [])
        for needle in ("lead@example.com", "sender@example.org", "Happy to help"):
            self.assertIn(needle, out)
        self.assertEqual(self.comms.rows, [])

    def test_yes_without_since_exits_2_and_posts_nothing(self):
        code, out, err = self.reply("--yes", since=None)
        self.assertEqual(code, 2)
        self.assertEqual(self.http.posts(), [])

    def test_newer_message_exits_5(self):
        self.http.thread = [sent(), received(), sent(id="e-late", timestamp_email=T2)]
        code, out, _ = self.reply("--yes")
        self.assertEqual(code, 5)
        self.assertEqual(self.http.posts(), [])

    def test_unparseable_thread_time_is_refused_not_ignored(self):
        self.http.thread = [sent(), received(), sent(id="e-x", timestamp_email=None)]
        code, _, _ = self.reply("--yes")
        self.assertEqual(code, 5)
        self.assertEqual(self.http.posts(), [])

    def test_reply_to_a_sent_email_is_refused(self):
        self.http.emails = {"e-sent": sent()}
        out_code, out, _ = self.run_cli("reply", "e-sent", "hello", "--workspace", "acme",
                                        "--since", SINCE_OK, "--yes")
        self.assertEqual(out_code, 1)
        self.assertEqual(self.http.posts(), [])

    def test_unknown_email_id_is_refused(self):
        code, _, _ = self.run_cli("reply", "nope", "hello", "--workspace", "acme",
                                  "--since", SINCE_OK, "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.http.posts(), [])

    def test_money_lock_exits_6_without_money_ok(self):
        self.money = ["$500"]
        code, out, _ = self.reply("--yes", text="It is $500 a month")
        self.assertEqual(code, 6)
        self.assertEqual(self.http.posts(), [])

    def test_money_lock_passes_with_money_ok(self):
        self.money = ["$500"]
        code, _, _ = self.reply("--yes", "--money-ok", text="It is $500 a month")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.http.posts()), 1)

    def test_money_check_that_cannot_run_locks(self):
        with mock.patch.object(I, "_money_hits", lambda text: None):
            code, _, _ = self.reply("--yes")
        self.assertEqual(code, 6)
        self.assertEqual(self.http.posts(), [])

    def test_gate_refusal_sends_nothing(self):
        self.gate.allow = False
        code, out, _ = self.reply("--yes")
        self.assertEqual(code, 7)
        self.assertEqual(self.http.posts(), [])
        self.assertEqual(self.comms.rows, [])

    def test_gate_that_cannot_load_in_enforce_mode_sends_nothing(self):
        with mock.patch.object(I, "_comms_module", lambda name: None), \
                mock.patch.object(I, "_gate_mode_fallback", lambda: "enforce"):
            code, _, _ = self.reply("--yes")
        self.assertEqual(code, 7)
        self.assertEqual(self.http.posts(), [])


class ReplySend(Base):
    def test_success_posts_once_and_archives_one_row(self):
        code, out, _ = self.reply("--yes")
        self.assertEqual(code, 0)
        posts = self.http.posts()
        self.assertEqual(len(posts), 1)
        _, path, _, body, _ = posts[0]
        self.assertEqual(path, "/emails/reply")
        self.assertEqual(body["reply_to_uuid"], "e-recv")
        self.assertEqual(body["eaccount"], "sender@example.org")
        self.assertEqual(body["subject"], "Re: Quick question")
        self.assertIn("Happy to help", body["body"]["text"])
        self.assertEqual(len(self.comms.rows), 1)
        args, kw = self.comms.rows[0]
        self.assertEqual(args[:3], ("instantly", "send", "lead@example.com"))
        self.assertIs(kw["hook_covers"], False)
        self.assertEqual(kw["verb"], "reply")
        self.assertEqual(kw["message_id"], "new-1")
        self.assertEqual(self.gate.finished[-1][1], "sent")

    def test_subject_not_double_prefixed(self):
        self.http.emails["e-recv"] = received(subject="Re: Quick question")
        self.reply("--yes")
        self.assertEqual(self.http.posts()[0][3]["subject"], "Re: Quick question")

    def test_html_body_escapes_markup(self):
        self.reply("--yes", text="a <b>bold</b> & more")
        html = self.http.posts()[0][3]["body"]["html"]
        self.assertNotIn("<b>", html)
        self.assertIn("&amp;", html)

    def test_http_500_fails_and_archives_nothing(self):
        self.http.post = (500, {"message": "boom"})
        code, out, err = self.reply("--yes")
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.http.posts()), 1)   # never retried
        self.assertEqual(self.comms.rows, [])
        self.assertNotEqual(self.gate.finished[-1][1], "sent")

    def test_http_400_fails_before_send(self):
        self.http.post = (400, {"message": "bad"})
        code, _, _ = self.reply("--yes")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.finished[-1][1], "failed_before_send")

    def test_timeout_is_uncertain_and_never_retried(self):
        self.http.post_exc = I.UncertainError("timed out")
        code, out, err = self.reply("--yes")
        self.assertEqual(code, I.UNCERTAIN_EXIT)
        self.assertIn("UNCERTAIN", out + err)
        self.assertIn("do not resend", (out + err).lower())
        self.assertEqual(len(self.http.posts()), 1)
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.finished[-1][1], "uncertain")


class KeyHandling(Base):
    def test_key_never_in_output_on_any_path(self):
        runs = [
            self.reply(), self.reply("--yes"),
            self.run_cli("unread", "--workspace", "acme"),
            self.run_cli("find", "x@example.com", "--workspace", "acme"),
            self.run_cli("thread", "t-1", "--workspace", "acme"),
        ]
        self.http.post = (500, {"message": "boom " + SECRET})
        runs.append(self.reply("--yes"))
        for code, out, err in runs:
            self.assertNotIn(SECRET, out + err)

    def test_key_not_in_error_from_a_failed_read(self):
        self.http.emails = {}
        code, out, err = self.run_cli("thread", "t-1", "--workspace", "acme")
        self.assertNotIn(SECRET, out + err)

    def test_failed_key_lookup_exits_2_with_one_line_no_key(self):
        script = "import sys; sys.stderr.write('%s'); sys.exit(3)" % SECRET
        with mock.patch.object(I, "load_workspaces", lambda: {"acme": [sys.executable, "-c", script]}), \
                mock.patch.object(I, "_run_key_cmd", I._real_run_key_cmd):
            code, out, err = self.run_cli("unread", "--workspace", "acme")
        self.assertEqual(code, 2)
        self.assertNotIn(SECRET, out + err)
        self.assertEqual(len((out + err).strip().splitlines()), 1)
        self.assertEqual(self.http.calls, [])

    def test_missing_command_binary_exits_2(self):
        with mock.patch.object(I, "load_workspaces", lambda: {"acme": ["/nonexistent/zzz"]}), \
                mock.patch.object(I, "_run_key_cmd", I._real_run_key_cmd):
            code, out, err = self.run_cli("unread", "--workspace", "acme")
        self.assertEqual(code, 2)

    def test_unknown_workspace_exits_2(self):
        code, _, err = self.run_cli("unread", "--workspace", "zzz")
        self.assertEqual(code, 2)

    def test_env_key_used_when_workspace_omitted(self):
        with mock.patch.dict(os.environ, {"INSTANTLY_API_KEY": SECRET}):
            code, out, err = self.run_cli("unread")
        self.assertEqual(code, 0)
        self.assertEqual(self.http.calls[0][4], SECRET)
        self.assertNotIn(SECRET, out + err)

    def test_no_workspace_and_no_env_exits_2(self):
        code, _, _ = self.run_cli("unread")
        self.assertEqual(code, 2)

    def test_key_command_runs_without_shell(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(kw, cmd=cmd)
            return mock.Mock(returncode=0, stdout=SECRET + "\n", stderr="")
        with mock.patch("subprocess.run", fake_run):
            self.assertEqual(I._real_run_key_cmd(["your-secret-tool", "X"]), SECRET)
        self.assertFalse(seen.get("shell", False))
        self.assertIsInstance(seen["cmd"], list)


class Reads(Base):
    def test_unread_asks_for_unread_received_and_prints_ids(self):
        code, out, _ = self.run_cli("unread", "--workspace", "acme", "-n", "3")
        self.assertEqual(code, 0)
        call = self.http.calls[0]
        self.assertEqual(call[0], "GET")
        self.assertEqual(call[2]["is_unread"], "true")
        self.assertEqual(call[2]["email_type"], "received")
        self.assertLessEqual(int(call[2]["limit"]), 100)
        self.assertIn("e-recv", out)
        self.assertIn("t-1", out)

    def test_thread_is_oldest_first(self):
        self.http.thread = [received(), sent()]
        code, out, _ = self.run_cli("thread", "t-1", "--workspace", "acme")
        self.assertEqual(code, 0)
        self.assertLess(out.index("e-sent"), out.index("e-recv"))
        self.assertEqual(self.http.calls[0][2]["search"], "thread:t-1")

    def test_find_by_text_filters_locally(self):
        self.http.emails = {"e-recv": received(), "e-two": received(id="e-two", body={"text": "no thanks"},
                                                                 subject="other", content_preview="no")}
        code, out, _ = self.run_cli("find", "tell me more", "--workspace", "acme")
        self.assertEqual(code, 0)
        self.assertIn("e-recv", out)
        self.assertNotIn("e-two", out)

    def test_mark_read_dry_run_posts_nothing(self):
        code, out, _ = self.run_cli("mark-read", "t-1", "--workspace", "acme")
        self.assertEqual(code, 0)
        self.assertEqual(self.http.posts(), [])

    def test_mark_read_yes_posts_once(self):
        self.http.post = (I.HTTP_OK, {"success": True})
        code, _, _ = self.run_cli("mark-read", "t-1", "--workspace", "acme", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.http.posts()[0][1], "/emails/threads/t-1/mark-as-read")

    def test_mark_read_failure_exits_nonzero(self):
        self.http.post = (500, {})
        code, _, _ = self.run_cli("mark-read", "t-1", "--workspace", "acme", "--yes")
        self.assertNotEqual(code, 0)

    def test_workspaces_lists_names_only(self):
        code, out, _ = self.run_cli("workspaces")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "acme")
        self.assertEqual(self.http.calls, [])


class RateLimit(unittest.TestCase):
    """Instantly answers 429 when reads come fast, so a few reads in a row can hit it."""

    def go(self, statuses):
        seq, sleeps, err = iter(statuses), [], io.StringIO()
        with mock.patch.object(I, "http_request", side_effect=lambda *a, **k: (next(seq), {})), \
                mock.patch.object(I.time, "sleep", sleeps.append), \
                contextlib.redirect_stderr(err):
            try:
                return I._get("k", "/x")[0], sleeps, err.getvalue()
            except SystemExit as stop:
                return stop.code, sleeps, err.getvalue()

    def test_a_read_waits_longer_each_time_then_succeeds(self):
        status, sleeps, _ = self.go([429, 429, 200])
        self.assertEqual(status, 200)
        self.assertEqual(sleeps, [15, 30])

    def test_it_gives_up_with_a_line_that_says_why(self):
        status, sleeps, err = self.go([429, 429, 429, 200])
        self.assertEqual(status, 1)
        self.assertEqual(sleeps, [15, 30])
        self.assertIn("rate limit", err)


class RealHttp(unittest.TestCase):
    def test_timeout_raises_uncertain_and_hides_key(self):
        import socket
        with mock.patch("urllib.request.urlopen", side_effect=socket.timeout("t")):
            with self.assertRaises(I.UncertainError) as cm:
                I._real_http_request("POST", "/emails/reply", SECRET, body={"a": 1})
        self.assertNotIn(SECRET, str(cm.exception))

    def test_connection_error_on_get_is_a_plain_error(self):
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertRaises(I.ApiError) as cm:
                I._real_http_request("GET", "/emails", SECRET)
        self.assertNotIn(SECRET, str(cm.exception))

    def test_http_error_returns_status(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 500, "x", {}, io.BytesIO(b'{"message":"boom"}'))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            status, body = I._real_http_request("POST", "/emails/reply", SECRET, body={})
        self.assertEqual(status, 500)


if __name__ == "__main__":
    unittest.main()
