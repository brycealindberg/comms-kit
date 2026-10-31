"""The Slack admin verbs: open-dm, find, mentions, edit, delete, channel-*.
Every Slack call is mocked. No test may reach the VPS or Slack, least of all a client workspace.

The ways each verb can go wrong, each pinned below:
  - a write runs without --yes (every write verb is a dry run first);
  - a method outside the allow-list reaches the VPS (python side AND the node script);
  - Slack answers ok:false and the CLI prints it as done, or exits 0;
  - `edit` or `delete` touches a message you did not write, or one that is missing;
  - `edit` puts text in front of a person without --since, or after someone wrote (exit 5);
  - a price or deadline message is edited or deleted unseen (the react money lock, exit 6);
  - `edit` skips the outbound gate or the recipient guard;
  - a bad id, ts, or channel name reaches Slack;
  - `open-dm` sends a message, or says "ready" for a DM the bridge cannot send to yet;
  - a read verb (find, mentions, members) writes anything.

Run: python3 -m pytest -q test_slack_admin.py   (from this folder)
"""
import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import slack_bridge as S  # noqa: E402

_REAL_RUN = subprocess.run          # only the local node-script tests may use it (no network in them)

SELF = "U0SELFOWNER12345"
THEM = "U0THEMPERSON1234"
TS = "1790190627.123456"
CH = "C0TESTCH0001"
DM = "D0TESTDM0001"
WS = "clientws"          # stands for a CLIENT workspace: nothing in this file may write to it live

READ_ONLY = {"conversations.info", "conversations.members", "search.messages", "auth.test"}


def _use_a_stub_detector_if_the_real_one_is_missing(test):
    """The money terms live in the WhatsApp channel's draft_check.py. Where that file is not
    beside this one (a bare checkout), a minimal `$amount` detector stands in so the lock
    logic under test still runs. With the real module present, the real one is used."""
    if S._money_hits("it costs $5") is None:
        stub = mock.patch.object(S, "_money_hits",
                                 side_effect=lambda t: re.findall(r"\$\s?\d[\d,.]*", str(t)))
        stub.start()
        test.addCleanup(stub.stop)


