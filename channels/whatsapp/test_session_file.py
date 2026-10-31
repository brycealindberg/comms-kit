"""The cached Matrix token must never sit in a file that other users can read.

The failures that matter: a new file takes its mode from the umask, an older file keeps a loose
mode, and a chmod after the write leaves the token readable for a moment.

Run: python3 -m unittest test_session_file   (from this directory)
"""
import atexit
import json
import os
import stat
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Hermetic config: fake values only, never the real kit config.
_CFG_FD, _CFG_PATH = tempfile.mkstemp(suffix=".json")
with os.fdopen(_CFG_FD, "w") as _cfg_file:
    json.dump({"whatsapp": {"matrix_user": "owner", "owner_lid": "4440100",
                            "self_number": "12065550100", "bot_user": "@whatsappbot:x",
                            "homeserver": "https://matrix.example.com"}}, _cfg_file)
atexit.register(os.unlink, _CFG_PATH)
os.environ["COMMS_KIT_CONFIG"] = _CFG_PATH
import whatsapp_mautrix as W  # noqa: E402


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class SessionFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "new-folder", "session.json")
        old = os.umask(0)
        self.addCleanup(os.umask, old)

    def test_a_new_file_is_private_under_an_open_umask(self):
        W._write_session(self.path, {"token": "t"})
        self.assertEqual(mode_of(self.path), 0o600)
        with open(self.path) as fh:
            self.assertEqual(json.load(fh), {"token": "t"})

    def test_an_older_loose_file_is_private_before_the_token_is_written(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as fh:
            fh.write('{"token": "a much longer old token that must be gone"}')
        os.chmod(self.path, 0o644)
        seen = []
        real_dump = json.dump

        def spy(data, fh, **kw):
            seen.append(mode_of(self.path))
            return real_dump(data, fh, **kw)

        with mock.patch.object(W.json, "dump", spy):
            W._write_session(self.path, {"token": "t"})
        self.assertEqual(seen, [0o600])
        with open(self.path) as fh:
            self.assertEqual(json.load(fh), {"token": "t"})


if __name__ == "__main__":
    unittest.main()
