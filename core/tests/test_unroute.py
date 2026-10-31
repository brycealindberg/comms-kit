#!/usr/bin/env python3
"""Tests for `comms unroute` -- the inbox's People and channels removes a person or chat from a
client; the relay's apply_overlay.py then runs `comms unroute KEY --from CLIENT` here (2026-10-07).

Runs the real CLI against a throwaway HOME. Stdlib only. Run:

    python3 test_unroute.py
"""
import json
import os
import subprocess
import tempfile
import unittest

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # kit: code path
COMMS = os.path.join(CODE, "bin", "comms")
LIB = os.path.join(CODE, "commslib.py")

ROUTES = {
    "ann@x.example.com": {"project": "acme_dental", "path": None, "label": "Ann", "aliases": ["t1"]},
    "12065550177": {"project": "acme_co", "path": None, "label": "acme_co", "aliases": []},
}


class Unroute(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.root = os.path.join(self.home, ".claude", "communications")
        os.makedirs(self.root)
        self.config = os.path.join(self.home, "kit-config.json")
        with open(self.config, "w") as fh:
            json.dump({"data_dir": self.root}, fh)
        os.symlink(LIB, os.path.join(self.root, "commslib.py"))
        self.path = os.path.join(self.root, "routes.json")
        with open(self.path, "w") as fh:
            json.dump(ROUTES, fh)
        with open(os.path.join(self.root, "threads.json"), "w") as fh:
            json.dump({}, fh)

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        env = dict(os.environ, HOME=self.home, COMMS_KIT_CONFIG=self.config)
        return subprocess.run(["python3", COMMS, "unroute", *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def routes(self):
        with open(self.path) as fh:
            return json.load(fh)

    def test_removes_the_key_with_its_aliases_and_keeps_the_rest(self):
        p = self.run_cli("Ann@X.example.com", "--from", "acme_dental")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(set(self.routes()), {"12065550177"})
        self.assertIn("unrouted: ann@x.example.com (was acme_dental)", p.stdout)

    def test_a_key_routed_to_another_client_is_left_alone(self):
        p = self.run_cli("ann@x.example.com", "--from", "acme_co")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.routes(), ROUTES)

    def test_a_key_that_is_not_routed_is_not_an_error(self):
        p = self.run_cli("bob@x.example.com")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.routes(), ROUTES)
        self.assertIn("not routed", p.stdout)

    def test_a_corrupt_routes_file_is_never_rewritten(self):
        with open(self.path, "w") as fh:
            fh.write("{torn")
        p = self.run_cli("ann@x.example.com")
        self.assertEqual(p.returncode, 1)
        with open(self.path) as fh:
            self.assertEqual(fh.read(), "{torn")


if __name__ == "__main__":
    unittest.main()