class Base(unittest.TestCase):
    def setUp(self):
        _use_a_stub_detector_if_the_real_one_is_missing(self)
        os.environ.pop("OUTBOUND_GUARDS", None)
        self.calls = []                  # (method, params) the admin script was asked for
        self.responses = {}              # method -> reply override
        self.message = {"ts": TS, "user": SELF, "name": "Alex Doe", "text": "see you at 3"}
        self.live = []                   # any real ssh/subprocess attempt
        self.gate_texts = []
        self.guard_calls = []

        def fake_run(*a, **k):
            self.live.append(a)
            raise AssertionError(f"LIVE subprocess/ssh attempted in a unit test: {a}")

        patches = [
            mock.patch.object(S.subprocess, "run", side_effect=fake_run),
            mock.patch.object(S, "_admin_call", side_effect=self.fake_admin),
            mock.patch.object(S, "_react_call", side_effect=self.fake_check),
            mock.patch.object(S, "chat_unchanged_guard", side_effect=self.fake_unchanged),
            mock.patch.object(S, "_gate_check", side_effect=self.fake_gate),
            mock.patch.object(S, "_gate_begin", return_value=None),
            mock.patch.object(S, "_gate_finish"),
            mock.patch.object(S, "_log_send"),
            mock.patch.object(S, "_apply_recipient_guard", side_effect=self.fake_guard),
            mock.patch.object(S, "load_config", return_value=object()),
            mock.patch.object(S, "request_json", side_effect=self.fake_bridge),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.bridge_channels = [{"id": DM, "workspace": WS}]
        self.since_stale = False

    # --- fakes
    def fake_admin(self, workspace, method, params=None, timeout=90):
        if method not in S.ADMIN_METHODS:
            raise SystemExit("not allowed")
        self.calls.append((method, dict(params or {})))
        if method in self.responses:
            return self.responses[method]
        defaults = {
            "conversations.info": {"ok": True, "result": {"id": CH, "name": "general", "is_private": False,
                                                         "is_im": False, "is_member": True, "num_members": 4}},
            "conversations.open": {"ok": True, "result": {"channel": DM}},
            "conversations.members": {"ok": True, "result": {"members": [SELF, THEM], "next_cursor": ""}},
            "search.messages": {"ok": True, "result": {"total": 1, "matches": [
                {"ts": TS, "channel_id": DM, "channel_name": "Alex", "user": SELF, "username": "alex",
                 "text": "see you at 3", "permalink": "https://x.slack.com/archives/D/p1"}]}},
        }
        return defaults.get(method, {"ok": True, "result": {}})

    def fake_check(self, channel, workspace, ts, name, mode, timeout=90):
        assert mode == "check", "only the read-only check may run here"
        return {"ok": True, "self": SELF, "channel": channel, "message": dict(self.message), "reactions": []}

    def fake_unchanged(self, config, channel, workspace, since):
        if since is not None and self.since_stale:
            print("NOT SENT: 1 newer message(s) in this channel since the one you read:")
            raise SystemExit(S.STALE_EXIT)
        print("  newest message here: [now] X: hi  (--since 1790190627.123456)")

    def fake_gate(self, args, **kw):
        self.gate_texts.append((kw.get("texts"), kw.get("dry_run"), kw.get("source")))
        return None

    def fake_guard(self, channel, exclude):
        self.guard_calls.append(channel)

    def fake_bridge(self, config, path, params=None, **kw):
        return self.bridge_channels

    # --- helpers
    def run_fn(self, fn, **kw):
        out, code = io.StringIO(), 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                fn(argparse.Namespace(**kw))
            except SystemExit as e:
                code = e.code
        return out.getvalue(), code

    def writes(self):
        return [c for c in self.calls if c[0] not in READ_ONLY]

    def methods(self):
        return [c[0] for c in self.calls]

    def tearDown(self):
        self.assertEqual(self.live, [], "a test reached the real VPS")
        for method, _ in self.calls:
            self.assertIn(method, S.ADMIN_METHODS)


# ---------------------------------------------------------------- allow-list

class AllowList(Base):
    def test_exact_methods(self):
        self.assertEqual(S.ADMIN_METHODS, frozenset({
            "conversations.open", "chat.update", "chat.delete", "search.messages",
            "conversations.create", "conversations.invite", "conversations.kick",
            "conversations.rename", "conversations.leave", "conversations.archive",
            "conversations.members", "conversations.info", "auth.test"}))

    def test_python_refuses_other_methods_before_ssh(self):
        # the real _admin_call, not the fake
        for bad in ("chat.postMessage", "conversations.mark", "reactions.add", "users.admin.invite",
                    "files.delete", "conversations.unarchive", "chat.meMessage", "", "conversations.open "):
            with self.assertRaises(SystemExit, msg=bad):
                S._admin_call_impl(WS, bad, {})
        self.assertEqual(self.live, [])

    def test_no_mark_method_is_allowed(self):
        for m in S.ADMIN_METHODS:
            self.assertNotIn("mark", m)

    def test_node_script_refuses_other_methods(self):
        # runs the real script locally; it must refuse BEFORE it looks for a key or a token
        script = os.path.join(HERE, "scripts", "slack-admin.js")
        env = {k: v for k, v in os.environ.items() if k != "ENCRYPTION_KEY"}
        for bad in ("chat.postMessage", "conversations.mark", "reactions.add", "conversations.unarchive",
                    "constructor", "__proto__", "toString"):
            p = _REAL_RUN(["node", script, "someworkspace", bad, "e30="], capture_output=True,
                               text=True, env=env, timeout=30)
            self.assertEqual(p.returncode, 2, (bad, p.stdout, p.stderr))
            self.assertIn("method_not_allowed", p.stdout)

    def test_node_script_refuses_unknown_and_missing_params(self):
        script = os.path.join(HERE, "scripts", "slack-admin.js")
        env = {k: v for k, v in os.environ.items() if k != "ENCRYPTION_KEY"}

        def run(method, params):
            import base64  # noqa: PLC0415
            b64 = base64.b64encode(json.dumps(params).encode()).decode()
            return _REAL_RUN(["node", script, "someworkspace", method, b64], capture_output=True,
                                  text=True, env=env, timeout=30)
        p = run("chat.delete", {"channel": CH, "ts": TS, "as_user": True})
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("bad_params", p.stdout)
        p = run("chat.update", {"channel": CH, "ts": TS})              # text missing
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("bad_params", p.stdout)
        p = run("conversations.open", {"users": "U1,U2,U3"})            # one person only
        self.assertEqual(p.returncode, 2, p.stdout)
        p = run("chat.delete", "not-an-object")
        self.assertEqual(p.returncode, 2, p.stdout)

    def test_node_script_has_no_mark_or_post_call(self):
        src = open(os.path.join(HERE, "scripts", "slack-admin.js"), encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("//"))
        for banned in (".mark", "postMessage", "reactions.", "apiCall", "files.", "admin."):
            self.assertNotIn(banned, code)


class AdminCallImpl(unittest.TestCase):
    """The real ssh wrapper, with subprocess.run mocked."""

    def call(self, proc=None, exc=None, params=None, method="conversations.info"):
        with mock.patch.object(S.subprocess, "run", side_effect=exc, return_value=proc) as run:
            res = S._admin_call_impl("ws", method, params or {"channel": CH})
        return res, run

    def test_ok_line_parsed_and_params_not_in_shell_text(self):
        proc = subprocess.CompletedProcess([], 0, stdout='noise\n{"ok": true, "result": {"id": "C1"}}\n', stderr="")
        res, run = self.call(proc, params={"channel": CH, "x": "$(rm -rf ~);'\""})
        self.assertTrue(res["ok"])
        remote = run.call_args[0][0][-1]
        self.assertNotIn("rm -rf", remote)                 # params travel base64, never as shell text
        self.assertEqual(run.call_args[1]["input"], S.ADMIN_SCRIPT.read_text())

    def test_timeout_is_uncertain_failure(self):
        res, _ = self.call(exc=subprocess.TimeoutExpired("ssh", 5))
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("uncertain"))

    def test_garbage_output_is_uncertain_failure(self):
        res, _ = self.call(subprocess.CompletedProcess([], 255, stdout="", stderr="ssh: no route"))
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("uncertain"))
        self.assertIn("no route", res["error"])

    def test_slack_ok_false_is_definite(self):
        proc = subprocess.CompletedProcess([], 1, stdout='{"ok": false, "error": "cant_delete_message"}\n', stderr="")
        res, _ = self.call(proc)
        self.assertFalse(res["ok"])
        self.assertFalse(res.get("uncertain"))
        self.assertEqual(res["error"], "cant_delete_message")


