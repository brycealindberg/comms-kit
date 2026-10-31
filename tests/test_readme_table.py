#!/usr/bin/env python3
"""The README's capability table must be what the tool prints, so a new verb cannot leave it stale."""
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ReadmeTable(unittest.TestCase):
    def test_the_readme_table_matches_the_manifests(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as fh:
            text = fh.read()
        start, end = "<!-- capabilities:start -->\n", "<!-- capabilities:end -->"
        self.assertIn(start, text)
        block = text[text.index(start) + len(start):text.index(end)].strip()
        done = subprocess.run([sys.executable, os.path.join(REPO, "tools", "capabilities.py"),
                               "--repo", REPO, "--summary"], capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(block, done.stdout.strip(),
                         "Run tools/capabilities.py --summary and paste the output in the README.")


if __name__ == "__main__":
    unittest.main()
