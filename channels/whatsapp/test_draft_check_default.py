#!/usr/bin/env python3
"""The default pre-send check. Each test is a way a money term or an oversize message slips out."""
import importlib.util
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "draft_check.py")
SPEC = importlib.util.spec_from_file_location("kit_draft_check", PATH)
dc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dc)


def money(text):
    return [m.group(0) for m in dc.MONEY.finditer(dc.normalize_for_matching(text))]


def run_cli(*bubbles, channel="imessage"):
    """The exact way the iMessage wrapper runs it: isolated python, nearly empty env."""
    with tempfile.TemporaryDirectory() as scratch:
        paths = []
        for index, text in enumerate(bubbles):
            path = os.path.join(scratch, f"bubble{index}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            paths.append(path)
        return subprocess.run(
            ["/usr/bin/python3", "-I", PATH, "--channel", channel, *paths],
            capture_output=True, text=True, timeout=30,
            env={"PATH": "/usr/bin:/bin", "HOME": scratch, "LC_ALL": "C.UTF-8"})


class Money(unittest.TestCase):
    def test_plain_amounts_and_words_are_caught(self):
        for text in ("the deposit is $500", "that is 2k usd", "I will send the invoice",
                     "we can refund it", "pay by wire", "it is 40 per month"):
            self.assertTrue(money(text), text)

    def test_an_invisible_character_cannot_split_an_amount(self):
        self.assertTrue(money("$​500"))
        self.assertTrue(money("de‍posit"))

    def test_a_full_width_dollar_sign_is_caught(self):
        self.assertTrue(money("＄500"))

    def test_words_that_only_contain_a_term_do_not_lock(self):
        for text in ("thanks for the feedback", "my charger died", "see you at 5",
                     "great coffee", "that was priceless"):
            self.assertEqual(money(text), [], text)

    def test_normalizing_is_for_matching_only(self):
        # The message on the wire must stay byte-identical, so check() must not rewrite it.
        text = "hello ​there"
        findings, hits, _stats = dc.check([("b1", text)], "whatsapp")
        self.assertEqual((findings, hits), ([], []))
        self.assertEqual(text, "hello ​there")


class Budget(unittest.TestCase):
    def test_every_channel_the_gate_can_ask_for_has_a_budget(self):
        for name in ("whatsapp", "imessage", "slack-dm", "slack-public", "upwork", "gmail",
                     "instantly"):
            self.assertIn(name, dc.BUDGETS)

    def test_the_cap_itself_is_allowed_and_one_past_it_is_not(self):
        max_bubbles, max_chars = dc.BUDGETS["whatsapp"]
        at_cap = [(f"b{i}", "x") for i in range(max_bubbles)]
        self.assertEqual(dc.check(at_cap, "whatsapp")[0], [])
        self.assertTrue(dc.check(at_cap + [("extra", "x")], "whatsapp")[0])
        self.assertEqual(dc.check([("b", "x" * max_chars)], "whatsapp")[0], [])
        self.assertTrue(dc.check([("b", "x" * (max_chars + 1))], "whatsapp")[0])

    def test_the_char_cap_counts_all_bubbles_together(self):
        max_bubbles, max_chars = dc.BUDGETS["whatsapp"]
        self.assertGreater(max_bubbles, 1)
        half = "x" * (max_chars // 2 + 1)
        self.assertTrue(dc.check([("a", half), ("b", half)], "whatsapp")[0])


class Cli(unittest.TestCase):
    """The iMessage wrapper tells a finding from a crash by exit 1 plus text on stdout."""

    def test_clean_exits_0(self):
        done = run_cli("see you at 5")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_money_exits_1_with_a_report_and_no_traceback(self):
        done = run_cli("the deposit is $500")
        self.assertEqual(done.returncode, 1)
        self.assertIn("MONEY LOCK", done.stdout)
        self.assertNotIn("Traceback", done.stderr)

    def test_over_budget_exits_1_with_a_report(self):
        done = run_cli(*["hi"] * (dc.BUDGETS["imessage"][0] + 1))
        self.assertEqual(done.returncode, 1)
        self.assertTrue(done.stdout.strip())

    def test_an_unknown_channel_and_a_missing_file_are_usage_errors(self):
        self.assertEqual(run_cli("hi", channel="nope").returncode, 2)
        done = subprocess.run(["/usr/bin/python3", "-I", PATH, "/no/such/file"],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 2)


if __name__ == "__main__":
    unittest.main()