# ---------------------------------------------------------------- open-dm

class OpenDm(Base):
    def args(self, **kw):
        base = dict(user_id="U0123456789", workspace=WS, wait=0, exclude=None)
        base.update(kw)
        return base

    def test_opens_and_prints_id_sends_nothing(self):
        out, code = self.run_fn(S.run_open_dm, **self.args())
        self.assertEqual(code, 0, out)
        self.assertIn(DM, out)
        self.assertEqual(self.calls, [("conversations.open", {"users": "U0123456789"})])
        self.assertNotIn("chat.postMessage", self.methods())

    def test_known_dm_says_send_works(self):
        out, _ = self.run_fn(S.run_open_dm, **self.args())
        self.assertIn("send works now", out)

    def test_unknown_dm_says_when_send_will_work_and_exits_nonzero_only_when_waiting(self):
        self.bridge_channels = []
        out, code = self.run_fn(S.run_open_dm, **self.args())
        self.assertEqual(code, 0, out)
        self.assertIn("not in the bridge yet", out)
        self.assertIn("15 minutes", out)
        self.assertNotIn("send works now", out)

    def test_wait_gives_up_with_nonzero(self):
        self.bridge_channels = []
        with mock.patch.object(S.time, "sleep"), mock.patch.object(S.time, "monotonic", side_effect=[0, 1, 99, 99, 99]):
            out, code = self.run_fn(S.run_open_dm, **self.args(wait=10))
        self.assertEqual(code, 3, out)
        self.assertIn("still not", out)

    def test_wait_succeeds_when_the_bridge_learns_it(self):
        replies = [[], [], [{"id": DM, "workspace": WS}]]
        self.bridge_channels = None
        with mock.patch.object(S, "request_json", side_effect=lambda *a, **k: replies.pop(0)), \
             mock.patch.object(S.time, "sleep"), \
             mock.patch.object(S.time, "monotonic", side_effect=[0, 1, 2, 3, 4, 5]):
            out, code = self.run_fn(S.run_open_dm, **self.args(wait=100))
        self.assertEqual(code, 0, out)
        self.assertIn("send works now", out)

    def test_ok_false_exits_nonzero(self):
        self.responses["conversations.open"] = {"ok": False, "error": "user_not_found"}
        out, code = self.run_fn(S.run_open_dm, **self.args())
        self.assertNotEqual(code, 0)
        self.assertIn("user_not_found", out)

    def test_bad_user_ids_never_reach_slack(self):
        for bad in ("", "alex", "U0123456789,U0THEMPERSON1234", "C0TESTCH0001", "u0lower", "U1"):
            out, code = self.run_fn(S.run_open_dm, **self.args(user_id=bad))
            self.assertNotEqual(code, 0, bad)
        self.assertEqual(self.calls, [])


