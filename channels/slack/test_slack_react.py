"""`react`: an emoji reaction on one Slack message. The VPS script call is mocked.

The ways it can go wrong, each pinned below:
  - a raw emoji or a word goes to reactions.add as a name Slack does not know;
  - "already reacted" misses your own reaction because Slack calls 👍 `+1` and the
    caller said `thumbsup`, or Slack added a skin tone (`+1::skin-tone-3`);
  - a malformed ts reaches the VPS (wrong message, or a confusing Slack error);
  - a heart or thumbs up on a price or a deadline goes out unseen (it reads as a yes);
  - the dry run writes anything;
  - a failed add is printed as if it worked.

Run: python3 -m pytest -q test_slack_react.py   (from this folder)
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
import slack_bridge as S  # noqa: E402

SELF = "U0SELF"
TS = "1790190627.123456"


def _use_a_stub_detector_if_the_real_one_is_missing(test):
    """The money terms live in the WhatsApp channel's draft_check.py. Where that file is not
    beside this one (a bare checkout), a minimal `$amount` detector stands in so the lock
    logic under test still runs. With the real module present, the real one is used."""
    if S._money_hits("it costs $5") is None:
        stub = mock.patch.object(S, "_money_hits",
                                 side_effect=lambda t: re.findall(r"\$\s?\d[\d,.]*", str(t)))
        stub.start()
        test.addCleanup(stub.stop)


class ReactionName(unittest.TestCase):
    def test_aliases_and_raw_emojis(self):
        for value in ("heart", "love", "❤️", "❤"):
            self.assertEqual(S._slack_reaction_name(value), "heart", value)
        for value in ("thumbsup", "like", "+1", "👍", "👍🏼", "👍️"):
            self.assertEqual(S._slack_reaction_name(value), "+1", value)
        self.assertEqual(S._slack_reaction_name("😂"), "joy")
        self.assertEqual(S._slack_reaction_name("pray"), "pray")
        self.assertEqual(S._slack_reaction_name("eyes"), "eyes")

    def test_colon_names_pass_through(self):
        self.assertEqual(S._slack_reaction_name(":white_check_mark:"), "white_check_mark")
        self.assertEqual(S._slack_reaction_name(":party-parrot:"), "party-parrot")

    def test_words_two_emojis_and_unknown_raw_emoji_refused(self):
        for bad in ("", "great job", "❤️👍", "🦄", ":bad name:", "great"):
            with self.assertRaises(ValueError, msg=bad):
                S._slack_reaction_name(bad)


class NormName(unittest.TestCase):
    def test_slack_spellings_of_the_same_reaction(self):
        self.assertEqual(S._norm_slack_name("thumbsup"), S._norm_slack_name("+1"))
        self.assertEqual(S._norm_slack_name("+1::skin-tone-3"), "+1")
        self.assertNotEqual(S._norm_slack_name("heart"), S._norm_slack_name("+1"))


class CmdReact(unittest.TestCase):
    def setUp(self):
        _use_a_stub_detector_if_the_real_one_is_missing(self)
        os.environ.pop("OUTBOUND_GUARDS", None)
        self.text = "Thanks so much!"
        self.reactions = []
        self.add_result = {"ok": True, "mode": "add"}
        self.modes = []

    def fake_call(self, channel, workspace, ts, name, mode):
        self.modes.append((mode, name))
        if mode == "check":
            return {"ok": True, "self": SELF, "channel": channel,
                    "message": {"ts": ts, "user": "U0THEM", "name": "Sam Roe", "text": self.text},
                    "reactions": self.reactions}
        return dict(self.add_result, name=name)

    def run_cmd(self, emoji="heart", ts=TS, yes=False, remove=False, money_ok=False):
        args = argparse.Namespace(channel="D0TESTDM0001", ts=ts, emoji=emoji, workspace="clientws",
                                  yes=yes, remove=remove, money_ok=money_ok, exclude=None)
        out, code = io.StringIO(), 0
        with mock.patch.object(S, "_react_call", side_effect=self.fake_call), \
             contextlib.redirect_stdout(out):
            try:
                S.run_react(args)
            except SystemExit as e:
                code = e.code
        return out.getvalue(), code

    def writes(self):
        return [m for m in self.modes if m[0] != "check"]

    def test_dry_run_reads_only(self):
        out, code = self.run_cmd()
        self.assertEqual(code, 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("Thanks so much!", out)
        self.assertIn("Sam Roe", out)

    def test_yes_adds_the_slack_name(self):
        out, code = self.run_cmd(emoji="👍", yes=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.writes(), [("add", "+1")])
        self.assertIn("Reacted :+1:", out)

    def test_bad_ts_refused_before_any_call(self):
        out, code = self.run_cmd(ts="14:53", yes=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.modes, [])
        self.assertIn("NOT SENT", out)

    def test_already_reacted_under_another_spelling(self):
        self.reactions = [{"name": "+1::skin-tone-2", "users": [SELF], "count": 1}]
        out, code = self.run_cmd(emoji="thumbsup", yes=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("already reacted", out)

    def test_someone_elses_reaction_does_not_count_as_his(self):
        self.reactions = [{"name": "heart", "users": ["U0OTHER"], "count": 1}]
        out, code = self.run_cmd(yes=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.writes(), [("add", "heart")])

    def test_money_lock(self):
        self.text = "Invoice is attached, it's $1,200 for October"
        out, code = self.run_cmd(yes=True)
        self.assertEqual(code, S.MONEY_LOCK_EXIT)
        self.assertEqual(self.writes(), [])
        self.assertIn("MONEY LOCK", out)
        out, code = self.run_cmd(yes=True, money_ok=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.writes(), [("add", "heart")])

    def test_question_note(self):
        self.text = "Can you look at the dashboard today?"
        out, _code = self.run_cmd()
        self.assertIn("does not answer it", out)

    def test_failed_add_is_not_sent(self):
        self.add_result = {"ok": False, "mode": "add", "error": "invalid_name"}
        out, code = self.run_cmd(yes=True)
        self.assertNotEqual(code, 0)
        self.assertIn("NOT SENT", out)

    def test_remove_only_what_he_has(self):
        out, code = self.run_cmd(emoji="heart", yes=True, remove=True)
        self.assertEqual(self.writes(), [])
        self.assertIn("NOT SENT", out)
        self.reactions = [{"name": "heart", "users": [SELF], "count": 1}]
        out, code = self.run_cmd(emoji=None, yes=True, remove=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.writes(), [("remove", "heart")])


if __name__ == "__main__":
    unittest.main()
