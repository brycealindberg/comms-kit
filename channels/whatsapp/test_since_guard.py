"""`send --since`: the pure parts of the live pre-send re-read. No network.

Run: python3 test_since_guard.py   (from this directory)
"""
import argparse
import os
import sys
import unittest

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


def ev(ts_ms, sender="@whatsapp_lid-1:x", kind="m.room.message", body="hi"):
    return {"type": kind, "origin_server_ts": ts_ms, "sender": sender,
            "content": {"body": body}}


class NewerThan(unittest.TestCase):
    def test_the_message_you_read_is_not_newer(self):
        self.assertEqual(W._newer_than([ev(1790190627000)], 1790190627.0), [])

    def test_a_later_message_is_newer_whoever_sent_it(self):
        events = [ev(1790190700000, "@whatsapp_lid-4440100:x"),   # the owner's phone
                  ev(1790190800000, "@owner:x"),                          # another session
                  ev(1790190900000, "@whatsapp_lid-231339907412126:x")]  # them
        self.assertEqual(len(W._newer_than(events, 1790190627.0)), 3)

    def test_within_tolerance_is_not_newer(self):
        # the KB stores whole seconds; Matrix sends from the tool carry ms
        self.assertEqual(W._newer_than([ev(1790190627900)], 1790190627.0), [])

    def test_only_messages_count(self):
        self.assertEqual(W._newer_than([ev(1790190900000, kind="m.reaction")], 1790190627.0), [])

    def test_oldest_first(self):
        out = W._newer_than([ev(1790190900000, body="b"), ev(1790190800000, body="a")], 1790190627.0)
        self.assertEqual([e["content"]["body"] for e in out], ["a", "b"])


class ParseSince(unittest.TestCase):
    def test_seconds_and_millis(self):
        self.assertEqual(W._parse_since("1790190627.5"), 1790190627.5)
        self.assertEqual(W._parse_since("1790190627000"), 1790190627.0)

    def test_empty_means_a_chat_with_no_message(self):
        self.assertEqual(W._parse_since("empty"), 0.0)
        # a message there now is newer than "empty"
        self.assertEqual(len(W._newer_than([ev(1790190627000)], 0.0)), 1)

    def test_clock_time_and_zero_refused(self):
        for bad in ("14:53", "2026-09-23 14:53", "0", "-5", ""):
            with self.assertRaises(argparse.ArgumentTypeError):
                W._parse_since(bad)


if __name__ == "__main__":
    unittest.main()
