#!/usr/bin/env python3
"""Tests for channel_support.py (shared helpers of the telegram, gchat and chatwoot adapters).

No network, no real HOME needed. Failure modes covered (written before the code):
  1. a module that fails to load is left half-registered, or the failure hides as a pass
  3. the money check changes sys.path, or reads the wrong draft_check, or passes when it cannot run
  4. a typo in the gate mode turns the gate off
  5. a gate that cannot load in enforce mode lets a send through
  6. a refusing gate does not stop the send (exit 7)
  7. a gate op is started when the gate denies, or finished with a falsy receipt
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import channel_support as CS  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


class LoadModule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def test_loads_by_exact_path_from_the_first_folder_that_has_it(self):
        a, b = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        write(os.path.join(a, "zz_support_probe.py"), "WHERE = 'a'\n")
        write(os.path.join(b, "zz_support_probe.py"), "WHERE = 'b'\n")
        try:
            mod = CS.load_module("zz_support_probe", [os.path.join(self.tmp, "none"), a, b])
            self.assertEqual(mod.WHERE, "a")
        finally:
            sys.modules.pop("zz_support_probe", None)

    def test_missing_module_is_none(self):
        self.assertIsNone(CS.load_module("zz_nothing_here", [self.tmp]))

    def test_broken_module_is_none_and_not_left_in_sys_modules(self):
        write(os.path.join(self.tmp, "zz_broken_probe.py"), "raise RuntimeError('x')\n")
        self.assertIsNone(CS.load_module("zz_broken_probe", [self.tmp]))
        self.assertNotIn("zz_broken_probe", sys.modules)

    def test_core_dirs_follow_the_documented_order_and_skip_missing_folders(self):
        env_dir, near, home = (os.path.join(self.tmp, n) for n in ("env", "near", "home"))
        for d in (env_dir, near, home):
            os.makedirs(d)
        got = CS.core_dirs("COMMS_KIT_CORE", [near, os.path.join(self.tmp, "gone")], home,
                           environ={"COMMS_KIT_CORE": env_dir})
        self.assertEqual(got, [env_dir, near, home])
        got = CS.core_dirs("COMMS_KIT_CORE", [near], os.path.join(self.tmp, "gone"), environ={})
        self.assertEqual(got, [near])


class MoneyHits(unittest.TestCase):
    DRAFT = ("import re\n"
             "MONEY = re.compile(r'\\$\\d+|invoice')\n"
             "def normalize_for_matching(t):\n    return t.lower()\n")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.here = os.path.join(self.tmp, "channels", "x")
        os.makedirs(self.here)
        self.nowhere = os.path.join(self.tmp, "nowhere")

    def put(self, text):
        write(os.path.join(self.tmp, "channels", "whatsapp", "draft_check.py"), text)

    def hits(self, text):
        return CS.money_hits(text, self.here, extra_dirs=[self.nowhere])

    def test_finds_money_terms_without_touching_sys_path(self):
        self.put(self.DRAFT)
        before = list(sys.path)
        self.assertEqual(self.hits("It is $500, plus the Invoice"), ["$500", "invoice"])
        self.assertEqual(sys.path, before)

    def test_clean_text_has_no_hits(self):
        self.put(self.DRAFT)
        self.assertEqual(self.hits("see you at noon"), [])

    def test_no_draft_check_means_the_check_cannot_run(self):
        self.assertIsNone(self.hits("anything"))

    def test_a_draft_check_that_raises_means_the_check_cannot_run(self):
        self.put("raise RuntimeError('x')\n")
        self.assertIsNone(self.hits("anything"))

    def test_a_draft_check_without_the_expected_names_cannot_run(self):
        self.put("X = 1\n")
        self.assertIsNone(self.hits("anything"))

    def test_the_first_draft_check_is_the_one_used(self):
        self.put(self.DRAFT)
        other = os.path.join(self.tmp, "other")
        write(os.path.join(other, "draft_check.py"),
              "import re\nMONEY = re.compile('zzz')\ndef normalize_for_matching(t):\n    return t\n")
        self.assertEqual(CS.money_hits("$5", self.here, extra_dirs=[other]), ["$5"])


class GateMode(unittest.TestCase):
    def mode(self, env=None, file_text=None, file_error=None):
        opener = mock.mock_open(read_data=file_text or "")
        if file_error:
            opener.side_effect = file_error
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GATE_MODE", None)
            if env is not None:
                os.environ["GATE_MODE"] = env
            with mock.patch("builtins.open", opener):
                return CS.gate_mode_fallback()

    def test_env_wins(self):
        self.assertEqual(self.mode(env="enforce"), "enforce")
        self.assertEqual(self.mode(env=" Report "), "report")

    def test_a_typo_means_enforce(self):
        self.assertEqual(self.mode(env="reprot"), "enforce")

    def test_no_file_means_report_an_unreadable_file_means_enforce(self):
        self.assertEqual(self.mode(file_error=FileNotFoundError()), "report")
        self.assertEqual(self.mode(file_error=PermissionError()), "enforce")

    def test_file_value_is_read(self):
        self.assertEqual(self.mode(file_text="enforce\n"), "enforce")


class FakeGate:
    class GateDenied(Exception):
        pass

    class Approvals:
        def __init__(self, **kw):
            self.kw = kw

        @classmethod
        def from_env(cls, **kw):
            return cls(**kw)

    class OutboundPayload:
        def __init__(self, channel, **kw):
            self.channel, self.kw = channel, kw

    def __init__(self, allow=True, deny_begin=False):
        self.allow, self.deny_begin = allow, deny_begin
        self.evaluated, self.finished = [], []

    def evaluate(self, payload, approvals, dry_run=False, source=""):
        self.evaluated.append((payload, approvals, dry_run, source))
        gate = self

        class V:
            allow = gate.allow

            def deny_line(self):
                return "NOT SENT: outbound gate refused."
        return V()

    def begin(self, payload, approvals):
        if self.deny_begin:
            raise self.GateDenied("NOT SENT: duplicate")
        return "op-1"

    def finish(self, op, status, receipts=()):
        self.finished.append((op, status, list(receipts)))


class GateCheck(unittest.TestCase):
    def check(self, gate, mode="report", **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result = CS.gate_check(lambda name: gate, lambda: mode, "chan", True, target="t", text="hi",
                                       scope="s", thread="th", dry_run=False, source="x send")
            except SystemExit as e:
                return e.code, out.getvalue(), err.getvalue(), None
        return 0, out.getvalue(), err.getvalue(), result

    def test_allowed_returns_the_context_and_passes_money_ok_and_the_payload(self):
        gate = FakeGate()
        code, _, _, ctx = self.check(gate)
        self.assertEqual(code, 0)
        payload, approvals, dry_run, source = gate.evaluated[0]
        self.assertEqual(payload.channel, "chan")
        self.assertEqual(payload.kw["recipients"], ["t"])
        self.assertEqual(payload.kw["texts"], ["hi"])
        self.assertTrue(approvals.kw["money_ok"])
        self.assertEqual((dry_run, source), (False, "x send"))
        self.assertIs(ctx[0], gate)

    def test_a_refusal_exits_7_and_prints_the_deny_line(self):
        code, out, _, ctx = self.check(FakeGate(allow=False))
        self.assertEqual(code, 7)
        self.assertIn("outbound gate refused", out)

    def test_unloadable_gate_in_enforce_mode_exits_7(self):
        code, out, _, _ = self.check(None, mode="enforce")
        self.assertEqual(code, 7)
        self.assertIn("NOT SENT", out)

    def test_unloadable_gate_in_report_mode_continues_with_a_warning(self):
        code, _, err, ctx = self.check(None, mode="report")
        self.assertEqual(code, 0)
        self.assertIsNone(ctx)
        self.assertIn("could not load", err)


class GateBeginFinish(unittest.TestCase):
    def test_begin_with_no_context_is_none(self):
        self.assertIsNone(CS.gate_begin(None))

    def test_begin_returns_the_op(self):
        gate = FakeGate()
        self.assertEqual(CS.gate_begin((gate, "p", "a")), "op-1")

    def test_a_denied_begin_exits_7(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            CS.gate_begin((FakeGate(deny_begin=True), "p", "a"))
        self.assertEqual(cm.exception.code, 7)
        self.assertIn("duplicate", out.getvalue())

    def test_finish_drops_falsy_receipts_and_ignores_missing_context_or_op(self):
        gate = FakeGate()
        CS.gate_finish((gate, "p", "a"), "op-1", "sent", ["m1", None, ""])
        self.assertEqual(gate.finished, [("op-1", "sent", ["m1"])])
        CS.gate_finish(None, "op-1", "sent")
        CS.gate_finish((gate, "p", "a"), None, "sent")
        self.assertEqual(len(gate.finished), 1)


if __name__ == "__main__":
    unittest.main()
