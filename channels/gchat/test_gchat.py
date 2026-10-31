#!/usr/bin/env python3
"""Tests for gchat.py. The real `gws` is NEVER run.

Every test points the tool at a stub script in a temp folder (GCHAT_GWS_BIN). The stub records
each call. A second recording stub named `gws` also sits first on PATH, so a bug that skipped the
override would hit that recorder and fail the test, not the owner's real Google login.
(A real gws under a stripped environment or a fake HOME DELETES the owner's login.)

Failure modes covered (written before the code):
  1. the tool runs gws under a fake HOME, or with a stripped environment
  2. a write runs without --yes
  3. --yes without --since still sends
  4. a newer message (from the other person, or a thread reply) still lets the send through
  5. a message with no readable time is ignored
  6. the money lock is skipped, or a money check that cannot run lets the text through
  7. a refusing outbound gate still lets the send through
  8. a timeout or an unreadable answer is retried, exits 0, or archives a row
  9. a clean refusal from Google archives a row or exits 0
 10. a permission error (personal Gmail) prints a traceback or exits 0
 11. a good send archives zero or two rows, or hook_covers is not False
 12. a reply starts a new thread instead of failing
 13. a hostile space name reaches gws
 14. a nine-digit Google timestamp is not read, so --since never works
 15. a missing gws binary prints a traceback
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gchat as G  # noqa: E402

TS_OLD = "2026-10-30T10:00:00.123456Z"
TS_NEW = "2026-10-30T10:05:00.000000Z"
SPACE = "spaces/AAAAexample1"
THREAD = SPACE + "/threads/T1"

STUB_SOURCE = '''#!{py}
import json, os, sys, time
d = os.environ["GCHAT_STUB_DIR"]
argv = sys.argv[1:]
with open(os.path.join(d, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps({{"argv": argv, "canary": os.environ.get("GCHAT_CANARY"),
                          "home": os.environ.get("HOME")}}) + "\\n")
words = []
for a in argv:
    if a.startswith("--"):
        break
    if "@" not in a:
        words.append(a)
key = " ".join(words)
try:
    scenario = json.load(open(os.path.join(d, "scenario.json")))
except OSError:
    scenario = {{}}
state_path = os.path.join(d, "state.json")
try:
    state = json.load(open(state_path))
except (OSError, ValueError):
    state = {{}}
n = state.get(key, 0)
state[key] = n + 1
json.dump(state, open(state_path, "w"))
replies = scenario.get(key) or [{{"rc": 1, "stdout": json.dumps({{"error": {{"code": 404, "message": "no scenario for " + key}}}})}}]
r = replies[min(n, len(replies) - 1)]
if r.get("sleep"):
    time.sleep(r["sleep"])
sys.stdout.write(r.get("stdout", ""))
sys.stderr.write(r.get("stderr", ""))
sys.exit(r.get("rc", 0))
'''

RECORDER = '''#!/bin/sh
printf 'REAL-GWS-REACHED %s\\n' "$*" >> "$GCHAT_RECORDER_LOG"
exit 99
'''


SHARED_DIR = tempfile.mkdtemp(prefix="gchat-stub-")
SHARED_STUB = os.path.join(SHARED_DIR, "gws-stub")
with open(SHARED_STUB, "w") as _fh:
    _fh.write(STUB_SOURCE.format(py=sys.executable))
os.chmod(SHARED_STUB, 0o755)


def tearDownModule():
    __import__("shutil").rmtree(SHARED_DIR, ignore_errors=True)


def gm(i, ts, thread=THREAD, text="hello", reply=False, sender="Alex Doe"):
    return {"name": f"{SPACE}/messages/{i}", "sender": {"name": "users/1", "displayName": sender, "type": "HUMAN"},
            "createTime": ts, "text": text, "thread": {"name": thread}, "threadReply": reply,
            "space": {"name": SPACE}}


def ok(obj):
    return {"rc": 0, "stdout": json.dumps(obj)}


def err(code, message="boom", status=""):
    return {"rc": 1, "stdout": json.dumps({"error": {"code": code, "message": message, "status": status}})}


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
    """A stub gws in a temp folder, a recorder named gws first on PATH, fake gate and archive."""

    CONF = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gchat-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.stub = SHARED_STUB      # one stub per run: a fresh script is slow to start on macOS
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        for name in ("gws", "gws-as"):
            path = os.path.join(self.bindir, name)
            with open(path, "w") as fh:
                fh.write(RECORDER)
            os.chmod(path, 0o755)
        self.recorder_log = os.path.join(self.tmp, "recorder.log")
        open(self.recorder_log, "w").close()
        self.scenario = {}
        self.gate = FakeGate()
        self.comms = FakeComms()
        self.money = []
        mods = {"outbound_gate": self.gate, "commslib": self.comms}
        env = {"GCHAT_GWS_BIN": self.stub, "GCHAT_STUB_DIR": self.tmp, "GCHAT_RECORDER_LOG": self.recorder_log,
               "PATH": self.bindir + os.pathsep + os.environ.get("PATH", ""), "GCHAT_CANARY": "kept"}
        env_clear = [k for k in ("GCHAT_GWS_AS_BIN",) if k in os.environ]
        patches = [
            mock.patch.dict(os.environ, env),
            mock.patch.object(G, "_comms_module", lambda name: mods.get(name)),
            mock.patch.object(G, "_money_hits", lambda text: list(self.money)),
            mock.patch.object(G, "_conf", lambda key, default=None: self.CONF.get(key, default)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for k in env_clear:
            os.environ.pop(k, None)
        self.addCleanup(self.assert_real_gws_never_reached)

    def assert_real_gws_never_reached(self):
        with open(self.recorder_log) as fh:
            self.assertEqual(fh.read(), "", "the PATH recorder was reached: the override was skipped")

    # ---- helpers
    def script(self, key, *replies):
        self.scenario[key] = list(replies)
        with open(os.path.join(self.tmp, "scenario.json"), "w") as fh:
            json.dump(self.scenario, fh)

    def calls(self):
        path = os.path.join(self.tmp, "calls.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    @staticmethod
    def words(call):
        out = []
        for a in call["argv"]:
            if a.startswith("--"):
                break
            if "@" not in a:
                out.append(a)
        return " ".join(out)

    @staticmethod
    def flag(call, name):
        argv = call["argv"]
        return json.loads(argv[argv.index(name) + 1]) if name in argv else None

    def writes(self):
        return [c for c in self.calls() if self.words(c) in
                ("chat spaces messages create", "chat users spaces updateSpaceReadState")]

    def run_cli(self, *argv):
        out, err_ = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err_):
            try:
                G.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return code, out.getvalue(), err_.getvalue()

    def feed_space(self, *messages):
        self.script("chat spaces messages list", ok({"messages": list(messages)}))

    def send(self, *extra, text="Happy to help, here is more.", since=TS_OLD, space=SPACE):
        args = ["send", space, text]
        if since:
            args += ["--since", since]
        return self.run_cli(*args, *extra)

    def reply(self, *extra, text="Happy to help, here is more.", since=TS_OLD):
        args = ["reply", f"{SPACE}/messages/m1", text]
        if since:
            args += ["--since", since]
        return self.run_cli(*args, *extra)


class HomeGuard(unittest.TestCase):
    """The tool must refuse, before any subprocess, to run the real gws under a fake HOME."""

    def run_tool(self, tmp, argv, extra_env=None, home=None, drop_home=False):
        recorder = os.path.join(tmp, "recorder.log")
        open(recorder, "w").close()
        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        for name in ("gws", "gws-as"):
            path = os.path.join(bindir, name)
            with open(path, "w") as fh:
                fh.write(RECORDER)
            os.chmod(path, 0o755)
        env = {"PATH": bindir + os.pathsep + "/usr/bin:/bin", "GCHAT_RECORDER_LOG": recorder,
               "PYTHONDONTWRITEBYTECODE": "1"}
        if not drop_home:
            env["HOME"] = home or tmp
        if os.environ.get("COMMS_KIT_CORE"):
            env["COMMS_KIT_CORE"] = os.environ["COMMS_KIT_CORE"]
        env.update(extra_env or {})
        proc = subprocess.run([sys.executable, os.path.join(HERE, "gchat.py")] + argv,
                              capture_output=True, text=True, env=env, timeout=60)
        with open(recorder) as fh:
            return proc, fh.read()

    def test_fake_home_is_refused_before_any_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            for argv in (["spaces"], [ "send", SPACE, "hi", "--since", TS_OLD, "--yes"],
                         ["history", SPACE], ["mark-read", SPACE, "--yes"]):
                proc, recorded = self.run_tool(tmp, argv)
                self.assertEqual(proc.returncode, 2, (argv, proc.stdout, proc.stderr))
                self.assertEqual(recorded, "", "gws was called under a fake HOME")
                self.assertIn("HOME", proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    def test_missing_home_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, recorded = self.run_tool(tmp, ["spaces"], drop_home=True)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(recorded, "")

    def test_account_wrapper_is_also_refused_under_a_fake_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "config.json")
            with open(conf, "w") as fh:
                json.dump({"gchat": {"gws_account": "alex@example.com"}}, fh)
            proc, recorded = self.run_tool(tmp, ["spaces"], extra_env={"COMMS_KIT_CONFIG": conf})
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(recorded, "")

    def test_override_alone_does_not_unlock_the_account_wrapper(self):
        # GCHAT_GWS_BIN points at a stub, but gws-as would still exec the real gws.
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "config.json")
            with open(conf, "w") as fh:
                json.dump({"gchat": {"gws_account": "alex@example.com"}}, fh)
            stub = os.path.join(tmp, "stub")
            with open(stub, "w") as fh:
                fh.write("#!/bin/sh\nexit 0\n")
            os.chmod(stub, 0o755)
            proc, recorded = self.run_tool(tmp, ["spaces"], extra_env={"COMMS_KIT_CONFIG": conf, "GCHAT_GWS_BIN": stub})
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(recorded, "")

    def test_help_works_under_a_fake_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, recorded = self.run_tool(tmp, ["--help"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(recorded, "")


class Reads(Base):
    def test_spaces_lists_and_pages(self):
        self.script("chat spaces list",
                    ok({"spaces": [{"name": SPACE, "displayName": "Team Room", "spaceType": "SPACE"}],
                        "nextPageToken": "p2"}),
                    ok({"spaces": [{"name": "spaces/BBBB", "displayName": "Ops", "spaceType": "SPACE"}]}))
        code, out, _ = self.run_cli("spaces")
        self.assertEqual(code, 0)
        self.assertIn("Team Room", out)
        self.assertIn("Ops", out)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.flag(calls[1], "--params")["pageToken"], "p2")
        self.assertEqual(self.writes(), [])

    def test_history_is_oldest_first_windowed_and_clamped(self):
        self.feed_space(gm("m2", TS_NEW, text="newer text"), gm("m1", TS_OLD, text="older text"))
        code, out, _ = self.run_cli("history", SPACE, "-n", "5000", "--days", "3")
        self.assertEqual(code, 0)
        self.assertLess(out.index("older text"), out.index("newer text"))
        params = self.flag(self.calls()[0], "--params")
        self.assertEqual(params["parent"], SPACE)
        self.assertRegex(params["filter"], r'^create_time > "\d{4}-\d\d-\d\dT')
        self.assertLessEqual(params["pageSize"], 1000)

    def test_history_keeps_the_newest_n(self):
        self.feed_space(*[gm(f"m{i}", f"2026-10-30T10:0{i}:00.000000Z", text=f"msg{i}") for i in range(1, 6)])
        code, out, _ = self.run_cli("history", SPACE, "-n", "2")
        self.assertIn("msg5", out)
        self.assertIn("msg4", out)
        self.assertNotIn("msg3", out)

    def test_find_is_client_side_and_case_insensitive(self):
        self.feed_space(gm("m1", TS_OLD, text="The Invoice is late"), gm("m2", TS_NEW, text="lunch?"))
        code, out, _ = self.run_cli("find", "invoice", "--space", SPACE)
        self.assertEqual(code, 0)
        self.assertIn("Invoice", out)
        self.assertNotIn("lunch", out)
        self.assertIn("(1 found", out)
        for call in self.calls():
            self.assertNotIn("invoice", json.dumps(call["argv"]).lower())   # the text never goes to the server

    def test_find_without_a_space_walks_the_listed_spaces(self):
        self.script("chat spaces list", ok({"spaces": [{"name": SPACE}, {"name": "spaces/BBBB"}]}))
        self.script("chat spaces messages list", ok({"messages": [gm("m1", TS_OLD, text="needle here")]}),
                    ok({"messages": [gm("m9", TS_NEW, text="nothing")]}))
        code, out, _ = self.run_cli("find", "needle")
        self.assertEqual(code, 0)
        self.assertIn("needle here", out)
        self.assertEqual(len([c for c in self.calls() if self.words(c) == "chat spaces messages list"]), 2)

    def test_hostile_space_names_never_reach_gws(self):
        for bad in ("spaces/../x", "x", "spaces/", "spaces/a b", "-rf", "spaces/a;b", "spaces/a/messages/b"):
            code, _, _ = self.run_cli("history", bad)
            self.assertEqual(code, 2, bad)
        self.assertEqual(self.calls(), [])

    def test_full_environment_reaches_gws(self):
        self.script("chat spaces list", ok({"spaces": []}))
        self.run_cli("spaces")
        self.assertEqual(self.calls()[0]["canary"], "kept")
        self.assertEqual(self.calls()[0]["home"], os.environ["HOME"])

    def test_missing_gws_binary_is_a_plain_line(self):
        with mock.patch.dict(os.environ, {"GCHAT_GWS_BIN": os.path.join(self.tmp, "nope")}):
            code, out, err_ = self.run_cli("spaces")
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", out + err_)
        self.assertIn("gws", out + err_)


class Permission(Base):
    def test_personal_gmail_gets_a_plain_workspace_line_and_nonzero_exit(self):
        for status in (403, 401):
            self.script("chat spaces list", err(status, "The caller does not have permission", "PERMISSION_DENIED"))
            code, out, err_ = self.run_cli("spaces")
            self.assertEqual(code, 3)
            text = out + err_
            self.assertIn("Workspace", text)
            self.assertIn("Chat", text)
            self.assertNotIn("Traceback", text)

    def test_permission_error_on_send_is_not_sent_and_not_archived(self):
        self.feed_space(gm("m1", TS_OLD))
        self.script("chat spaces messages create", err(403, "nope", "PERMISSION_DENIED"))
        code, out, err_ = self.send("--yes")
        self.assertEqual(code, 3)
        self.assertIn("NOT SENT", out + err_)
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.finished[-1][1], "failed_before_send")

    def test_other_errors_are_failures_with_a_short_line(self):
        self.script("chat spaces list", err(404, "Requested entity was not found."))
        code, out, err_ = self.run_cli("spaces")
        self.assertEqual(code, 1)
        self.assertIn("not found", (out + err_).lower())


class SendGuards(Base):
    def test_dry_run_calls_no_write_and_archives_nothing(self):
        self.feed_space(gm("m1", TS_OLD))
        self.script("chat spaces messages get", ok(gm("m1", TS_OLD)))
        for fn in (self.send, self.reply):
            code, out, _ = fn()
            self.assertEqual(code, 0)
            self.assertIn("DRY RUN", out)
            self.assertIn("Happy to help", out)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.begun, 0)

    def test_yes_without_since_exits_2_and_calls_nothing(self):
        for fn in (self.send, self.reply):
            code, out, err_ = fn("--yes", since=None)
            self.assertEqual(code, 2)
            self.assertIn("--since", out + err_)
        self.assertEqual(self.writes(), [])

    def test_newer_message_from_the_other_person_exits_5(self):
        self.feed_space(gm("m9", TS_NEW, text="wait, one more thing"), gm("m1", TS_OLD))
        self.script("chat spaces messages get", ok(gm("m1", TS_OLD)))
        for fn in (self.send, self.reply):
            code, out, _ = fn("--yes")
            self.assertEqual(code, 5)
            self.assertIn("NOT SENT:", out)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.comms.rows, [])

    def test_newer_thread_reply_exits_5(self):
        newer_reply = gm("m9", TS_NEW, thread=SPACE + "/threads/OTHER", reply=True, text="a reply in a thread")
        self.feed_space(newer_reply, gm("m1", TS_OLD))
        self.script("chat spaces messages get", ok(gm("m1", TS_OLD)))
        for fn in (self.send, self.reply):
            code, out, _ = fn("--yes")
            self.assertEqual(code, 5, out)
        self.assertEqual(self.writes(), [])

    def test_the_server_is_asked_only_for_messages_after_since(self):
        self.feed_space()
        self.send()
        params = self.flag(self.calls()[0], "--params")
        self.assertIn(f'create_time > "{TS_OLD}"', params["filter"])

    def test_message_at_exactly_since_is_not_newer(self):
        self.feed_space(gm("m1", TS_OLD))
        self.script("chat spaces messages create", ok(gm("new", TS_NEW)))
        code, _, _ = self.send("--yes")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.writes()), 1)

    def test_message_with_no_time_is_refused_not_ignored(self):
        broken = gm("m9", TS_NEW)
        del broken["createTime"]
        self.feed_space(broken, gm("m1", TS_OLD))
        code, _, _ = self.send("--yes")
        self.assertEqual(code, 5)
        self.assertEqual(self.writes(), [])

    def test_a_second_page_of_newer_messages_is_still_read(self):
        self.script("chat spaces messages list",
                    ok({"messages": [gm("m1", TS_OLD)], "nextPageToken": "more"}),
                    ok({"messages": [gm("m9", TS_NEW)]}))
        code, _, _ = self.send("--yes")
        self.assertEqual(code, 5)
        self.assertEqual(self.writes(), [])

    def test_since_junk_exits_2(self):
        code, _, _ = self.send("--yes", since="yesterday")
        self.assertEqual(code, 2)
        self.assertEqual(self.calls(), [])

    def test_empty_text_is_refused(self):
        code, _, _ = self.send("--yes", text="  ")
        self.assertEqual(code, 1)
        self.assertEqual(self.calls(), [])

    def test_money_lock_exits_6_without_money_ok(self):
        self.feed_space(gm("m1", TS_OLD))
        self.money = ["$500"]
        code, out, _ = self.send("--yes", text="It is $500 a month")
        self.assertEqual(code, 6)
        self.assertIn("MONEY LOCK", out)
        self.assertEqual(self.writes(), [])

    def test_money_ok_lets_it_through(self):
        self.feed_space(gm("m1", TS_OLD))
        self.script("chat spaces messages create", ok(gm("new", TS_NEW)))
        self.money = ["$500"]
        code, _, _ = self.send("--yes", "--money-ok", text="It is $500 a month")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.writes()), 1)

    def test_money_check_that_cannot_run_locks(self):
        self.feed_space(gm("m1", TS_OLD))
        with mock.patch.object(G, "_money_hits", lambda text: None):
            code, _, _ = self.send("--yes")
        self.assertEqual(code, 6)
        self.assertEqual(self.writes(), [])

    def test_gate_refusal_sends_nothing(self):
        self.feed_space(gm("m1", TS_OLD))
        self.gate.allow = False
        code, _, _ = self.send("--yes")
        self.assertEqual(code, 7)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.comms.rows, [])

    def test_gate_that_cannot_load_in_enforce_mode_sends_nothing(self):
        self.feed_space(gm("m1", TS_OLD))
        with mock.patch.object(G, "_comms_module", lambda name: None), \
                mock.patch.object(G, "_gate_mode_fallback", lambda: "enforce"):
            code, _, _ = self.send("--yes")
        self.assertEqual(code, 7)
        self.assertEqual(self.writes(), [])


class SendResults(Base):
    def test_send_writes_exactly_one_call_and_one_archive_row(self):
        self.feed_space(gm("m1", TS_OLD))
        self.script("chat spaces messages create", ok(gm("new", TS_NEW, text="Happy to help, here is more.")))
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 0)
        writes = self.writes()
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.flag(writes[0], "--params")["parent"], SPACE)
        self.assertEqual(self.flag(writes[0], "--json"), {"text": "Happy to help, here is more."})
        self.assertEqual(len(self.comms.rows), 1)
        args, kw = self.comms.rows[0]
        self.assertEqual(args[:3], ("gchat", "send", SPACE))
        self.assertIs(kw["hook_covers"], False)
        self.assertEqual(kw["verb"], "send")
        self.assertEqual(kw["message_id"], f"{SPACE}/messages/new")
        self.assertEqual(self.gate.finished[-1][1], "sent")

    def test_reply_goes_into_the_thread_and_fails_rather_than_start_a_new_one(self):
        self.feed_space(gm("m1", TS_OLD))
        self.script("chat spaces messages get", ok(gm("m1", TS_OLD, thread=THREAD)))
        self.script("chat spaces messages create", ok(gm("new", TS_NEW, thread=THREAD)))
        code, _, _ = self.reply("--yes")
        self.assertEqual(code, 0)
        w = self.writes()[0]
        self.assertEqual(self.flag(w, "--json")["thread"], {"name": THREAD})
        self.assertEqual(self.flag(w, "--params")["messageReplyOption"], "REPLY_MESSAGE_OR_FAIL")
        self.assertEqual(self.flag(w, "--params")["parent"], SPACE)
        self.assertEqual(len(self.comms.rows), 1)
        self.assertEqual(self.comms.rows[0][1]["verb"], "reply")

    def test_reply_to_a_message_with_no_thread_is_refused(self):
        no_thread = gm("m1", TS_OLD)
        del no_thread["thread"]
        self.script("chat spaces messages get", ok(no_thread))
        code, out, _ = self.reply("--yes")
        self.assertEqual(code, 1)
        self.assertIn("NOT SENT", out)
        self.assertEqual(self.writes(), [])

    def test_reply_to_an_unknown_message_is_refused(self):
        self.script("chat spaces messages get", err(404, "Message not found"))
        code, _, _ = self.reply("--yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.writes(), [])

    def test_a_timeout_is_uncertain_never_retried_never_archived(self):
        self.feed_space(gm("m1", TS_OLD))
        self.script("chat spaces messages create", {"rc": 0, "stdout": "{}", "sleep": 5})
        with mock.patch.object(G, "SEND_TIMEOUT", 1):
            code, out, err_ = self.send("--yes")
        self.assertEqual(code, G.UNCERTAIN_EXIT)
        self.assertIn("UNCERTAIN", out + err_)
        self.assertIn("do not resend", (out + err_).lower())
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.finished[-1][1], "uncertain")

    def test_an_answer_that_cannot_be_read_is_uncertain(self):
        self.feed_space(gm("m1", TS_OLD))
        for reply in ({"rc": 0, "stdout": ""}, {"rc": 0, "stdout": "not json"}, {"rc": 0, "stdout": "{}"},
                      {"rc": 137, "stdout": ""}, err(500, "backend error"), err(503, "unavailable")):
            self.calls_reset()
            self.script("chat spaces messages create", reply)
            code, out, err_ = self.send("--yes")
            self.assertEqual(code, G.UNCERTAIN_EXIT, reply)
            self.assertEqual(len(self.writes()), 1, reply)
        self.assertEqual(self.comms.rows, [])

    def calls_reset(self):
        for name in ("calls.jsonl", "state.json"):
            path = os.path.join(self.tmp, name)
            if os.path.exists(path):
                os.remove(path)
        self.feed_space(gm("m1", TS_OLD))

    def test_a_clean_4xx_refusal_is_a_failure_and_not_archived(self):
        self.feed_space(gm("m1", TS_OLD))
        self.script("chat spaces messages create", err(404, "Space not found"))
        code, out, err_ = self.send("--yes")
        self.assertEqual(code, 1)
        self.assertIn("NOT SENT", out + err_)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.finished[-1][1], "failed_before_send")


class MarkRead(Base):
    def test_dry_run_calls_nothing(self):
        code, out, _ = self.run_cli("mark-read", SPACE)
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertEqual(self.calls(), [])

    def test_yes_updates_the_read_state_once(self):
        self.script("chat users spaces updateSpaceReadState", ok({"name": "users/me/spaces/x/spaceReadState"}))
        code, _, _ = self.run_cli("mark-read", SPACE, "--yes")
        self.assertEqual(code, 0)
        w = self.writes()
        self.assertEqual(len(w), 1)
        params = self.flag(w[0], "--params")
        self.assertEqual(params["name"], f"users/me/{SPACE}/spaceReadState")
        self.assertEqual(params["updateMask"], "last_read_time")
        self.assertRegex(self.flag(w[0], "--json")["lastReadTime"], r"^\d{4}-\d\d-\d\dT.*Z$")
        self.assertEqual(self.comms.rows, [])

    def test_unknown_result_is_uncertain(self):
        self.script("chat users spaces updateSpaceReadState", {"rc": 0, "stdout": "", "sleep": 5})
        with mock.patch.object(G, "SEND_TIMEOUT", 1):
            code, out, err_ = self.run_cli("mark-read", SPACE, "--yes")
        self.assertEqual(code, G.UNCERTAIN_EXIT)
        self.assertIn("UNCERTAIN", out + err_)

    def test_permission_error_is_the_workspace_line(self):
        self.script("chat users spaces updateSpaceReadState", err(403, "scope", "PERMISSION_DENIED"))
        code, out, err_ = self.run_cli("mark-read", SPACE, "--yes")
        self.assertEqual(code, 3)
        self.assertIn("Workspace", out + err_)


class AccountWrapper(Base):
    def test_gws_account_goes_through_the_wrapper(self):
        wrapper = SHARED_STUB
        self.script("chat spaces list", ok({"spaces": []}))
        with mock.patch.dict(os.environ, {"GCHAT_GWS_AS_BIN": wrapper}), \
                mock.patch.dict(self.CONF, {"gchat.gws_account": "alex@example.com"}):
            code, _, _ = self.run_cli("spaces")
        self.assertEqual(code, 0)
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[:2], ["alex@example.com", "chat"])

    def test_a_bad_account_name_never_reaches_a_subprocess(self):
        wrapper = SHARED_STUB
        for bad in ("--help", "a b@example.com", "../x", "nobody", "a@@example.com"):
            with mock.patch.dict(os.environ, {"GCHAT_GWS_AS_BIN": wrapper}), \
                    mock.patch.dict(self.CONF, {"gchat.gws_account": bad}):
                code, _, _ = self.run_cli("spaces")
            self.assertEqual(code, 2, bad)
        self.assertEqual(self.calls(), [])


class TimeParsing(unittest.TestCase):
    def test_google_timestamps_with_any_fraction_length(self):
        base = G.parse_ts("2026-10-30T10:00:00Z")
        self.assertAlmostEqual(G.parse_ts("2026-10-30T10:00:00.123456789Z") - base, 0.123456, places=5)
        self.assertAlmostEqual(G.parse_ts("2026-10-30T10:00:00.5Z") - base, 0.5, places=5)
        self.assertAlmostEqual(G.parse_ts("2026-10-30T10:00:00.123Z") - base, 0.123, places=5)
        self.assertEqual(G.parse_ts("2026-10-30T12:00:00+02:00"), base)

    def test_junk_is_none(self):
        for bad in ("", None, "yesterday", "2026-13-45T00:00:00Z"):
            self.assertIsNone(G.parse_ts(bad), bad)


def pages_of(*pages):
    """Scripted `messages list` answers: each page links to the next with a token."""
    out = []
    for i, rows in enumerate(pages):
        body = {"messages": list(rows)}
        if i < len(pages) - 1:
            body["nextPageToken"] = f"tok{i + 2}"
        out.append(ok(body))
    return out


class BusySpace(Base):
    """More pages than the page limit: the newest messages must still be the ones on screen."""

    def busy(self):
        # newest first, as orderBy asks. Page 3 (never read) holds the oldest message.
        self.script("chat spaces messages list", *pages_of(
            [gm("m10", "2026-10-30T10:10:00.000000Z", text="newest"), gm("m9", "2026-10-30T10:09:00.000000Z")],
            [gm("m8", "2026-10-30T10:08:00.000000Z"), gm("m7", "2026-10-30T10:07:00.000000Z")],
            [gm("m1", "2026-10-30T10:01:00.000000Z", text="oldest of all")]))

    def test_history_asks_for_newest_first_and_shows_the_real_newest(self):
        self.busy()
        with mock.patch.object(G, "MAX_PAGES", 2):
            code, out, _ = self.run_cli("history", SPACE, "-n", "200")
        self.assertEqual(code, 0)
        self.assertEqual(self.flag(self.calls()[0], "--params")["orderBy"], "create_time DESC")
        self.assertIn("newest", out)
        self.assertNotIn("oldest of all", out)
        self.assertLess(out.index("m7"), out.index("m10"))          # shown oldest first
        self.assertIn("cut at the page limit", out)                  # and the note still prints
        self.assertEqual(len(self.calls()), 2)

    def test_history_stops_reading_once_it_has_enough(self):
        self.busy()
        code, out, _ = self.run_cli("history", SPACE, "-n", "2")
        self.assertEqual(len(self.calls()), 1)
        self.assertNotIn("cut at the page limit", out)

    def test_a_server_that_ignores_the_order_is_caught_when_the_answer_is_cut(self):
        self.script("chat spaces messages list", *pages_of(
            [gm("m1", "2026-10-30T10:01:00.000000Z"), gm("m2", "2026-10-30T10:02:00.000000Z")],
            [gm("m3", "2026-10-30T10:03:00.000000Z")]))
        with mock.patch.object(G, "MAX_PAGES", 1):
            code, out, err_ = self.run_cli("history", SPACE, "-n", "200")
        self.assertEqual(code, 1)
        self.assertIn("newest messages first", out + err_)

    def test_since_still_finds_a_newer_message_in_a_cut_answer(self):
        self.busy()
        with mock.patch.object(G, "MAX_PAGES", 2):
            code, out, _ = self.send("--yes", since="2026-10-30T10:09:00.000000Z")
        self.assertEqual(code, 5)
        self.assertIn("NOT SENT:", out)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.flag(self.calls()[0], "--params")["orderBy"], "create_time DESC")

    def test_since_with_a_cut_answer_and_nothing_newer_on_screen_still_refuses(self):
        # all pages are "after since" by the server filter; too many to check is a refusal, not a pass
        self.script("chat spaces messages list", *pages_of([gm("m9", TS_OLD)], [gm("m8", TS_OLD)]))
        with mock.patch.object(G, "MAX_PAGES", 1):
            code, out, _ = self.send("--yes")
        self.assertEqual(code, 5)
        self.assertEqual(self.writes(), [])


class FindIsHonestAboutWhatItSkipped(Base):
    def spaces(self, names, per_page=2):
        pages = [names[i:i + per_page] for i in range(0, len(names), per_page)]
        replies = []
        for i, chunk in enumerate(pages):
            body = {"spaces": [{"name": n} for n in chunk]}
            if i < len(pages) - 1:
                body["nextPageToken"] = f"s{i + 2}"
            replies.append(ok(body))
        self.script("chat spaces list", *replies)

    def test_spaces_beyond_the_limit_make_the_last_line_say_partial(self):
        self.spaces([f"spaces/S{i}" for i in range(5)])
        self.script("chat spaces messages list", ok({"messages": [gm("m1", TS_OLD, text="nothing here")]}))
        code, out, _ = self.run_cli("find", "needle", "--max-spaces", "2")
        self.assertEqual(code, 0)
        last = out.strip().splitlines()[-1]
        self.assertIn("PARTIAL", last)
        self.assertIn("3 spaces not searched", last)
        self.assertNotRegex(last, r"^\(0 found in 2 spaces, last")
        self.assertEqual(len([c for c in self.calls() if self.words(c) == "chat spaces messages list"]), 2)

    def test_the_space_list_is_paged_not_cut_at_one_page(self):
        self.spaces([f"spaces/S{i}" for i in range(5)], per_page=2)
        self.script("chat spaces messages list", ok({"messages": []}))
        self.run_cli("find", "needle", "--max-spaces", "10")
        self.assertEqual(len([c for c in self.calls() if self.words(c) == "chat spaces messages list"]), 5)

    def test_a_cut_space_makes_the_last_line_say_partial(self):
        self.spaces(["spaces/S0", "spaces/S1"])
        self.script("chat spaces messages list", *pages_of([gm("m1", TS_OLD, text="x")], [gm("m0", TS_OLD)]))
        with mock.patch.object(G, "MAX_PAGES", 1):
            code, out, _ = self.run_cli("find", "needle")
        self.assertEqual(code, 0)
        last = out.strip().splitlines()[-1]
        self.assertIn("PARTIAL", last)
        self.assertIn("page limit", last)

    def test_a_cut_single_space_is_partial_too(self):
        self.script("chat spaces messages list", *pages_of([gm("m1", TS_OLD, text="x")], [gm("m0", TS_OLD)]))
        with mock.patch.object(G, "MAX_PAGES", 1):
            code, out, _ = self.run_cli("find", "needle", "--space", SPACE)
        self.assertIn("PARTIAL", out.strip().splitlines()[-1])

    def test_a_complete_search_with_no_hits_is_not_called_partial(self):
        self.spaces(["spaces/S0", "spaces/S1"])
        self.script("chat spaces messages list", ok({"messages": [gm("m1", TS_OLD, text="x")]}))
        code, out, _ = self.run_cli("find", "needle")
        self.assertEqual(code, 0)
        last = out.strip().splitlines()[-1]
        self.assertIn("0 found in 2 spaces", last)
        self.assertNotIn("PARTIAL", last)


class WriteErrorCodes(Base):
    """Only a clear numeric 4xx 'no' means not sent. Anything else is unknown."""

    def send_with(self, reply):
        self.feed_space(gm("m1", TS_OLD))
        self.script("chat spaces messages create", reply)
        return self.send("--yes")

    def test_non_numeric_and_ambiguous_codes_are_uncertain_with_no_traceback(self):
        for code_value in ("ABORTED", "UNAVAILABLE", 408, 409, 500, 503, None, 0):
            self.calls_reset()
            reply = {"rc": 1, "stdout": json.dumps({"error": {"code": code_value, "message": "x"}})}
            code, out, err_ = self.send_with(reply)
            self.assertEqual(code, G.UNCERTAIN_EXIT, code_value)
            self.assertIn("UNCERTAIN", out + err_)
            self.assertNotIn("Traceback", out + err_)
            self.assertEqual(self.gate.finished[-1][1], "uncertain", code_value)
            self.assertEqual(len(self.writes()), 1, code_value)
        self.assertEqual(self.comms.rows, [])

    def test_clear_numeric_refusals_are_not_sent(self):
        for code_value in (400, 404, 422, 429, "404"):
            self.calls_reset()
            code, out, err_ = self.send_with(err(code_value, "no"))
            self.assertEqual(code, 1, code_value)
            self.assertIn("NOT SENT", out + err_)
            self.assertEqual(self.gate.finished[-1][1], "failed_before_send", code_value)
        self.assertEqual(self.comms.rows, [])

    def test_permission_codes_still_print_the_workspace_line_first(self):
        for code_value in (401, 403, "403"):
            self.calls_reset()
            code, out, err_ = self.send_with(err(code_value, "denied"))
            self.assertEqual(code, 3, code_value)
            self.assertTrue((out + err_).strip().splitlines()[-1].startswith("NOT SENT: Google Chat needs a Workspace"))
        self.assertEqual(self.comms.rows, [])

    def test_the_same_rules_hold_for_mark_read(self):
        self.script("chat users spaces updateSpaceReadState",
                    {"rc": 1, "stdout": json.dumps({"error": {"code": "ABORTED", "message": "x"}})})
        code, out, err_ = self.run_cli("mark-read", SPACE, "--yes")
        self.assertEqual(code, G.UNCERTAIN_EXIT)
        self.assertNotIn("Traceback", out + err_)

    def calls_reset(self):
        for name in ("calls.jsonl", "state.json"):
            path = os.path.join(self.tmp, name)
            if os.path.exists(path):
                os.remove(path)
        self.gate.finished.clear()


class SupportModule(Base):
    """channel_support.py holds the shared guards. Without it, a write must stop, never pass."""

    def test_a_missing_support_module_stops_a_write_with_exit_2(self):
        self.feed_space(gm("m1", TS_OLD))
        with mock.patch.object(G, "_CS", None):
            code, out, err_ = self.send("--yes")
        self.assertEqual(code, 2)
        self.assertIn("channel_support", out + err_)
        self.assertEqual(len((out + err_).strip().splitlines()), 1)
        self.assertEqual(self.calls(), [])

    def test_bootstrap_takes_the_first_folder_in_order(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules):
            first, second = os.path.join(tmp, "first"), os.path.join(tmp, "second")
            for d, tag in ((first, "first"), (second, "second")):
                os.makedirs(d)
                with open(os.path.join(d, "channel_support.py"), "w") as fh:
                    fh.write("TAG = %r\n" % tag)
            sys.modules.pop("channel_support", None)
            with mock.patch.object(G, "_core_dirs", lambda: [first, second]):
                mod = G._bootstrap()
        self.assertEqual(mod.TAG, "first")

    def test_bootstrap_with_no_module_anywhere_is_none(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules):
            sys.modules.pop("channel_support", None)
            with mock.patch.object(G, "_core_dirs", lambda: [tmp]):
                self.assertIsNone(G._bootstrap())

    def test_the_money_check_does_not_change_sys_path(self):
        before = list(sys.path)
        REAL_MONEY_HITS("It is $500 a month")
        self.assertEqual(sys.path, before)


class RealArchive(Base):
    """One send through the REAL core into a temp archive: exactly one row, no more."""

    CORE = os.path.abspath(os.path.join(HERE, "..", "..", "core"))

    def test_one_send_one_row_in_the_real_archive(self):
        self.feed_space(gm("m1", TS_OLD))
        self.script("chat spaces messages create", ok(gm("new", TS_NEW, text="Real core check, one row please.")))
        conf = os.path.join(self.tmp, "config.json")
        with open(conf, "w") as fh:
            json.dump({"data_dir": os.path.join(self.tmp, "data")}, fh)
        names = ("commslib", "kitconfig", "outbound_gate")
        saved = {n: sys.modules.pop(n, None) for n in names}
        try:
            with mock.patch.dict(os.environ, {"COMMS_KIT_CONFIG": conf, "COMMS_KIT_CORE": self.CORE,
                                              "GATE_MODE": "report"}), \
                    mock.patch.object(G, "_comms_module", REAL_COMMS_MODULE), \
                    mock.patch.object(G, "_conf", lambda key, default=None: default):
                self.send("--yes", text="Real core check, one row please.")
            rows = []
            for root, _, files in os.walk(os.path.join(self.tmp, "data", "store")):
                for name in files:
                    with open(os.path.join(root, name), encoding="utf-8") as fh:
                        rows += [json.loads(line) for line in fh if line.strip().startswith("{")]
        finally:
            for n in names:
                sys.modules.pop(n, None)
                if saved[n] is not None:
                    sys.modules[n] = saved[n]
        sends = [r for r in rows if r.get("channel") == "gchat" and r.get("dir") == "send"]
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(len(sends), 1, rows)
        self.assertIn("Real core check", sends[0]["text"])


REAL_COMMS_MODULE = G._comms_module   # the function as shipped, before any test patches it
REAL_MONEY_HITS = G._money_hits

if __name__ == "__main__":
    unittest.main()