# ---------------------------------------------------------------- find / mentions

class Finders(Base):
    def test_find_prints_hit_and_only_reads(self):
        out, code = self.run_fn(S.run_find, text="at 3", workspace=WS, limit=5)
        self.assertEqual(code, 0, out)
        self.assertIn("see you at 3", out)
        self.assertIn(TS, out)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.calls[0][0], "search.messages")
        self.assertEqual(self.calls[0][1]["query"], "at 3")
        self.assertEqual(self.calls[0][1]["count"], 5)

    def test_find_empty_text_refused(self):
        out, code = self.run_fn(S.run_find, text="   ", workspace=WS, limit=5)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.calls, [])

    def test_find_count_is_clamped(self):
        self.run_fn(S.run_find, text="x", workspace=WS, limit=9999)
        self.assertEqual(self.calls[0][1]["count"], 100)
        self.calls.clear()
        self.run_fn(S.run_find, text="x", workspace=WS, limit=0)
        self.assertEqual(self.calls[0][1]["count"], 1)

    def test_find_no_hits_says_so_and_exits_zero(self):
        self.responses["search.messages"] = {"ok": True, "result": {"total": 0, "matches": []}}
        out, code = self.run_fn(S.run_find, text="zzz", workspace=WS, limit=5)
        self.assertEqual(code, 0)
        self.assertIn("no match", out.lower())

    def test_find_ok_false_exits_nonzero(self):
        self.responses["search.messages"] = {"ok": False, "error": "not_allowed_token_type"}
        out, code = self.run_fn(S.run_find, text="x", workspace=WS, limit=5)
        self.assertNotEqual(code, 0)
        self.assertIn("not_allowed_token_type", out)

    def test_mentions_searches_for_him_and_only_reads(self):
        out, code = self.run_fn(S.run_mentions, workspace=WS, limit=7)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.calls[0][0], "search.messages")
        self.assertEqual(self.calls[0][1]["query"], "<@{ME}>")      # the script swaps in his id
        self.assertEqual(self.calls[0][1]["count"], 7)

    def test_mentions_ok_false_exits_nonzero(self):
        self.responses["search.messages"] = {"ok": False, "error": "ratelimited"}
        _out, code = self.run_fn(S.run_mentions, workspace=WS, limit=7)
        self.assertNotEqual(code, 0)


# ---------------------------------------------------------------- edit

