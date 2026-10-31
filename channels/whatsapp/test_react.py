"""`react`: a WhatsApp reaction (thumbs up, heart, ...) on one exact message. Network mocked.

The ways it can go wrong, each pinned below:
  - a word or two emojis goes out as the key (WhatsApp shows nothing, or the wrong thing);
  - the auto-pick lands on the owner's own message (phone LID or this tool) or a bot notice;
  - "already reacted" misses the owner's phone reaction because their phone sends `👍️` (VS16);
  - `--yes` reacts to whatever is newest NOW instead of the message he previewed;
  - a heart or thumbs up on a price or a deadline goes out unseen (it reads as a yes);
  - `--remove` tries to redact a reaction his phone made (only this tool's can go);
  - the bridge refuses the reaction and the CLI still says it worked.

Run: python3 -m pytest -q test_react.py   (from this directory)
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
PHONE = "@whatsapp_lid-4440100:x"      # the owner on their phone
TOOL = "@owner:x"                             # the owner through this CLI
THEM = "@whatsapp_lid-231339907412126:x"     # the other person
BOT = "@whatsappbot:x"


def msg(eid, ts, sender=THEM, body="Thanks so much!", msgtype="m.text"):
    return {"type": "m.room.message", "event_id": eid, "origin_server_ts": ts,
            "sender": sender, "room_id": ROOM, "content": {"msgtype": msgtype, "body": body}}


def reaction(eid, target, key, sender):
    return {"type": "m.reaction", "event_id": eid, "sender": sender, "origin_server_ts": 1,
            "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": target, "key": key}}}


class ReactionKey(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(W._reaction_key("heart"), "❤️")
        self.assertEqual(W._reaction_key("love"), "❤️")
        for word in ("thumbsup", "like", "+1", "thumbs-up"):
            self.assertEqual(W._reaction_key(word), "👍")
        self.assertEqual(W._reaction_key("Haha"), "😂")
        self.assertEqual(W._reaction_key("pray"), "🙏")

    def test_one_raw_emoji_passes_unchanged(self):
        for raw in ("👍", "👍🏼", "❤️", "🙏", "🔥", "👨‍👩‍👧", "🇺🇸", "✅"):
            self.assertEqual(W._reaction_key(raw), raw)

    def test_words_and_two_emojis_refused(self):
        for bad in ("", "  ", "great", "ok!", "❤️👍", "👍 👍", "👍a", ":heart:"):
            with self.assertRaises(ValueError, msg=bad):
                W._reaction_key(bad)


class NormKey(unittest.TestCase):
    def test_variation_selector_and_skin_tone_do_not_make_a_new_reaction(self):
        self.assertEqual(W._norm_key("👍️"), W._norm_key("👍"))     # his phone sends VS16
        self.assertEqual(W._norm_key("👍🏼"), W._norm_key("👍"))
        self.assertEqual(W._norm_key("❤"), W._norm_key("❤️"))
        self.assertNotEqual(W._norm_key("❤️"), W._norm_key("👍"))


class PickTarget(unittest.TestCase):
    def test_an_edit_event_is_not_a_message_to_react_to(self):
        edit = msg("$edit", 300, body="* fixed typo")
        edit["content"]["m.relates_to"] = {"rel_type": "m.replace", "event_id": "$orig"}
        chunk = [edit, msg("$orig", 100, body="fixd typo")]
        self.assertEqual(W._react_target(chunk)["event_id"], "$orig")

    def test_newest_message_from_them(self):
        chunk = [msg("$new", 300, body="newest"), msg("$old", 100, body="older")]  # newest first
        self.assertEqual(W._react_target(chunk)["event_id"], "$new")

    def test_skips_owner_both_ways_bots_notices_and_reactions(self):
        chunk = [msg("$tool", 600, sender=TOOL),
                 msg("$phone", 500, sender=PHONE),
                 msg("$bot", 450, sender=BOT, body="⚠️ Your message was not bridged"),
                 msg("$notice", 420, msgtype="m.notice"),
                 reaction("$r", "$x", "❤️", THEM),
                 msg("$redacted", 410) | {"content": {}},
                 msg("$them", 400)]
        self.assertEqual(W._react_target(chunk)["event_id"], "$them")

    def test_self_dm_has_no_target(self):
        # The owner's own self-DM: every message is theirs, so the pick finds nothing
        chunk = [msg("$a", 2, sender=PHONE), msg("$b", 1, sender=TOOL)]
        self.assertIsNone(W._react_target(chunk))


class OwnerReactions(unittest.TestCase):
    def test_finds_his_phone_and_tool_reactions_only(self):
        chunk = [reaction("$1", "$t", "👍️", PHONE), reaction("$2", "$t", "❤️", TOOL),
                 reaction("$3", "$t", "😂", THEM)]
        got = {(r["key"], r["via"]) for r in W._owner_reactions(chunk)}
        self.assertEqual(got, {("👍️", "phone"), ("❤️", "tool")})


class CmdReact(unittest.TestCase):
    """cmd_react end to end with the Matrix calls faked."""

    def setUp(self):
        os.environ.pop("OUTBOUND_GUARDS", None)
        self.room_msgs = [msg("$them", 1790190627000, body="Thanks so much!")]
        self.relations = []
        self.after_send = []          # what the room shows once the reaction is in
        self.calls = []

    def fake_request(self, method, path, token=None, params=None, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and "/relations/" in path:
            return {"chunk": self.relations}
        if method == "GET" and "/event/" in path:
            eid = W.urllib.parse.unquote(path.rsplit("/", 1)[1])
            for e in self.room_msgs + self.after_send:
                if e["event_id"] == eid:
                    return e
            if eid == "$sent":
                return {"event_id": "$sent", "origin_server_ts": 1790190700000}
            raise W.MatrixError(404, f"M_NOT_FOUND {eid}")
        if method == "GET" and "/messages" in path:
            sent = any(m == "PUT" for m, _p, _b in self.calls[:-1])
            return {"chunk": (self.after_send if sent else []) + self.room_msgs}
        if method == "PUT":
            return {"event_id": "$sent"}
        raise AssertionError(f"unexpected {method} {path}")

    def run_cmd(self, emoji="heart", event=None, yes=False, remove=False, money_ok=False):
        args = argparse.Namespace(identifier="Alex Doe", emoji=emoji, event=event, yes=yes,
                                  remove=remove, money_ok=money_ok, group_ok=False, exclude=None)
        chat = {"portal_id": ROOM, "is_group": False, "label": "Alex Doe"}
        out, code = io.StringIO(), 0
        with mock.patch.object(W, "_require_session", return_value=("tok", "!mgmt:x")), \
             mock.patch.object(W, "_resolve_chat", return_value=chat), \
             mock.patch.object(W, "_request", side_effect=self.fake_request), \
             mock.patch.object(W.time, "sleep"), \
             contextlib.redirect_stdout(out):
            try:
                W.cmd_react(args)
            except SystemExit as e:
                code = e.code
        return out.getvalue(), code

    def puts(self):
        return [(p, b) for m, p, b in self.calls if m == "PUT"]

    def test_dry_run_shows_the_message_and_the_pin_and_sends_nothing(self):
        out, code = self.run_cmd()
        self.assertEqual(code, 0)
        self.assertEqual(self.puts(), [])
        self.assertIn("Thanks so much!", out)
        self.assertIn("--event $them", out)

    def test_yes_without_event_refuses(self):
        out, code = self.run_cmd(yes=True)
        self.assertEqual(code, 2)
        self.assertEqual(self.puts(), [])
        self.assertIn("NOT SENT", out)

    def test_yes_with_event_sends_one_annotation(self):
        out, code = self.run_cmd(event="$them", yes=True)
        self.assertEqual(code, 0, out)
        [(path, body)] = self.puts()
        self.assertIn("/send/m.reaction/", path)
        self.assertEqual(body, {"m.relates_to": {"rel_type": "m.annotation",
                                                 "event_id": "$them", "key": "❤️"}})
        self.assertIn("Reacted ❤️", out)

    def test_event_pin_wins_over_a_newer_message(self):
        self.room_msgs.insert(0, msg("$newer", 1790190800000, body="Actually one more thing?"))
        out, code = self.run_cmd(event="$them", yes=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.puts()[0][1]["m.relates_to"]["event_id"], "$them")
        self.assertIn("1 newer message", out)          # told, not silently ignored

    def test_unknown_event_refuses(self):
        out, code = self.run_cmd(event="$nope", yes=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.puts(), [])
        self.assertIn("NOT SENT", out)

    def test_already_reacted_from_phone_sends_nothing(self):
        self.relations = [reaction("$r1", "$them", "❤", PHONE)]   # no VS16 from the phone
        out, code = self.run_cmd(event="$them", yes=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.puts(), [])
        self.assertIn("already reacted", out)

    def test_a_different_reaction_is_replaced_and_says_so(self):
        self.relations = [reaction("$r1", "$them", "👍️", PHONE)]
        out, code = self.run_cmd(event="$them", yes=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.puts()), 1)
        self.assertIn("replaces your 👍", out)

    def test_money_lock_on_the_target_message(self):
        self.room_msgs = [msg("$them", 1790190627000, body="Ok so it's $500 per month, deal?")]
        out, code = self.run_cmd(event="$them", yes=True)
        self.assertEqual(code, W.MONEY_LOCK_EXIT)
        self.assertEqual(self.puts(), [])
        self.assertIn("MONEY LOCK", out)
        out, code = self.run_cmd(event="$them", yes=True, money_ok=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.puts()), 1)

    def test_a_question_gets_a_note(self):
        self.room_msgs = [msg("$them", 1790190627000, body="Can you send the file today?")]
        out, _code = self.run_cmd()
        self.assertIn("does not answer it", out)

    def test_bridge_refusal_is_not_reported_as_sent(self):
        self.after_send = [msg("$botnote", 1790190701000, sender=BOT,
                               body="⚠️ Your message was not bridged: unsupported", msgtype="m.notice")]
        out, code = self.run_cmd(event="$them", yes=True)
        self.assertEqual(code, 4)
        self.assertIn("NOT SENT", out)

    def test_word_emoji_refused_before_any_call(self):
        out, code = self.run_cmd(emoji="great")
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, [])
        self.assertIn("NOT SENT", out)

    def test_remove_redacts_only_this_tools_reaction(self):
        self.relations = [reaction("$mine", "$them", "❤️", TOOL)]
        out, code = self.run_cmd(emoji=None, event="$them", yes=True, remove=True)
        self.assertEqual(code, 0, out)
        [(path, _body)] = self.puts()
        self.assertIn("/redact/%24mine/", path)

    def test_remove_a_phone_reaction_refuses(self):
        self.relations = [reaction("$ph", "$them", "❤️", PHONE)]
        out, code = self.run_cmd(emoji=None, event="$them", yes=True, remove=True)
        self.assertEqual(code, 1)
        self.assertEqual(self.puts(), [])
        self.assertIn("from your phone", out)


class HistoryIds(unittest.TestCase):
    def test_default_lines_are_unchanged_and_ids_are_opt_in(self):
        chunk = [msg("$e1", 1790190627000, body="hello")]
        chat = {"portal_id": ROOM, "is_group": False, "label": "Alex Doe"}

        def run(ids):
            args = argparse.Namespace(identifier="Alex Doe", number=5, no_transcribe=True, ids=ids)
            out = io.StringIO()
            with mock.patch.object(W, "_require_session", return_value=("tok", "!mgmt:x")), \
                 mock.patch.object(W, "_resolve_chat", return_value=chat), \
                 mock.patch.object(W, "_request", return_value={"chunk": chunk}), \
                 contextlib.redirect_stdout(out):
                W.cmd_history(args)
            return out.getvalue()

        plain = run(False)
        self.assertNotIn("$e1", plain)
        self.assertTrue(plain.rstrip().endswith("hello"))
        self.assertIn("[$e1]", run(True))


class MoneyCheckLocation(unittest.TestCase):
    """A mirror copy in another repo has no draft_check.py next to it. It must
    still find the skill's checker, and lock only when no checker exists anywhere."""

    def _copy_without_checker(self, tmp):
        import importlib.util
        import shutil
        shutil.copy(W.__file__, os.path.join(tmp, "wa_copy.py"))
        spec = importlib.util.spec_from_file_location("wa_copy", os.path.join(tmp, "wa_copy.py"))
        mod = importlib.util.module_from_spec(spec)
        # The copy sits outside the kit, so it needs the core folder named.
        with mock.patch.dict(os.environ, {"COMMS_KIT_CORE": W._core_dir()}):
            spec.loader.exec_module(mod)
        return mod

    @unittest.skipUnless(os.path.isfile(os.path.join(os.path.dirname(W.__file__), "draft_check.py")),
                         "draft_check.py is private to the owner's machine, not in the kit")
    def test_copy_finds_the_skill_checker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            mod = self._copy_without_checker(tmp)
            skill = os.path.dirname(os.path.abspath(W.__file__))   # holds draft_check.py
            with mock.patch.dict(os.environ, {"WHATSAPP_SKILL_DIR": skill}):
                self.assertEqual(mod._money_hits("Thanks so much!"), [])
                self.assertTrue(mod._money_hits("The deposit is $500"))

    def test_no_checker_anywhere_locks(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as empty:
            mod = self._copy_without_checker(tmp)
            with mock.patch.dict(os.environ, {"WHATSAPP_SKILL_DIR": empty}):
                self.assertIsNone(mod._money_hits("Thanks so much!"))


if __name__ == "__main__":
    unittest.main()
