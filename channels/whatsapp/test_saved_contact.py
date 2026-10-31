"""Saved contacts (`comms contact add`) in WhatsApp name resolution.

The failures that matter: a saved number that is not digits reaches start-chat; a broken directory
stops a read that used to work; a saved contact loses to a substring hit on some other room.

Run: python3 -m unittest test_saved_contact   (from this directory)
"""
import json
import os
import subprocess
import sys
import unittest
import unittest.mock as mock

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


def done(rc, out=""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr="")


class SavedNumber(unittest.TestCase):
    def test_a_saved_contact_gives_digits(self):
        with mock.patch.object(W.subprocess, "run", return_value=done(0, "12065550100\n")):
            self.assertEqual(W._saved_contact_number("Ann Lee"), "12065550100")

    def test_not_saved_is_none(self):
        with mock.patch.object(W.subprocess, "run", return_value=done(1)):
            self.assertIsNone(W._saved_contact_number("Ann Lee"))

    def test_output_that_is_not_a_phone_number_is_none(self):
        with mock.patch.object(W.subprocess, "run", return_value=done(0, "!room:x\n")):
            self.assertIsNone(W._saved_contact_number("Ann Lee"))

    def test_a_missing_or_hung_directory_never_stops_the_old_lookup(self):
        for boom in (FileNotFoundError(), subprocess.TimeoutExpired("comms", 5)):
            with mock.patch.object(W.subprocess, "run", side_effect=boom):
                self.assertIsNone(W._saved_contact_number("Ann Lee"))


class SavedSearch(unittest.TestCase):
    def test_rows_with_a_number_are_returned(self):
        rows = [{"name": "Ann Lee", "channels": {"whatsapp": "12065550100"}},
                {"name": "Slack Only", "channels": {"slack": "D1"}}]
        with mock.patch.object(W.subprocess, "run", return_value=done(0, json.dumps(rows))):
            self.assertEqual(W._saved_contacts("a"), [("12065550100", "Ann Lee")])

    def test_garbage_is_empty(self):
        with mock.patch.object(W.subprocess, "run", return_value=done(0, "not json")):
            self.assertEqual(W._saved_contacts("a"), [])


class ResolveChat(unittest.TestCase):
    def resolve(self, saved):
        with mock.patch.object(W, "_self_number", return_value="1"), \
             mock.patch.object(W, "_saved_contact_number", return_value=saved) as lookup, \
             mock.patch.object(W, "_resolve_portal", return_value="!dm:x") as portal, \
             mock.patch.object(W, "_joined_portals", return_value=[("!group:x", "Ann Lee and Co")]) as joined, \
             mock.patch.object(W, "_join_portal", return_value="!group:x"), \
             mock.patch.object(W, "_is_group", return_value=True), \
             mock.patch.object(W, "_group_label", return_value="Ann Lee and Co"):
            return W._resolve_chat("t", "!m:x", "Ann Lee"), lookup, portal, joined

    def test_a_saved_exact_name_beats_a_substring_room_match(self):
        chat, _, portal, joined = self.resolve("12065550100")
        portal.assert_called_once_with("t", "!m:x", "12065550100")
        joined.assert_not_called()
        self.assertFalse(chat["is_group"])
        self.assertEqual(chat["label"], "12065550100 (Ann Lee)")

    def test_no_saved_contact_keeps_the_old_flow(self):
        chat, _, portal, joined = self.resolve(None)
        joined.assert_called_once()
        portal.assert_not_called()
        self.assertTrue(chat["is_group"])

    def test_a_number_never_reads_the_directory(self):
        with mock.patch.object(W, "_self_number", return_value="1"), \
             mock.patch.object(W, "_saved_contact_number") as lookup, \
             mock.patch.object(W, "_resolve_portal", return_value="!dm:x"):
            W._resolve_chat("t", "!m:x", "12065550100")
        lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
