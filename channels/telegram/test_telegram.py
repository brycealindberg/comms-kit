#!/usr/bin/env python3
"""Tests for telegram.py with a fake client. No Telethon, no network, no account.

Failure modes covered (written before the code):
  1. a send or mark-read runs without --yes (dry run must call no write method)
  2. --yes without --since still sends
  3. a newer message from the other person still lets the send through
  4. a newer message that is a reply to an old one is not counted as newer
  5. a message with no readable time is ignored instead of refused
  6. the money lock is skipped, or a money check that cannot run lets the text through
  7. a refusing outbound gate still lets the send through
  8. an unknown send result (timeout, drop) is retried, exits 0, or archives a row
  9. a clean refusal from Telegram archives a row or exits 0
 10. a good send archives zero or two rows, or hook_covers is not False
 11. a missing Telethon prints a traceback or a wrong exit code
 12. `login` or `status` reaches the network, or needs Telethon
 13. a secret leaks into output, or a key command runs through a shell
 14. an API id that is not a number crashes with a traceback
 15. a real archive gets a second row from one send (checked with the real core)
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import telegram as T  # noqa: E402

SECRET = "-".join(["tok", "FAKE", "HASH", "not", "real"])
T_OLD = 1_790_000_000.0      # the message the owner read
T_NEW = T_OLD + 60.0
CHAT_ID = "1000200030"          # what Telegram calls the chat
CHAT = "id:" + CHAT_ID          # how the owner types it (bare digits are refused)


def msg(mid, ts, out=False, text="hello", reply_to=None, sender="Alex Doe"):
    return {"id": mid, "chat": CHAT_ID, "chat_title": "Alex Doe", "sender": sender, "out": out,
            "ts": ts, "text": text, "reply_to": reply_to}


class FakeClient:
    """Stands in for the Telethon wrapper. Records every call."""

    def __init__(self, history=None, dialogs=None, send_exc=None, send_result=None):
        self.calls = []
        self.history = history if history is not None else [msg(7, T_OLD)]   # newest first
        self.dialog_rows = dialogs if dialogs is not None else []
        self.send_exc = send_exc
        self.send_result = send_result or {"id": 8, "ts": T_NEW}
        self.mark_exc = None
        self.unknown = set()          # chats that cannot be resolved (a mistyped name)

    def resolve(self, chat):
        self.calls.append(("resolve", chat))
        if chat in self.unknown:
            raise T.ApiError("Telegram has no chat called %s" % chat)
        return {"id": CHAT_ID, "title": "Alex Doe"}

    def dialogs(self, limit):
        self.calls.append(("dialogs", limit))
        return list(self.dialog_rows)[:limit]

    def messages(self, chat, limit, search=None):
        self.calls.append(("messages", chat, limit, search))
        rows = list(self.history)
        if search:
            rows = [r for r in rows if search.lower() in (r["text"] or "").lower()]
        return rows[:limit]

    def send(self, chat, text, reply_to=None):
        self.calls.append(("send", chat, text, reply_to))
        if self.send_exc:
            raise self.send_exc
        return self.send_result

    def mark_read(self, chat):
        self.calls.append(("mark_read", chat))
        if self.mark_exc:
            raise self.mark_exc

    def close(self):
        self.calls.append(("close",))

    def writes(self):
        return [c for c in self.calls if c[0] in ("send", "mark_read")]


class FakeGate:
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
        self.client = FakeClient()
        self.gate = FakeGate()
        self.comms = FakeComms()
        self.money = []
        mods = {"outbound_gate": self.gate, "commslib": self.comms}
        patches = [
            mock.patch.object(T, "make_client", lambda: self.client),
            mock.patch.object(T, "_comms_module", lambda name: mods.get(name)),
            mock.patch.object(T, "_money_hits", lambda text: list(self.money)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                T.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    def send(self, *extra, text="Happy to help, here is more.", since=str(T_OLD)):
        args = ["send", CHAT, text]
        if since:
            args += ["--since", since]
        return self.run_cli(*args, *extra)

    def reply(self, *extra, text="Happy to help, here is more.", since=str(T_OLD)):
        args = ["reply", CHAT, "7", text]
        if since:
            args += ["--since", since]
        return self.run_cli(*args, *extra)


class SendGuards(Base):
    def test_dry_run_calls_no_write_and_archives_nothing(self):
        for fn in (self.send, self.reply):
            code, out, _ = fn()
            self.assertEqual(code, 0)
            self.assertIn("DRY RUN", out)
            self.assertIn("Happy to help", out)
        self.assertEqual(self.client.writes(), [])
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.begun, 0)

    def test_yes_without_since_exits_2_and_sends_nothing(self):
        for fn in (self.send, self.reply):
            code, out, err = fn("--yes", since=None)
            self.assertEqual(code, 2)
            self.assertIn("--since", out + err)
        self.assertEqual(self.client.writes(), [])

    def test_newer_message_from_the_other_person_exits_5(self):
        self.client.history = [msg(9, T_NEW, out=False, text="wait, one more thing"), msg(7, T_OLD)]
        for fn in (self.send, self.reply):
            code, out, _ = fn("--yes")
            self.assertEqual(code, 5)
            self.assertIn("NOT SENT:", out)
        self.assertEqual(self.client.writes(), [])
        self.assertEqual(self.comms.rows, [])

    def test_newer_message_that_is_a_reply_to_an_old_one_still_counts(self):
        self.client.history = [msg(9, T_NEW, reply_to=3), msg(7, T_OLD)]
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 5)
        self.assertEqual(self.client.writes(), [])

    def test_message_at_exactly_since_is_not_newer(self):
        code, _, _ = self.send("--yes")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.client.writes()), 1)

    def test_message_with_no_time_is_refused_not_ignored(self):
        self.client.history = [msg(9, None), msg(7, T_OLD)]
        code, _, _ = self.send("--yes")
        self.assertEqual(code, 5)
        self.assertEqual(self.client.writes(), [])

    def test_since_accepts_iso_and_refuses_junk(self):
        code, _, _ = self.send("--yes", since="2020-10-30T10:00:00Z")
        self.assertEqual(code, 5)   # that is long before the fake history
        code, out, err = self.send("--yes", since="yesterday")
        self.assertEqual(code, 2)
        self.assertEqual(self.client.writes(), [])

    def test_empty_text_is_refused(self):
        code, _, _ = self.send("--yes", text="   ")
        self.assertEqual(code, 1)
        self.assertEqual(self.client.writes(), [])

    def test_money_lock_exits_6_without_money_ok(self):
        self.money = ["$500"]
        code, out, _ = self.send("--yes", text="It is $500 a month")
        self.assertEqual(code, 6)
        self.assertIn("MONEY LOCK", out)
        self.assertEqual(self.client.writes(), [])

    def test_dry_run_shows_the_money_lock(self):
        self.money = ["$500"]
        code, out, _ = self.send(text="It is $500 a month")
        self.assertEqual(code, 0)
        self.assertIn("MONEY LOCK", out)
        self.assertEqual(self.client.writes(), [])

    def test_money_ok_lets_it_through(self):
        self.money = ["$500"]
        code, _, _ = self.send("--yes", "--money-ok", text="It is $500 a month")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.client.writes()), 1)

    def test_money_check_that_cannot_run_locks(self):
        with mock.patch.object(T, "_money_hits", lambda text: None):
            code, _, _ = self.send("--yes")
        self.assertEqual(code, 6)
        self.assertEqual(self.client.writes(), [])

    def test_gate_refusal_sends_nothing(self):
        self.gate.allow = False
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 7)
        self.assertEqual(self.client.writes(), [])
        self.assertEqual(self.comms.rows, [])

    def test_gate_that_cannot_load_in_enforce_mode_sends_nothing(self):
        with mock.patch.object(T, "_comms_module", lambda name: None), \
                mock.patch.object(T, "_gate_mode_fallback", lambda: "enforce"):
            code, _, _ = self.send("--yes")
        self.assertEqual(code, 7)
        self.assertEqual(self.client.writes(), [])


class SendResults(Base):
    def test_send_writes_exactly_one_archive_row(self):
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.client.writes(), [("send", CHAT, "Happy to help, here is more.", None)])
        self.assertEqual(len(self.comms.rows), 1)
        args, kw = self.comms.rows[0]
        self.assertEqual(args[:3], ("telegram", "send", CHAT))
        self.assertIs(kw["hook_covers"], False)
        self.assertEqual(kw["verb"], "send")
        self.assertEqual(kw["message_id"], "8")
        self.assertEqual(self.gate.finished[-1][1], "sent")

    def test_reply_passes_the_message_id_and_archives_one_row(self):
        code, _, _ = self.reply("--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.client.writes()[0][3], 7)
        self.assertEqual(len(self.comms.rows), 1)
        self.assertEqual(self.comms.rows[0][1]["verb"], "reply")

    def test_reply_id_must_be_a_number(self):
        code, _, _ = self.run_cli("reply", CHAT, "abc", "hi", "--since", str(T_OLD), "--yes")
        self.assertEqual(code, 2)
        self.assertEqual(self.client.writes(), [])

    def test_unknown_result_is_uncertain_never_retried_never_archived(self):
        self.client.send_exc = T.UncertainError("timed out")
        code, out, err = self.send("--yes")
        self.assertEqual(code, T.UNCERTAIN_EXIT)
        self.assertIn("UNCERTAIN", out + err)
        self.assertIn("do not resend", (out + err).lower())
        self.assertEqual(len(self.client.writes()), 1)
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.finished[-1][1], "uncertain")

    def test_clean_refusal_from_telegram_is_a_failure_and_archives_nothing(self):
        self.client.send_exc = T.ApiError("Telegram refused: the chat was not found")
        code, out, err = self.send("--yes")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.client.writes()), 1)
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.finished[-1][1], "failed_before_send")
        self.assertIn("NOT SENT", out + err)

    def test_surprise_exception_in_send_counts_as_uncertain(self):
        self.client.send_exc = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.send("--yes")
        self.assertEqual(self.gate.finished[-1][1], "uncertain")
        self.assertEqual(self.comms.rows, [])


class ReadVerbs(Base):
    def test_chats_lists_ids_titles_unread(self):
        self.client.dialog_rows = [{"id": CHAT_ID, "title": "Alex Doe", "unread": 2, "ts": T_OLD}]
        code, out, _ = self.run_cli("chats")
        self.assertEqual(code, 0)
        for needle in (CHAT, "Alex Doe", "2"):
            self.assertIn(needle, out)

    def test_unread_shows_only_incoming_oldest_first(self):
        self.client.dialog_rows = [{"id": CHAT_ID, "title": "Alex Doe", "unread": 2, "ts": T_NEW},
                                   {"id": "5", "title": "Quiet", "unread": 0, "ts": T_OLD}]
        self.client.history = [msg(9, T_NEW, text="second"), msg(8, T_NEW - 5, out=True, text="mine"),
                               msg(7, T_OLD, text="first")]
        code, out, _ = self.run_cli("unread")
        self.assertEqual(code, 0)
        self.assertLess(out.index("first"), out.index("second"))
        self.assertNotIn("mine", out)
        self.assertNotIn("Quiet", out)

    def test_unread_with_nothing_says_so(self):
        code, out, _ = self.run_cli("unread")
        self.assertEqual(code, 0)
        self.assertIn("No unread", out)

    def test_history_is_oldest_first_and_clamps_n(self):
        self.client.history = [msg(9, T_NEW, text="newer"), msg(7, T_OLD, text="older")]
        code, out, _ = self.run_cli("history", CHAT, "-n", "5000")
        self.assertEqual(code, 0)
        self.assertLess(out.index("older"), out.index("newer"))
        self.assertLessEqual(self.client.calls[0][2], 200)

    def test_find_searches_one_chat_or_all(self):
        self.client.history = [msg(9, T_NEW, text="the invoice is late"), msg(7, T_OLD, text="hi")]
        code, out, _ = self.run_cli("find", "invoice", "--chat", CHAT)
        self.assertEqual(code, 0)
        self.assertIn("invoice", out)
        self.assertNotIn("id=7", out)
        self.assertEqual(self.client.calls[0][1], CHAT)
        self.run_cli("find", "invoice")
        last = [c for c in self.client.calls if c[0] == "messages"][-1]
        self.assertIsNone(last[1])

    def test_a_failed_read_exits_1_with_one_line(self):
        def boom(*a, **k):
            raise T.ApiError("could not reach Telegram")
        self.client.messages = boom
        code, out, err = self.run_cli("history", CHAT)
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", out + err)


class MarkRead(Base):
    def test_dry_run_calls_nothing(self):
        code, out, _ = self.run_cli("mark-read", CHAT)
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertEqual(self.client.writes(), [])

    def test_yes_marks_once(self):
        code, _, _ = self.run_cli("mark-read", CHAT, "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.client.writes(), [("mark_read", CHAT)])
        self.assertEqual(self.comms.rows, [])      # not a message: nothing to archive

    def test_unknown_result_is_uncertain_exit_4(self):
        self.client.mark_exc = T.UncertainError("drop")
        code, out, err = self.run_cli("mark-read", CHAT, "--yes")
        self.assertEqual(code, T.UNCERTAIN_EXIT)
        self.assertIn("UNCERTAIN", out + err)


class NoTelethon(unittest.TestCase):
    def test_missing_telethon_prints_install_line_and_exits_2(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules, {"telethon": None}), \
                mock.patch.object(T, "_read_credentials", lambda: (1, "h", "/tmp/x")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                T.main(["chats"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("pip install telethon", out.getvalue() + err.getvalue())
        self.assertNotIn("Traceback", out.getvalue() + err.getvalue())

    def test_real_process_without_telethon_has_no_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "telethon.py"), "w") as fh:
                fh.write("raise ImportError('blocked for this test')\n")
            conf = os.path.join(tmp, "config.json")
            with open(conf, "w") as fh:
                json.dump({"telegram": {"api_id_command": [sys.executable, "-c", "print(12345)"],
                                        "api_hash_command": [sys.executable, "-c", "print('abc')"],
                                        "session_path": os.path.join(tmp, "s")}}, fh)
            env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp, "PYTHONPATH": tmp,
                   "COMMS_KIT_CONFIG": conf, "PYTHONDONTWRITEBYTECODE": "1"}
            if os.environ.get("COMMS_KIT_CORE"):
                env["COMMS_KIT_CORE"] = os.environ["COMMS_KIT_CORE"]
            proc = subprocess.run([sys.executable, os.path.join(HERE, "telegram.py"), "chats"],
                                  capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        self.assertIn("pip install telethon", proc.stdout + proc.stderr)


class LoginAndStatus(Base):
    def test_login_only_prints_steps_and_makes_no_client(self):
        with mock.patch.object(T, "make_client", side_effect=AssertionError("no client")):
            code, out, _ = self.run_cli("login")
        self.assertEqual(code, 0)
        for needle in ("my.telegram.org", "api_id", "api_hash", "code", "pip install telethon"):
            self.assertIn(needle, out)
        self.assertEqual(self.client.calls, [])

    def test_status_needs_no_telethon_and_no_client(self):
        with mock.patch.dict(sys.modules, {"telethon": None}), \
                mock.patch.object(T, "make_client", side_effect=AssertionError("no client")), \
                mock.patch.object(T, "_conf", lambda key, default=None: default):
            code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("telethon", out.lower())
        self.assertIn("missing", out.lower())


class Credentials(unittest.TestCase):
    def setUp(self):
        self.conf = {"telegram.api_id_command": ["id-cmd"], "telegram.api_hash_command": ["hash-cmd"],
                     "telegram.session_path": "/tmp/session-x"}
        self.outputs = {"id-cmd": "12345", "hash-cmd": SECRET}

        def fake_run_key(cmd):
            return self.outputs[cmd[0]]
        for p in (mock.patch.object(T, "_conf", lambda key, default=None: self.conf.get(key, default)),
                  mock.patch.object(T, "_run_key_cmd", fake_run_key)):
            p.start()
            self.addCleanup(p.stop)

    def test_reads_all_three_keys(self):
        self.assertEqual(T._read_credentials(), (12345, SECRET, "/tmp/session-x"))

    def test_api_id_that_is_not_a_number_exits_2_one_line(self):
        self.outputs["id-cmd"] = "not-a-number"
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            T._read_credentials()
        self.assertEqual(cm.exception.code, 2)
        self.assertNotIn("Traceback", err.getvalue())

    def test_missing_key_exits_2_and_names_the_key(self):
        del self.conf["telegram.session_path"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            T._read_credentials()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("telegram.session_path", err.getvalue())

    def test_failed_key_command_hides_the_secret(self):
        script = "import sys; sys.stderr.write('%s'); sys.exit(3)" % SECRET
        err = io.StringIO()
        self.conf["telegram.api_hash_command"] = [sys.executable, "-c", script]
        self.outputs = None
        with mock.patch.object(T, "_run_key_cmd", T._real_run_key_cmd), \
                mock.patch.dict(self.conf, {"telegram.api_id_command": [sys.executable, "-c", "print(1)"]}), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            T._read_credentials()
        self.assertEqual(cm.exception.code, 2)
        self.assertNotIn(SECRET, err.getvalue())

    def test_key_command_runs_without_shell(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(kw, cmd=cmd)
            return mock.Mock(returncode=0, stdout=SECRET + "\n", stderr="")
        with mock.patch("subprocess.run", fake_run):
            self.assertEqual(T._real_run_key_cmd(["your-secret-tool", "X"]), SECRET)
        self.assertFalse(seen.get("shell", False))
        self.assertIsInstance(seen["cmd"], list)

    def test_secret_never_printed_by_a_send(self):
        # the credentials reach only the client factory; no verb prints them
        client = FakeClient()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(T, "make_client", lambda: client), \
                mock.patch.object(T, "_comms_module", lambda name: None), \
                mock.patch.object(T, "_gate_mode_fallback", lambda: "report"), \
                mock.patch.object(T, "_money_hits", lambda text: []), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            T.main(["send", CHAT, "hello there", "--since", str(T_OLD), "--yes"])
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())


class MessageMapping(unittest.TestCase):
    """The pure part of the Telethon wrapper: turn a library message into our dict."""

    def test_maps_time_direction_and_reply(self):
        import datetime as dt
        m = types.SimpleNamespace(
            id=5, date=dt.datetime(2026, 10, 30, 10, 0, tzinfo=dt.timezone.utc), out=True,
            message="hi there", reply_to=types.SimpleNamespace(reply_to_msg_id=3),
            sender=types.SimpleNamespace(first_name="Alex", last_name="Doe", username=None, title=None),
            chat_id=77)
        row = T.message_to_dict(m, "Alex Doe")
        self.assertEqual(row["id"], 5)
        self.assertEqual(row["ts"], dt.datetime(2026, 10, 30, 10, 0, tzinfo=dt.timezone.utc).timestamp())
        self.assertTrue(row["out"])
        self.assertEqual(row["reply_to"], 3)
        self.assertEqual(row["sender"], "Alex Doe")
        self.assertEqual(row["chat"], "77")

    def test_naive_date_is_read_as_utc_and_missing_date_is_none(self):
        import datetime as dt
        base = dict(id=1, out=False, message="x", reply_to=None, sender=None, chat_id=1)
        naive = T.message_to_dict(types.SimpleNamespace(date=dt.datetime(2026, 10, 30, 10, 0), **base), "c")
        self.assertEqual(naive["ts"], dt.datetime(2026, 10, 30, 10, 0, tzinfo=dt.timezone.utc).timestamp())
        self.assertIsNone(T.message_to_dict(types.SimpleNamespace(date=None, **base), "c")["ts"])


class ChatNames(Base):
    """A chat name that reaches a person must never be guessed."""

    def test_accepted_forms(self):
        self.assertEqual(T.parse_chat("+1 206 555 0100"), "+12065550100")
        self.assertEqual(T.parse_chat("+12065550100"), "+12065550100")
        self.assertEqual(T.parse_chat("id:12345"), "id:12345")
        self.assertEqual(T.parse_chat("-100777"), "-100777")
        self.assertEqual(T.parse_chat("@alexdoe"), "@alexdoe")
        self.assertEqual(T.parse_chat("me"), "me")

    def test_telethon_gets_an_int_for_ids_and_text_for_the_rest(self):
        self.assertEqual(T.chat_ref("id:12345"), 12345)
        self.assertEqual(T.chat_ref("-100777"), -100777)
        self.assertEqual(T.chat_ref("+12065550100"), "+12065550100")
        self.assertEqual(T.chat_ref("@alexdoe"), "@alexdoe")

    def test_bare_digits_and_junk_are_refused_with_both_forms_shown(self):
        for bad in ("12065550100", "12345", "+12", "+abc", "id:", "id:abc", "alex doe", "@a", "", "--x"):
            with self.assertRaises(ValueError) as cm:
                T.parse_chat(bad)
            text = str(cm.exception)
            self.assertNotIn("\n", text)
            self.assertIn("+12065550100", text)
            self.assertIn("id:12345", text)

    def test_every_chat_argument_is_checked_before_any_client_exists(self):
        made = []
        with mock.patch.object(T, "make_client", lambda: made.append(1) or self.client):
            for argv in (["send", "12065550100", "hi"], ["reply", "123", "7", "hi"], ["history", "123"],
                         ["mark-read", "123", "--yes"], ["find", "x", "--chat", "123"]):
                code, out, err = self.run_cli(*argv)
                self.assertEqual(code, 2, argv)
                self.assertIn("id:12345", out + err)
        self.assertEqual(made, [])
        self.assertEqual(self.client.calls, [])

    def test_a_phone_with_spaces_is_normalised_before_it_reaches_the_client(self):
        code, _, _ = self.run_cli("history", "+1 206 555 0100")
        self.assertEqual(code, 0)
        self.assertEqual(self.client.calls[0][1], "+12065550100")

    def test_chats_prints_ids_that_send_accepts(self):
        self.client.dialog_rows = [{"id": "555", "title": "A user", "unread": 0, "ts": T_OLD},
                                   {"id": "-100999", "title": "A channel", "unread": 0, "ts": T_OLD},
                                   {"id": "-777", "title": "A group", "unread": 0, "ts": T_OLD}]
        code, out, _ = self.run_cli("chats")
        shown = re.findall(r"chat=(\S+)", out)
        self.assertEqual(shown, ["id:555", "-100999", "-777"])
        for ref in shown:
            self.assertEqual(T.parse_chat(ref), ref)

    def test_message_lines_print_the_same_form(self):
        code, out, _ = self.run_cli("history", CHAT)
        self.assertIn("chat=" + CHAT, out)


class UnknownChat(Base):
    """A mistyped name fails before anything leaves: not uncertain, nothing for the gate or archive."""

    def test_unknown_chat_on_send_yes_is_not_sent_and_leaves_no_trace(self):
        self.client.unknown = {"@nobody_here"}
        for fn in ("send", "reply"):
            args = [fn, "@nobody_here"] + (["7"] if fn == "reply" else []) + ["hi there", "--since", str(T_OLD), "--yes"]
            code, out, err = self.run_cli(*args)
            self.assertEqual(code, 1, fn)
            self.assertIn("NOT SENT", out + err)
            self.assertNotIn("UNCERTAIN", out + err)
        self.assertEqual(self.client.writes(), [])
        self.assertEqual(self.gate.begun, 0)
        self.assertEqual(self.gate.finished, [])
        self.assertEqual(self.comms.rows, [])

    def test_unknown_chat_on_a_dry_run_also_fails(self):
        self.client.unknown = {"@nobody_here"}
        code, out, err = self.run_cli("send", "@nobody_here", "hi there")
        self.assertEqual(code, 1)
        self.assertNotIn("DRY RUN", out)

    def test_unknown_chat_on_mark_read_is_a_plain_failure_not_uncertain(self):
        self.client.unknown = {"@nobody_here"}
        code, out, err = self.run_cli("mark-read", "@nobody_here", "--yes")
        self.assertEqual(code, 1)
        self.assertNotIn("UNCERTAIN", out + err)
        self.assertEqual(self.client.writes(), [])

    def test_the_chat_is_resolved_before_the_send_is_attempted(self):
        self.send("--yes")
        names = [c[0] for c in self.client.calls]
        self.assertLess(names.index("resolve"), names.index("send"))


def fake_telethon(entity_exc=None, send_exc=None):
    """A stand-in for the telethon module, just enough for TelethonClient. Records every call."""
    calls = []

    class RPCError(Exception):
        pass

    class FloodWaitError(RPCError):
        seconds = 30

    class TelegramClient:
        def __init__(self, session, api_id, api_hash, loop=None):
            pass

        async def connect(self):
            calls.append("connect")

        async def is_user_authorized(self):
            return True

        async def get_entity(self, ref):
            calls.append(("get_entity", ref))
            if entity_exc:
                raise entity_exc
            return types.SimpleNamespace(id=42, first_name="Alex", last_name="Doe", ref=ref)

        async def send_message(self, entity, text, reply_to=None):
            calls.append(("send_message", getattr(entity, "ref", entity), text, reply_to))
            if send_exc:
                raise send_exc
            return types.SimpleNamespace(id=5, date=None)

        async def send_read_acknowledge(self, entity):
            calls.append(("ack", getattr(entity, "ref", entity)))

        async def disconnect(self):
            pass

    mod = types.SimpleNamespace(TelegramClient=TelegramClient,
                                errors=types.SimpleNamespace(RPCError=RPCError, FloodWaitError=FloodWaitError))
    return mod, calls


class TelethonWrapper(unittest.TestCase):
    """The real wrapper class against a fake telethon module (the library itself is not needed)."""

    def make(self, **kw):
        mod, calls = fake_telethon(**kw)
        client = T.TelethonClient(mod, 1, "h", "/tmp/never-used")
        self.addCleanup(client.close)
        return client, mod, calls

    def test_an_unknown_chat_is_a_clean_error_and_send_message_is_never_called(self):
        client, mod, calls = self.make(entity_exc=ValueError("Cannot find any entity corresponding to x"))
        with self.assertRaises(T.ApiError):
            client.send("@nobody_here", "hi")
        self.assertFalse([c for c in calls if isinstance(c, tuple) and c[0] == "send_message"])

    def test_resolve_then_send_uses_the_resolved_entity_once(self):
        client, mod, calls = self.make()
        self.assertEqual(client.resolve("id:42")["title"], "Alex Doe")
        self.assertEqual(client.send("id:42", "hello", reply_to=3)["id"], 5)
        self.assertEqual([c for c in calls if isinstance(c, tuple) and c[0] == "get_entity"], [("get_entity", 42)])
        self.assertEqual([c for c in calls if isinstance(c, tuple) and c[0] == "send_message"],
                         [("send_message", 42, "hello", 3)])

    def test_a_failure_after_the_request_can_have_left_stays_uncertain(self):
        for exc in (ConnectionError("reset"), OSError("drop"), ValueError("odd")):
            client, mod, calls = self.make(send_exc=exc)
            with self.assertRaises(T.UncertainError):
                client.send("id:42", "hi")

    def test_a_telegram_refusal_is_a_clean_failure(self):
        mod, calls = fake_telethon()
        client = T.TelethonClient(mod, 1, "h", "/tmp/never-used")
        self.addCleanup(client.close)
        # raise the module's own RPCError subclass from send_message
        exc = mod.errors.FloodWaitError("wait")
        client._client.send_message = _raiser(exc)
        with self.assertRaises(T.ApiError) as cm:
            client.send("id:42", "hi")
        self.assertIn("wait 30", str(cm.exception))

    def test_mark_read_resolves_first_too(self):
        client, mod, calls = self.make(entity_exc=ValueError("Cannot find any entity"))
        with self.assertRaises(T.ApiError):
            client.mark_read("@nobody_here")


def _raiser(exc):
    async def go(*a, **k):
        raise exc
    return go


class SupportModule(unittest.TestCase):
    """channel_support.py holds the shared guards. Without it, a write must stop, never pass."""

    def test_a_missing_support_module_stops_a_write_with_exit_2(self):
        client = FakeClient()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(T, "_CS", None), mock.patch.object(T, "make_client", lambda: client), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                T.main(["send", CHAT, "hello there", "--since", str(T_OLD), "--yes"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("channel_support", out.getvalue() + err.getvalue())
        self.assertEqual(len((out.getvalue() + err.getvalue()).strip().splitlines()), 1)
        self.assertEqual(client.writes(), [])

    def test_bootstrap_takes_the_first_folder_in_order(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules):
            first, second = os.path.join(tmp, "first"), os.path.join(tmp, "second")
            for d, tag in ((first, "first"), (second, "second")):
                os.makedirs(d)
                with open(os.path.join(d, "channel_support.py"), "w") as fh:
                    fh.write("TAG = %r\n" % tag)
            sys.modules.pop("channel_support", None)
            with mock.patch.object(T, "_core_dirs", lambda: [first, second]):
                mod = T._bootstrap()
        self.assertEqual(mod.TAG, "first")

    def test_bootstrap_with_no_module_anywhere_is_none(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules):
            sys.modules.pop("channel_support", None)
            with mock.patch.object(T, "_core_dirs", lambda: [tmp]):
                self.assertIsNone(T._bootstrap())

    def test_the_money_check_does_not_change_sys_path(self):
        before = list(sys.path)
        T._money_hits("It is $500 a month")
        self.assertEqual(sys.path, before)


class RealArchive(unittest.TestCase):
    """One send through the REAL core into a temp archive: exactly one row, no more."""

    CORE = os.path.abspath(os.path.join(HERE, "..", "..", "core"))

    def test_one_send_one_row_in_the_real_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "config.json")
            with open(conf, "w") as fh:
                json.dump({"data_dir": os.path.join(tmp, "data")}, fh)
            names = ("commslib", "kitconfig", "outbound_gate")
            saved = {n: sys.modules.pop(n, None) for n in names}
            env = {"COMMS_KIT_CONFIG": conf, "COMMS_KIT_CORE": self.CORE, "HOME": tmp, "GATE_MODE": "report"}
            client = FakeClient()
            try:
                with mock.patch.dict(os.environ, env), \
                        mock.patch.object(T, "make_client", lambda: client), \
                        mock.patch.object(T, "_money_hits", lambda text: []), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    T.main(["send", CHAT, "Real core check, one row please.", "--since", str(T_OLD), "--yes"])
                rows = []
                for root, _, files in os.walk(os.path.join(tmp, "data", "store")):
                    for name in files:
                        with open(os.path.join(root, name), encoding="utf-8") as fh:
                            rows += [json.loads(line) for line in fh if line.strip().startswith("{")]
            finally:
                for n in names:
                    sys.modules.pop(n, None)
                    if saved[n] is not None:
                        sys.modules[n] = saved[n]
        sends = [r for r in rows if r.get("channel") == "telegram" and r.get("dir") == "send"]
        self.assertEqual(len(client.writes()), 1)
        self.assertEqual(len(sends), 1, rows)
        self.assertIn("Real core check", sends[0]["text"])


if __name__ == "__main__":
    unittest.main()
