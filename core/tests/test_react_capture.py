"""`react` on WhatsApp and Slack is a send: it answers a message the way a text does,
so /inbox must see the thread as answered. These pin how the capture hook parses it.

Ways it goes wrong: the verb is unclassified (never archived, and `comms doctor`
fails); a dry run is archived as a sent reaction; `--remove` or `--money-ok` eats the
emoji as its value; the target is the emoji instead of the chat.

Run: python3 -m pytest -q test_react_capture.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import commslib as C  # noqa: E402

WA = "python3 /tmp/skills/whatsapp/whatsapp_mautrix.py"
SL = "python3 /tmp/skills/slack-bridge/slack_bridge.py"


class WhatsAppReact(unittest.TestCase):
    def test_classified_as_send(self):
        self.assertEqual(C.VERBS["whatsapp"].get("react"), "send")

    def test_yes_is_a_send_with_the_chat_as_target(self):
        info = C.parse("whatsapp", f"{WA} react '!abc:x' heart --event '$e1' --yes")
        self.assertEqual(info["verb"], "react")
        self.assertEqual(info["target"], "!abc:x")
        self.assertEqual(info["text"], "[reaction heart]")
        self.assertFalse(info["dry_run"])

    def test_preview_is_a_dry_run(self):
        self.assertTrue(C.parse("whatsapp", f"{WA} react 12065550123 heart")["dry_run"])

    def test_boolean_flags_do_not_eat_the_emoji(self):
        info = C.parse("whatsapp", f"{WA} react 12065550123 --money-ok 👍 --event '$e' --yes")
        self.assertEqual(info["text"], "[reaction 👍]")

    def test_remove(self):
        info = C.parse("whatsapp", f"{WA} react 12065550123 --remove --event '$e' --yes")
        self.assertEqual(info["target"], "12065550123")
        self.assertEqual(info["text"], "[reaction removed]")

    def test_history_ids_does_not_eat_the_chat(self):
        info = C.parse("whatsapp", f"{WA} history --ids 12065550123")
        self.assertEqual(info["target"], "12065550123")


class SlackReact(unittest.TestCase):
    def test_classified_as_send(self):
        self.assertEqual(C.VERBS["slack"].get("react"), "send")

    def test_yes_is_a_send_with_the_channel_as_target(self):
        info = C.parse("slack", f"{SL} react D0FAKEDM002 1700000000.000100 thumbsup "
                                "--workspace acmeworkspace --yes")
        self.assertEqual(info["target"], "D0FAKEDM002")
        self.assertEqual(info["text"], "[reaction thumbsup]")
        self.assertFalse(info["dry_run"])

    def test_preview_is_a_dry_run(self):
        info = C.parse("slack", f"{SL} react D0FAKEDM002 1700000000.000100 heart")
        self.assertTrue(info["dry_run"])

    def test_remove(self):
        info = C.parse("slack", f"{SL} react D0FAKEDM002 1700000000.000100 --remove --yes")
        self.assertEqual(info["text"], "[reaction removed]")


if __name__ == "__main__":
    unittest.main()
