#!/usr/bin/env python3
"""The verbs added 2026-10-31 and how the archive reads them.

Ways it goes wrong: a reply or edit that reaches a person is never archived; a dry run is
archived as sent; the event id or ts lands in the row's text; a word inside a search or a new
group name is read as a send verb; an edit is written twice in a hook runtime.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import commslib as C  # noqa: E402

WA = "python3 /tmp/skills/whatsapp/whatsapp_mautrix.py"
SL = "python3 /tmp/skills/slack-bridge/slack_bridge.py"


class Classified(unittest.TestCase):
    def test_person_facing_writes_are_sends(self):
        for channel, verb in (("whatsapp", "reply"), ("whatsapp", "edit"), ("slack", "edit")):
            self.assertEqual(C.VERBS[channel].get(verb), "send", (channel, verb))
            self.assertIn((channel, verb), C.DRYRUN_UNLESS_YES)

    def test_admin_verbs_are_deliberately_not_logged(self):
        whatsapp = {"delete", "mark-read", "find", "group-create", "group-add", "group-remove",
                    "group-rename", "group-leave", "group-members", "block", "unblock"}
        slack = {"open-dm", "find", "mentions", "delete", "channel-create", "channel-invite",
                 "channel-remove", "channel-rename", "channel-leave", "channel-archive",
                 "channel-members"}
        self.assertLessEqual(whatsapp, C.IGNORED["whatsapp"])
        self.assertLessEqual(slack, C.IGNORED["slack"])
        self.assertFalse(whatsapp & set(C.VERBS["whatsapp"]))
        self.assertFalse(slack & set(C.VERBS["slack"]))


class Parse(unittest.TestCase):
    def test_whatsapp_reply_row_holds_the_chat_and_the_words_not_the_event(self):
        info = C.parse("whatsapp", f"{WA} reply Alex '$e1' 'On it' --since 170 --yes")
        self.assertEqual((info["verb"], info["target"], info["text"], info["dry_run"]),
                         ("reply", "Alex", "On it", False))

    def test_whatsapp_edit_row_holds_the_new_words(self):
        info = C.parse("whatsapp", f"{WA} edit Alex '$e1' 'New words here' --since 170 --yes")
        self.assertEqual((info["verb"], info["target"], info["text"], info["dry_run"]),
                         ("edit", "Alex", "New words here", False))

    def test_whatsapp_reply_with_flag_before_the_words(self):
        info = C.parse("whatsapp", f"{WA} reply Alex --since 170 '$e1' 'On it' --yes")
        self.assertEqual(info["text"], "On it")

    def test_slack_edit_row_holds_the_channel_and_the_words_not_the_ts(self):
        info = C.parse("slack", f"{SL} edit D123 1790.1 'see you at 4' --workspace w --since 1 --yes")
        self.assertEqual((info["verb"], info["target"], info["text"], info["dry_run"]),
                         ("edit", "D123", "see you at 4", False))

    def test_a_preview_is_a_dry_run(self):
        self.assertTrue(C.parse("whatsapp", f"{WA} reply Alex '$e1' 'On it'")["dry_run"])
        self.assertTrue(C.parse("whatsapp", f"{WA} edit Alex '$e1' 'x'")["dry_run"])
        self.assertTrue(C.parse("slack", f"{SL} edit D1 1.1 'x' --workspace w")["dry_run"])

    def test_a_verb_word_inside_an_ignored_command_is_not_a_send(self):
        for cmd in (f"{WA} find send",
                    f"{WA} group-rename Team send --yes",
                    f"{WA} group-create history --member 12065550100 --yes",
                    f"{SL} find upload --workspace w",
                    f"{SL} channel-rename C1 send --workspace w --yes",
                    f"{SL} channel-create react --workspace w --yes"):
            channel = "whatsapp" if cmd.startswith(WA) else "slack"
            self.assertIsNone(C.parse(channel, cmd)["verb"], cmd)

    def test_old_verbs_still_parse_the_same(self):
        info = C.parse("whatsapp", f"{WA} send Alex 'hello there' --yes")
        self.assertEqual((info["verb"], info["text"], info["dry_run"]), ("send", "hello there", False))
        info = C.parse("slack", f"{SL} send D1 'hello' --workspace w")
        self.assertEqual((info["verb"], info["text"], info["dry_run"]), ("send", "hello", True))


class EditRowCount(unittest.TestCase):
    """An edit has no receipt, so the CLI row and the hook row must not both land."""

    def setUp(self):
        for p in (mock.patch.object(C, "write_rows"), mock.patch.object(C, "remember_receipts")):
            p.start()
            self.addCleanup(p.stop)

    def log(self):
        return C.log_event("slack", "send", "D1", text="x", verb="edit", scope="w:D1")

    def test_the_hook_runtime_leaves_the_row_to_the_hook(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.assertFalse(self.log())
        C.write_rows.assert_not_called()

    def test_a_plain_shell_writes_the_row(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(self.log())
        C.write_rows.assert_called_once()


if __name__ == "__main__":
    unittest.main()
