"""The ElevenLabs request body carries the respelled text; nothing else changes. No network.

Run: python3 test_tts_respell.py   (from this directory)
"""
import json
import os
import sys
import tempfile
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


def sent_body(message):
    seen = {}

    def fake_request(path, api_key, *, body=None, accept=None, timeout=0):
        seen["body"] = json.loads(body)
        raise RuntimeError("stop after the request is built")

    with mock.patch.object(W, "_eleven_credentials", return_value=("key", "voice")), \
         mock.patch.object(W, "_eleven_request", fake_request), \
         tempfile.TemporaryDirectory() as d:
        with _swallow():
            W._generate_eleven_voice_wav(message, d, model="eleven_multilingual_v2",
                                         settings={"stability": 0.5}, label="t")
    return seen["body"]


class _swallow:
    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        return et is RuntimeError


class RequestBody(unittest.TestCase):
    def test_lead_is_respelled_in_the_request(self):
        body = sent_body("Hey, quick note about the lead list and Leads.")
        self.assertEqual(body["text"], "Hey, quick note about the leed list and Leeds.")

    def test_words_containing_lead_are_untouched(self):
        text = "The leader said the misleading numbers were pleaded away."
        self.assertEqual(sent_body(text)["text"], text)

    def test_go_live_sense_only(self):
        self.assertEqual(sent_body("It goes live Monday; the list lives in Airtable.")["text"],
                         "It goes lyve Monday; the list lives in Airtable.")


if __name__ == "__main__":
    unittest.main()