class Edit(Base):
    def args(self, **kw):
        base = dict(channel=DM, ts=TS, text="see you at 4", workspace=WS, yes=False, since=None,
                    money_ok=False, override_check=None, resend_ok=False, exclude=None)
        base.update(kw)
        return base

    def test_no_post_without_yes(self):
        out, code = self.run_fn(S.run_edit, **self.args())
        self.assertEqual(code, 0, out)
        self.assertEqual(self.writes(), [])
        self.assertIn("Dry run", out)
        self.assertIn("see you at 3", out)          # shows what it replaces
        self.assertIn("see you at 4", out)

    def test_yes_without_since_refuses_exit_2(self):
        out, code = self.run_fn(S.run_edit, **self.args(yes=True))
        self.assertEqual(code, 2, out)
        self.assertEqual(self.writes(), [])
        self.assertIn("--since", out)

    def test_stale_channel_refuses_exit_5(self):
        self.since_stale = True
        out, code = self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0))
        self.assertEqual(code, S.STALE_EXIT, out)
        self.assertEqual(self.writes(), [])

    def test_yes_with_since_updates_once_with_exact_params(self):
        out, code = self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.writes(), [("chat.update", {"channel": DM, "ts": TS, "text": "see you at 4"})])
        self.assertIn("Edited", out)

    def test_the_archive_row_has_no_receipt_so_a_hook_runtime_keeps_one_row(self):
        # The original send already spent this ts as its receipt, and "Edited ts X." gives the
        # hook no receipt to match. A ts here would mean two rows in a hook runtime.
        out, code = self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0))
        self.assertEqual(code, 0, out)
        S._log_send.assert_called_once()
        channel, text, resp, verb = S._log_send.call_args.args[:4]
        self.assertEqual((channel, text, verb), (DM, "see you at 4", "edit"))
        self.assertIs(resp.get("ok"), True)
        self.assertNotIn("ts", resp)

    def test_someone_elses_message_refused(self):
        self.message["user"] = THEM
        out, code = self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("did not write", out)

    def test_bot_or_user_less_message_refused(self):
        self.message["user"] = None
        _out, code = self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.writes(), [])

    def test_missing_message_refused(self):
        with mock.patch.object(S, "_react_call", return_value={"ok": False, "error": "message_not_found"}):
            out, code = self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("message_not_found", out)

    def test_money_in_old_text_locks_exit_6(self):
        self.message["text"] = "Invoice is $1,200 for October"
        out, code = self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0))
        self.assertEqual(code, S.MONEY_LOCK_EXIT, out)
        self.assertEqual(self.writes(), [])
        out, code = self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0, money_ok=True))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.writes()), 1)

    def test_money_in_new_text_locks_exit_6(self):
        out, code = self.run_fn(S.run_edit, **self.args(text="it is $900 now", yes=True, since=1790190000.0))
        self.assertEqual(code, S.MONEY_LOCK_EXIT, out)
        self.assertEqual(self.writes(), [])

    def test_money_check_that_cannot_run_locks(self):
        with mock.patch.object(S, "_money_hits", return_value=None):
            out, code = self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0))
        self.assertEqual(code, S.MONEY_LOCK_EXIT, out)
        self.assertEqual(self.writes(), [])

    def test_ok_false_exits_nonzero_and_says_not_edited(self):
        self.responses["chat.update"] = {"ok": False, "error": "edit_window_closed"}
        out, code = self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0))
        self.assertEqual(code, 4, out)
        self.assertIn("NOT EDITED", out)
        self.assertIn("edit_window_closed", out)
        self.assertNotIn("Edited ts", out)

    def test_gate_sees_final_text_and_dry_flag(self):
        self.run_fn(S.run_edit, **self.args())
        self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0))
        self.assertEqual(self.gate_texts[0], (["see you at 4"], True, "slack edit"))
        self.assertEqual(self.gate_texts[1], (["see you at 4"], False, "slack edit"))

    def test_gate_denial_stops_before_any_write(self):
        def deny(args, **kw):
            print("NOT SENT: outbound gate (enforce): style")
            raise SystemExit(S.GATE_DENY_EXIT)
        with mock.patch.object(S, "_gate_check", side_effect=deny):
            out, code = self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0))
        self.assertEqual(code, S.GATE_DENY_EXIT)
        self.assertEqual(self.writes(), [])

    def test_recipient_guard_runs(self):
        self.run_fn(S.run_edit, **self.args())
        self.assertEqual(self.guard_calls, [DM])
        with mock.patch.object(S, "_apply_recipient_guard", side_effect=SystemExit("recipient guard refused")):
            _out, code = self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.writes(), [])

    def test_bad_ts_and_empty_text_never_reach_slack(self):
        for kw in ({"ts": "14:53"}, {"ts": ""}, {"text": ""}, {"text": "   "}, {"channel": "general"}):
            _out, code = self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0, **kw))
            self.assertNotEqual(code, 0, kw)
        self.assertEqual(self.calls, [])

    def test_same_text_is_a_noop(self):
        out, code = self.run_fn(S.run_edit, **self.args(text="see you at 3", yes=True, since=1790190000.0))
        self.assertEqual(code, 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("same", out.lower())

    def test_uncertain_ssh_failure_marks_gate_uncertain(self):
        self.responses["chat.update"] = {"ok": False, "error": "no answer", "uncertain": True}
        with mock.patch.object(S, "_gate_check", return_value=("G", "p", "a")), \
             mock.patch.object(S, "_gate_begin", return_value="op1"), \
             mock.patch.object(S, "_gate_finish") as fin:
            out, code = self.run_fn(S.run_edit, **self.args(yes=True, since=1790190000.0))
        self.assertEqual(code, 4)
        self.assertEqual(fin.call_args[0][2], "uncertain")
        self.assertIn("may or may not", out)


# ---------------------------------------------------------------- delete

class Delete(Base):
    def args(self, **kw):
        base = dict(channel=DM, ts=TS, workspace=WS, yes=False, money_ok=False, exclude=None)
        base.update(kw)
        return base

    def test_no_post_without_yes(self):
        out, code = self.run_fn(S.run_delete, **self.args())
        self.assertEqual(code, 0, out)
        self.assertEqual(self.writes(), [])
        self.assertIn("Dry run", out)
        self.assertIn("see you at 3", out)

    def test_yes_deletes_with_exact_params(self):
        out, code = self.run_fn(S.run_delete, **self.args(yes=True))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.writes(), [("chat.delete", {"channel": DM, "ts": TS})])
        self.assertIn("Deleted", out)

    def test_someone_elses_message_refused(self):
        self.message["user"] = THEM
        out, code = self.run_fn(S.run_delete, **self.args(yes=True))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("did not write", out)

    def test_money_lock(self):
        self.message["text"] = "Quote: $5,000 flat"
        out, code = self.run_fn(S.run_delete, **self.args(yes=True))
        self.assertEqual(code, S.MONEY_LOCK_EXIT, out)
        self.assertEqual(self.writes(), [])
        out, code = self.run_fn(S.run_delete, **self.args(yes=True, money_ok=True))
        self.assertEqual(code, 0, out)

    def test_ok_false_exits_nonzero(self):
        self.responses["chat.delete"] = {"ok": False, "error": "cant_delete_message"}
        out, code = self.run_fn(S.run_delete, **self.args(yes=True))
        self.assertEqual(code, 4, out)
        self.assertIn("NOT DELETED", out)
        self.assertNotIn("Deleted ts", out)

    def test_already_gone_is_not_a_success_claim(self):
        self.responses["chat.delete"] = {"ok": False, "error": "message_not_found"}
        out, code = self.run_fn(S.run_delete, **self.args(yes=True))
        self.assertEqual(code, 4)

    def test_bad_ts_never_reaches_slack(self):
        _out, code = self.run_fn(S.run_delete, **self.args(ts="yesterday", yes=True))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.calls, [])


