#!/usr/bin/env python3
"""Tests for `comms unrouted` — the list the SessionStart comms note points to.

That note no longer prints the per-contact list (2026-09-28); it tells the
reader to run `comms unrouted` for each contact's folder. So the folder the
contact was seen from (UNROUTED.md, "Seen from" column) must be in the output.

Runs the real CLI against a throwaway HOME. Stdlib only. Run:

    python3 test_unrouted.py
"""
import json
import os
import subprocess
import tempfile
import unittest

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # kit: code path
COMMS = os.path.join(CODE, "bin", "comms")
LIB = os.path.join(CODE, "commslib.py")

HEADER = ("# Unrouted contacts\n\nNo project assigned yet. Assign one with:\n\n"
          "    comms route <target> <project> --path /abs/repo/path\n\n"
          "| Target | Channel | Seen from |\n| --- | --- | --- |\n")


def thread(key, channel="gmail", count=3, ts=100, et="2026-09-28 09:00"):
    return {"target": key, "channel": channel, "count": count,
            "last_ts": ts, "last_et": et, "first_et": et,
            "project": None, "label": None}


class Unrouted(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.root = os.path.join(self.home, ".claude", "communications")
        os.makedirs(self.root)
        self.config = os.path.join(self.home, "kit-config.json")
        with open(self.config, "w") as fh:
            json.dump({"data_dir": self.root}, fh)
        os.symlink(LIB, os.path.join(self.root, "commslib.py"))

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, threads, routes=None, note=None):
        with open(os.path.join(self.root, "threads.json"), "w") as fh:
            json.dump(threads, fh)
        with open(os.path.join(self.root, "routes.json"), "w") as fh:
            json.dump(routes or {}, fh)
        if note is not None:
            with open(os.path.join(self.root, "UNROUTED.md"), "w") as fh:
                fh.write(note)

    def run_cli(self):
        env = dict(os.environ, HOME=self.home, COMMS_KIT_CONFIG=self.config)
        p = subprocess.run(["python3", COMMS, "unrouted"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def line_for(self, out, key):
        rows = [ln for ln in out.splitlines() if ln.startswith(key)]
        self.assertEqual(len(rows), 1, out)
        return rows[0]

    def test_folder_from_note_is_shown(self):
        self.write({"a@x.example.com": thread("a@x.example.com")},
                   note=HEADER + "| `a@x.example.com` | gmail | /Users/you/Desktop/projects/acme app |\n")
        out = self.run_cli()
        self.assertIn("SEEN FROM", out.splitlines()[0])
        self.assertTrue(self.line_for(out, "a@x.example.com").endswith(
            "/Users/you/Desktop/projects/acme app"), out)

    def test_contact_without_a_row_still_listed(self):
        self.write({"a@x.example.com": thread("a@x.example.com"),
                    "b@x.example.com": thread("b@x.example.com", ts=50)},
                   note=HEADER + "| `a@x.example.com` | gmail | /tmp/a |\n")
        out = self.run_cli()
        b = self.line_for(out, "b@x.example.com")
        self.assertNotIn("/tmp/a", b)
        self.assertTrue(self.line_for(out, "a@x.example.com").endswith("/tmp/a"))

    def test_missing_note_file_is_not_an_error(self):
        self.write({"a@x.example.com": thread("a@x.example.com")})
        out = self.run_cli()
        self.line_for(out, "a@x.example.com")

    def test_routed_contact_is_hidden_even_with_a_row(self):
        self.write({"a@x.example.com": thread("a@x.example.com"),
                    "b@x.example.com": dict(thread("b@x.example.com"), project="acme")},
                   note=HEADER + "| `a@x.example.com` | gmail | /tmp/a |\n"
                                 "| `b@x.example.com` | gmail | /tmp/b |\n")
        out = self.run_cli()
        self.assertNotIn("b@x.example.com", out)
        self.assertNotIn("/tmp/b", out)

    def test_row_with_empty_folder_shows_nothing(self):
        self.write({"a@x.example.com": thread("a@x.example.com")},
                   note=HEADER + "| `a@x.example.com` | gmail |  |\n")
        out = self.run_cli()
        self.assertTrue(self.line_for(out, "a@x.example.com").rstrip().endswith(
            "2026-09-28 09:00"), out)

    def test_nothing_unrouted(self):
        self.write({"b@x.example.com": dict(thread("b@x.example.com"), project="acme")})
        self.assertEqual(self.run_cli().strip(), "nothing unrouted")


if __name__ == "__main__":
    unittest.main()
