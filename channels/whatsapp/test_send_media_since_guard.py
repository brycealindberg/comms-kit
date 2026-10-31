"""`send-media --since`: the same stale-chat guard `send` has. Network mocked.

Run: python3 -m pytest -q test_send_media_since_guard.py   (from this directory)
"""
import argparse
import contextlib
import io
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

ROOM = "!room:x"
READ_TS = 1790190627000          # ms: the message the caller read


def ev(ts_ms, body="hi"):
    return {"type": "m.room.message", "origin_server_ts": ts_ms,
            "sender": "@whatsapp_lid-1:x", "content": {"body": body}}


class SendMedia(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".png")
        os.write(fd, b"png-bytes")
        os.close(fd)
        self.addCleanup(os.unlink, self.path)
        self.chunk = [ev(READ_TS)]
        self.upload = mock.Mock(return_value={"event_id": "$sent"})

    def run_cmd(self, yes=False, since=None):
        args = argparse.Namespace(identifier="Group", filepath=self.path, caption=None,
                                  yes=yes, since=since)

        def fake_request(method, path, token=None, params=None, body=None):
            if "/messages" in path:
                return {"chunk": self.chunk}
            if "/event/" in path:
                return {"origin_server_ts": READ_TS + 5000}
            raise AssertionError(f"unexpected request {method} {path}")

        chat = {"portal_id": ROOM, "is_group": True, "label": "Group"}
        out = io.StringIO()
        code = None
        with mock.patch.object(W, "_require_session", return_value=("tok", "!mgmt:x")), \
                mock.patch.object(W, "_resolve_chat", return_value=chat), \
                mock.patch.object(W, "_request", side_effect=fake_request), \
                mock.patch.object(W, "_upload_and_send_media", self.upload), \
                contextlib.redirect_stdout(out):
            try:
                W.cmd_send_media(args)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_moved_chat_refuses_with_5_and_uploads_nothing(self):
        self.chunk = [ev(READ_TS + 60000, "3 new"), ev(READ_TS)]
        code, out = self.run_cmd(yes=True, since=READ_TS / 1000)
        self.assertEqual(code, W.STALE_EXIT)
        self.assertIn("NOT SENT: 1 newer message(s)", out)
        self.upload.assert_not_called()

    def test_dry_run_also_shows_the_refusal(self):
        self.chunk = [ev(READ_TS + 60000), ev(READ_TS)]
        code, out = self.run_cmd(yes=False, since=READ_TS / 1000)
        self.assertEqual(code, W.STALE_EXIT)
        self.assertIn("NOT SENT", out)
        self.upload.assert_not_called()

    def test_dry_run_without_since_prints_the_since_value(self):
        code, out = self.run_cmd(yes=False, since=None)
        self.assertIsNone(code)
        self.assertIn(f"--since {READ_TS / 1000:.3f}", out)
        self.upload.assert_not_called()

    def test_yes_without_since_refuses_with_2(self):
        code, out = self.run_cmd(yes=True, since=None)
        self.assertEqual(code, 2)
        self.assertIn("--yes needs --since", out)
        self.upload.assert_not_called()

    def test_empty_chat_refuses_unless_since_empty(self):
        self.chunk = []
        code, _ = self.run_cmd(yes=True, since=READ_TS / 1000)
        self.assertEqual(code, W.STALE_EXIT)
        self.upload.assert_not_called()

    def test_unchanged_chat_sends_and_prints_next_since(self):
        code, out = self.run_cmd(yes=True, since=READ_TS / 1000)
        self.assertIsNone(code)
        self.upload.assert_called_once()
        self.assertIn("Sent. event_id=$sent", out)
        self.assertIn(f"next --since {(READ_TS + 5000) / 1000:.3f}", out)

    def test_cli_accepts_since(self):
        with mock.patch.object(W, "cmd_send_media") as cmd, \
                mock.patch.object(sys, "argv",
                                  ["wa", "send-media", "Group", self.path, "--since", "empty"]):
            W.main()
        self.assertEqual(cmd.call_args[0][0].since, 0.0)


if __name__ == "__main__":
    unittest.main()