# ---------------------------------------------------------------- channel verbs

class ChannelVerbs(Base):
    def kw(self, **extra):
        base = dict(workspace=WS, yes=False, exclude=None)
        base.update(extra)
        return base

    CASES = [
        ("create", "run_channel_create", dict(name="proj-alpha", private=False),
         "conversations.create", {"name": "proj-alpha", "is_private": False}, False),
        ("create private", "run_channel_create", dict(name="proj-alpha", private=True),
         "conversations.create", {"name": "proj-alpha", "is_private": True}, False),
        ("invite", "run_channel_invite", dict(channel=CH, user_id=THEM),
         "conversations.invite", {"channel": CH, "users": THEM}, True),
        ("remove", "run_channel_remove", dict(channel=CH, user_id=THEM),
         "conversations.kick", {"channel": CH, "user": THEM}, True),
        ("rename", "run_channel_rename", dict(channel=CH, name="proj-beta"),
         "conversations.rename", {"channel": CH, "name": "proj-beta"}, True),
        ("leave", "run_channel_leave", dict(channel=CH),
         "conversations.leave", {"channel": CH}, True),
        ("archive", "run_channel_archive", dict(channel=CH),
         "conversations.archive", {"channel": CH}, True),
    ]

    def test_dry_run_writes_nothing_for_every_write_verb(self):
        for label, fn, extra, _m, _p, _info in self.CASES:
            self.calls.clear()
            out, code = self.run_fn(getattr(S, fn), **self.kw(**extra))
            self.assertEqual(code, 0, (label, out))
            self.assertEqual(self.writes(), [], label)
            self.assertIn("Dry run", out, label)

    def test_yes_writes_exactly_one_call_with_exact_params(self):
        for label, fn, extra, method, params, _info in self.CASES:
            self.calls.clear()
            out, code = self.run_fn(getattr(S, fn), **self.kw(yes=True, **extra))
            self.assertEqual(code, 0, (label, out))
            self.assertEqual(self.writes(), [(method, params)], label)

    def test_ok_false_exits_nonzero_and_does_not_claim_done(self):
        for label, fn, extra, method, _p, _info in self.CASES:
            self.calls.clear()
            self.responses = {method: {"ok": False, "error": "restricted_action"}}
            out, code = self.run_fn(getattr(S, fn), **self.kw(yes=True, **extra))
            self.assertEqual(code, 4, (label, out))
            self.assertIn("restricted_action", out, label)
            self.assertIn("NOT DONE", out, label)

    def test_recipient_guard_runs_on_channel_writes(self):
        for label, fn, extra, _m, _p, has_channel in self.CASES:
            if not has_channel:
                continue
            self.guard_calls.clear()
            self.run_fn(getattr(S, fn), **self.kw(**extra))
            self.assertEqual(self.guard_calls, [CH], label)
        with mock.patch.object(S, "_apply_recipient_guard", side_effect=SystemExit("refused")):
            _o, code = self.run_fn(S.run_channel_leave, **self.kw(channel=CH, yes=True))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.writes(), [])

    def test_preview_names_the_channel_it_will_hit(self):
        out, _ = self.run_fn(S.run_channel_leave, **self.kw(channel=CH))
        self.assertIn("general", out)
        self.assertEqual(self.methods(), ["conversations.info"])

    def test_unreadable_channel_stops_the_preview(self):
        self.responses["conversations.info"] = {"ok": False, "error": "channel_not_found"}
        out, code = self.run_fn(S.run_channel_kick if hasattr(S, "run_channel_kick") else S.run_channel_remove,
                                **self.kw(channel=CH, user_id=THEM, yes=True))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.writes(), [])

    def test_bad_ids_and_names_never_reach_slack(self):
        bad_runs = [
            (S.run_channel_create, dict(name="Has Space", private=False)),
            (S.run_channel_create, dict(name="", private=False)),
            (S.run_channel_create, dict(name="x" * 81, private=False)),
            (S.run_channel_create, dict(name="UPPER", private=False)),
            (S.run_channel_invite, dict(channel="general", user_id=THEM)),
            (S.run_channel_invite, dict(channel=CH, user_id="alex")),
            (S.run_channel_invite, dict(channel=CH, user_id=f"{THEM},U0OTHERPERSON1234")),   # one person per call
            (S.run_channel_remove, dict(channel=CH, user_id="")),
            (S.run_channel_rename, dict(channel=CH, name="Bad Name")),
            (S.run_channel_rename, dict(channel="nope", name="ok-name")),
            (S.run_channel_leave, dict(channel="")),
            (S.run_channel_members, dict(channel="general", limit=10)),
        ]
        for fn, extra in bad_runs:
            _o, code = self.run_fn(fn, **self.kw(yes=True, **extra))
            self.assertNotEqual(code, 0, (fn.__name__, extra))
        self.assertEqual(self.calls, [])

    def test_leaving_a_dm_is_refused(self):
        _o, code = self.run_fn(S.run_channel_leave, **self.kw(channel=DM, yes=True))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.writes(), [])

    def test_the_last_member_is_told_to_archive(self):
        self.responses = {"conversations.leave": {"ok": False, "error": "last_member"}}
        out, code = self.run_fn(S.run_channel_leave, **self.kw(channel=CH, yes=True))
        self.assertEqual(code, 4, out)
        self.assertIn("last_member", out)
        self.assertIn("channel-archive", out)

    def test_only_a_leave_gets_the_archive_advice(self):
        self.responses = {"conversations.rename": {"ok": False, "error": "last_member"}}
        out, code = self.run_fn(S.run_channel_rename, **self.kw(channel=CH, name="proj-beta", yes=True))
        self.assertEqual(code, 4, out)
        self.assertNotIn("channel-archive", out)

    def test_invite_remove_rename_archive_refuse_a_dm(self):
        self.responses["conversations.info"] = {"ok": True, "result": {"id": DM, "name": None, "is_im": True}}
        for fn, extra in ((S.run_channel_invite, dict(user_id=THEM)), (S.run_channel_remove, dict(user_id=THEM)),
                          (S.run_channel_rename, dict(name="ok-name")), (S.run_channel_archive, {})):
            self.calls.clear()
            out, code = self.run_fn(fn, **self.kw(channel=DM, yes=True, **extra))
            self.assertNotEqual(code, 0, fn.__name__)
            self.assertEqual(self.writes(), [], fn.__name__)
            self.assertIn("DM", out)

    def test_removing_yourself_points_to_leave(self):
        self.responses["auth.test"] = {"ok": True, "result": {"user_id": SELF}}
        out, code = self.run_fn(S.run_channel_remove, **self.kw(channel=CH, user_id=SELF, yes=True))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("channel-leave", out)

    def test_members_reads_only_and_needs_no_yes(self):
        out, code = self.run_fn(S.run_channel_members, **self.kw(channel=CH, limit=50))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.writes(), [])
        self.assertIn(THEM, out)
        self.assertEqual(self.methods(), ["conversations.members"])

    def test_members_follows_pages_until_limit(self):
        pages = [{"ok": True, "result": {"members": ["U0AAAAAAAAAA1", "U0AAAAAAAAAA2"], "next_cursor": "c2"}},
                 {"ok": True, "result": {"members": ["U0AAAAAAAAAA3"], "next_cursor": ""}}]
        seen = []

        def fake(ws, method, params=None, timeout=90):
            seen.append(dict(params))
            return pages.pop(0)
        with mock.patch.object(S, "_admin_call", side_effect=fake):
            out, code = self.run_fn(S.run_channel_members, **self.kw(channel=CH, limit=50))
        self.assertEqual(code, 0, out)
        self.assertEqual(seen[1].get("cursor"), "c2")
        for u in ("U0AAAAAAAAAA1", "U0AAAAAAAAAA2", "U0AAAAAAAAAA3"):
            self.assertIn(u, out)

    def test_members_ok_false_exits_nonzero(self):
        self.responses["conversations.members"] = {"ok": False, "error": "channel_not_found"}
        _o, code = self.run_fn(S.run_channel_members, **self.kw(channel=CH, limit=50))
        self.assertNotEqual(code, 0)


