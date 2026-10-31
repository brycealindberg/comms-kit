"""Kit-level safety checks for the iMessage channel. Fake runtime only: nothing here sends a real
message or reads the real Messages database.

Ways this channel can fail, and the test for each:
  1. A legacy or unknown command reaches the backend (the shim must refuse it first, exit 64,
     and the backend must never start).
  2. A swapped or tampered upstream binary runs (the pinned SHA-256 must stop it BEFORE exec).
  3. A send is reported as delivered when the message never reached the chat (the result must
     be read back from the chat; a lost row is "uncertain", never "sent_and_verified").
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SHIM = HERE / "imessage"
WRAPPER = HERE / "imessage.py"

SPEC = importlib.util.spec_from_file_location("imessage_kit_under_test", WRAPPER)
assert SPEC and SPEC.loader
imessage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(imessage)

BACKEND_STUB = """#!/usr/bin/env python3
import os, sys
open(os.environ["STUB_MARKER"], "w").write(" ".join(sys.argv[1:]))
"""


class ShimAllowListTests(unittest.TestCase):
    """The shell shim is the command allow-list. The backend beside it is a stub that leaves a
    marker file if it ever starts, so 'nothing was run' is proved, not assumed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.shim = root / "imessage"
        shutil.copy(SHIM, self.shim)
        self.shim.chmod(0o755)
        (root / "imessage.py").write_text(BACKEND_STUB)
        self.marker = root / "backend-started"
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "STUB_MARKER": str(self.marker)}

    def run_shim(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/sh", str(self.shim), *args], env=self.env,
                              capture_output=True, text=True, timeout=20, check=False)

    def test_legacy_and_unknown_commands_are_refused_before_the_backend_starts(self) -> None:
        legacy = (
            ["watch"], ["react"], ["rpc"], ["launch"], ["send-rich"], ["--unknown-flag"],
            # The old upstream shape: recipient and body on the command line (visible to `ps`).
            ["send", "+12065550101", "hello there"],
            ["send", "alex@example.com", "hello"],
        )
        for argv in legacy:
            with self.subTest(argv=argv):
                result = self.run_shim(*argv)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertIn("nothing was run", result.stderr)
                self.assertFalse(self.marker.exists(), "the backend started for a refused command")

    def test_every_allowed_command_reaches_the_backend_with_its_arguments_intact(self) -> None:
        allowed = (
            ["doctor"], ["chats", "--limit", "5"], ["history", "--chat-id", "5"],
            ["send", "--chat-id", "5", "--text-file", "/tmp/a b.txt"],
            ["send", "--text-file", "/tmp/x.txt", "--chat-id", "5"],  # flag order is free
            ["prepare-send", "--chat-id", "5", "--text-file", "/tmp/x.txt"],
        )
        for argv in allowed:
            with self.subTest(argv=argv):
                self.marker.unlink(missing_ok=True)
                # The stub is not a real python3 -I target for the system python, so run it the
                # same way the shim does and check only that it was reached with the arguments.
                result = self.run_shim(*argv)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.marker.read_text(), " ".join(argv))


class PinnedBinaryTests(unittest.TestCase):
    """The wrapper may run only the exact upstream build it pins."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.marker = self.root / "binary-ran"
        self.binary = self.root / "imsg"
        self.binary.write_text(
            "#!/bin/sh\n"
            f"echo ran > '{self.marker}'\n"
            f"echo {imessage.UPSTREAM_VERSION}\n")
        self.binary.chmod(0o755)
        config = self.root / "config.json"
        config.write_text(json.dumps({
            "data_dir": str(self.root / "data"),
            "imessage": {"binary_path": str(self.binary)},
        }))
        patcher = mock.patch.dict(os.environ, {"COMMS_KIT_CONFIG": str(config)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_binary_location_comes_from_config(self) -> None:
        self.assertEqual(imessage.upstream_binary(), self.binary)

    def test_a_binary_whose_hash_is_not_the_pinned_one_is_never_executed(self) -> None:
        # The file reports the right version and is owner-only writable, so only the hash
        # can stop it.
        self.assertNotEqual(hashlib.sha256(self.binary.read_bytes()).hexdigest(),
                            imessage.UPSTREAM_BINARY_SHA256)
        with self.assertRaisesRegex(imessage.ImsgError, "checksum mismatch"):
            imessage.run_imsg(["chats", "--limit", "1"])
        self.assertFalse(self.marker.exists(), "an unpinned binary was executed")

    def test_a_group_writable_binary_is_refused_even_with_the_right_hash(self) -> None:
        self.binary.chmod(0o775)
        pinned = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        with mock.patch.object(imessage, "UPSTREAM_BINARY_SHA256", pinned):
            with self.assertRaisesRegex(imessage.ImsgError, "unsafe ownership or write"):
                imessage.validate_upstream_binary()
        self.assertFalse(self.marker.exists())

    def test_a_missing_binary_is_reported_not_guessed(self) -> None:
        self.binary.unlink()
        with self.assertRaisesRegex(imessage.ImsgError, "unavailable"):
            imessage.validate_upstream_binary()


class SendVerificationTests(unittest.TestCase):
    """A send counts as delivered only when its message is found in the chat afterwards."""

    def setUp(self) -> None:
        from test_imessage import FAKE_IMSG  # the fake runtime used by the full suite

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake = self.root / "fake-imsg"
        self.fake.write_text(FAKE_IMSG)
        self.fake.chmod(0o755)
        self.state = self.root / "fake-state.json"
        config = self.root / "config.json"
        config.write_text(json.dumps({"data_dir": str(self.root / "data")}))
        for patcher in (
            mock.patch.object(imessage, "upstream_binary", return_value=self.fake),
            mock.patch.object(imessage, "state_root", return_value=self.root / "state"),
            mock.patch.object(imessage, "UPSTREAM_BINARY_SHA256",
                              hashlib.sha256(self.fake.read_bytes()).hexdigest()),
            mock.patch.object(imessage, "run_draft_check", return_value=("clean", "ok")),
            mock.patch.dict(os.environ, {"FAKE_IMSG_STATE": str(self.state),
                                         "COMMS_KIT_CONFIG": str(config)}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def send(self, text: str) -> dict:
        args = imessage.build_parser().parse_args(["send", "--chat-id", "5", "--text", text])
        value, ok = imessage.handle(args)
        self.assertTrue(ok)
        return value

    def test_a_delivered_message_is_read_back_from_the_chat(self) -> None:
        result = self.send("Lunch at noon?")
        self.assertEqual(result["status"], "sent_and_verified")
        sent = json.loads(self.state.read_text())["sends"]
        self.assertEqual([s["request"]["params"]["text"] for s in sent], ["Lunch at noon?"])
        self.assertEqual(len(result["sent"]), 1)
        self.assertTrue(result["sent"][0]["message_id"], "the verified row id is reported")

    def test_a_send_the_chat_never_shows_is_never_reported_as_verified(self) -> None:
        # The runtime accepts the send and answers ok, but no row ever appears in the chat.
        with mock.patch.dict(os.environ, {"FAKE_IMSG_MODE": "lost-row"}):
            with mock.patch.object(imessage.time, "sleep"):
                with self.assertRaises(imessage.ImsgError) as caught:
                    self.send("This one gets lost")
        self.assertNotIn("sent_and_verified", str(caught.exception))
        self.assertIn("UNCERTAIN SEND", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