# ---------------------------------------------------------------- wiring

class Wiring(Base):
    def test_parser_has_the_eleven_verbs_and_requires_workspace(self):
        p = S.build_parser()
        cases = [
            ["open-dm", "U0123456789"], ["find", "hello"], ["mentions"],
            ["edit", DM, TS, "new"], ["delete", DM, TS],
            ["channel-create", "abc"], ["channel-invite", CH, THEM], ["channel-remove", CH, THEM],
            ["channel-rename", CH, "abc"], ["channel-leave", CH], ["channel-archive", CH],
            ["channel-members", CH],
        ]
        for argv in cases:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit, msg=argv):
                p.parse_args(argv)                       # no --workspace: refused
            ns = p.parse_args(argv + ["--workspace", WS])
            self.assertEqual(ns.command, argv[0])
            if argv[0] not in ("find", "mentions", "open-dm", "channel-members"):
                self.assertFalse(ns.yes, argv)           # every write verb is a dry run by default

    def test_handle_routes_each_verb(self):
        routes = {"open-dm": "run_open_dm", "find": "run_find", "mentions": "run_mentions", "edit": "run_edit",
                  "delete": "run_delete", "channel-create": "run_channel_create",
                  "channel-invite": "run_channel_invite", "channel-remove": "run_channel_remove",
                  "channel-rename": "run_channel_rename", "channel-leave": "run_channel_leave",
                  "channel-archive": "run_channel_archive", "channel-members": "run_channel_members"}
        for cmd, fn in routes.items():
            with mock.patch.object(S, fn) as m:
                S.handle(argparse.Namespace(command=cmd))
            m.assert_called_once()

    def test_find_and_mentions_parse_n(self):
        p = S.build_parser()
        self.assertEqual(p.parse_args(["find", "x", "--workspace", WS, "-n", "7"]).limit, 7)
        self.assertEqual(p.parse_args(["mentions", "--workspace", WS, "-n", "3"]).limit, 3)

    def test_read_verbs_do_not_take_yes(self):
        p = S.build_parser()
        for argv in (["find", "x"], ["mentions"], ["channel-members", CH], ["open-dm", "U0123456789"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                p.parse_args(argv + ["--workspace", WS, "--yes"])

    def test_mark_read_not_added(self):
        src = open(os.path.join(HERE, "slack_bridge.py"), encoding="utf-8").read()
        self.assertNotIn("conversations.mark", src)
        self.assertNotIn(".mark(", src)

    def test_live_guard_itself_works(self):
        # the meta check: an unmocked ssh attempt in this file fails loudly
        with mock.patch.object(S, "_admin_call", S._admin_call_impl):
            with self.assertRaises(AssertionError):
                S._admin_call(WS, "conversations.create", {"name": "x", "is_private": False})
        self.live.clear()                                # that attempt was the point of the test


if __name__ == "__main__":
    unittest.main()
