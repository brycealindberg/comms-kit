#!/usr/bin/env python3
"""Ways the scrub check can fail silently, each with a planted positive control."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                    "tools", "scrub_check.py")


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    subprocess.run(["git", "-C", repo, *args], check=True, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class ScrubCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.mkdir(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        self.deny = os.path.join(self.tmp, "deny.txt")
        self.write_deny("# a comment\nacme-secret-client\nre:\\bROOM[0-9]{4}\\b\n")

    def write_deny(self, text):
        with open(self.deny, "w") as fh:
            fh.write(text)

    def rules_for(self, text):
        """The rule ids one planted line breaks. The ids are built from parts, so this test file
        itself never holds a value the rules are meant to catch."""
        self.put("planted.txt", text + "\n")
        done = subprocess.run([sys.executable, TOOL, "--repo", self.repo, "--deny", self.deny,
                               "--allow", os.path.join(self.tmp, "none.txt")],
                              capture_output=True, text=True)
        return done.stdout

    def test_a_real_looking_matrix_room_id_is_caught(self):
        self.assertIn("matrix-room", self.rules_for("room = '!" + "aQ3xT9vLm2Zp7KdY8sWb" + "'"))

    def test_a_fake_room_id_and_code_are_not_room_ids(self):
        out = self.rules_for("room = '!" + "fakeRoom3xT9vLm2Zp7KdY8sWb" + "'; if (!isRecoverableSlackError) {}")
        self.assertNotIn("matrix-room", out)

    def test_a_real_looking_slack_id_is_caught(self):
        for prefix in "CDGTUW":
            self.assertIn("slack-id", self.rules_for("id = " + prefix + "07" + "QWERT9XZ"))

    def test_marked_fake_slack_ids_pass(self):
        for fake in ("C0" + "FAKECH001", "D0" + "TESTDM0001", "U0" + "123456789", "C0" + "FIXTURE1"):
            self.assertNotIn("slack-id", self.rules_for("id = " + fake))

    def test_a_real_looking_whatsapp_group_id_is_caught_and_a_zero_filled_one_passes(self):
        self.assertIn("whatsapp-group", self.rules_for("g = " + "120363" + "423987651204"))
        self.assertNotIn("whatsapp-group", self.rules_for("g = " + "120363" + "000000000001"))

    def put(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def run_tool(self, *extra):
        return subprocess.run(
            [sys.executable, TOOL, "--repo", self.repo, "--deny", self.deny, *extra],
            capture_output=True, text=True)

    def test_clean_repo_passes_and_names_the_file_count(self):
        self.put("a.py", "x = 1\n")
        out = self.run_tool()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertRegex(out.stdout, r"scrub clean: 1 files, 0 hits")

    def test_denied_string_in_an_untracked_file_is_found(self):
        self.put("a.py", "client = 'Acme-Secret-Client'\n")
        out = self.run_tool()
        self.assertEqual(out.returncode, 1)
        self.assertIn("a.py:1", out.stdout)

    def test_regex_deny_rule_is_found(self):
        self.put("a.py", "room = 'ROOM1234'\n")
        self.assertEqual(self.run_tool().returncode, 1)

    def test_output_never_prints_the_matched_text(self):
        self.put("a.py", "client = 'acme-secret-client'\nroom = 'ROOM1234'\n")
        out = self.run_tool()
        self.assertNotIn("acme-secret-client", out.stdout + out.stderr)
        self.assertNotIn("ROOM1234", out.stdout + out.stderr)

    def test_built_in_rules_need_no_deny_entry(self):
        # Built from parts, so the kit's own scrub does not flag this test file.
        for text in ("p = '/Us" "ers/somebody/file'\n",
                     "h = 'box.tail12345.ts" ".net'\n",
                     "ip = '8.8." "4.4'\n",
                     "m = 'real.person@g" "mail.com'\n",
                     "n = '+1415" "5238886'\n",
                     "t = 'xo" "xb-1234567890-abcdef'\n"):
            with self.subTest(text=text):
                self.put("a.py", text)
                self.assertEqual(self.run_tool().returncode, 1, text)

    def test_placeholders_are_allowed(self):
        self.put("a.py", "\n".join([
            "m = 'mike@example.com'", "m2 = 'x@example.invalid'",
            "n = '+1 206 555 0100'", "ip = '127.0.0.1'", "lan = '192.0.2.10'",
            "p = '/Users/you/file'", ""]))
        out = self.run_tool()
        self.assertEqual(out.returncode, 0, out.stdout)

    def test_gitignored_file_is_not_scanned(self):
        self.put(".gitignore", "secret.json\n")
        self.put("secret.json", "acme-secret-client\n")
        self.assertEqual(self.run_tool().returncode, 0)

    def test_history_only_hit_fails_with_the_flag_and_passes_without(self):
        self.put("a.py", "acme-secret-client\n")
        git(self.repo, "add", "a.py")
        git(self.repo, "commit", "-qm", "one")
        self.put("a.py", "clean\n")
        git(self.repo, "add", "a.py")
        git(self.repo, "commit", "-qm", "two")
        self.assertEqual(self.run_tool().returncode, 0)
        out = self.run_tool("--history")
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("acme-secret-client", out.stdout)

    def test_a_decorator_in_a_diff_is_not_an_email_and_a_real_address_still_is(self):
        # git prints an added decorator as "+@module.name", and "+" is legal in an address.
        self.put("a.py", "@contextlib.contextmanager\ndef f():\n    yield\n")
        git(self.repo, "add", "a.py")
        git(self.repo, "commit", "-qm", "decorator")
        self.assertEqual(self.run_tool("--history").returncode, 0)
        self.put("a.py", "owner = 'real.person@g" "mail.com'\n")
        git(self.repo, "add", "a.py")
        git(self.repo, "commit", "-qm", "address")
        git(self.repo, "rm", "-qf", "a.py")
        self.put("b.py", "x = 1\n")
        git(self.repo, "add", "b.py")
        git(self.repo, "commit", "-qm", "clean tree, the address stays in history")
        self.assertEqual(self.run_tool().returncode, 0)
        self.assertEqual(self.run_tool("--history").returncode, 1)

    def test_commit_author_address_is_scanned_in_history(self):
        self.write_deny("private-author@\n")
        self.put("a.py", "x\n")
        git(self.repo, "add", "a.py")
        env = dict(os.environ, GIT_AUTHOR_NAME="n",
                   GIT_AUTHOR_EMAIL="private-author@corp.test",
                   GIT_COMMITTER_NAME="n", GIT_COMMITTER_EMAIL="n@example.com")
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "x"], check=True, env=env)
        self.assertEqual(self.run_tool("--history").returncode, 1)

    def test_missing_deny_file_is_an_error_not_a_pass(self):
        os.remove(self.deny)
        self.put("a.py", "x\n")
        self.assertEqual(self.run_tool().returncode, 2)

    def test_deny_file_with_no_rules_is_an_error_not_a_pass(self):
        self.write_deny("# only a comment\n\n")
        self.put("a.py", "x\n")
        self.assertEqual(self.run_tool().returncode, 2)

    def test_repo_with_no_files_is_an_error_not_a_pass(self):
        self.assertEqual(self.run_tool().returncode, 2)

    def test_allow_rule_covers_only_that_file_and_exact_line(self):
        self.write_deny("acme-secret-client\n")
        self.put("LICENSE", "Copyright acme-secret-client\n")
        self.put("tools/scrub-allow.txt", "LICENSE:Copyright acme-secret-client\n")
        self.assertEqual(self.run_tool().returncode, 1, "the allow file itself holds the string")
        os.remove(os.path.join(self.repo, "tools/scrub-allow.txt"))
        allow = os.path.join(self.tmp, "allow.txt")
        with open(allow, "w") as fh:
            fh.write("LICENSE:Copyright acme-secret-client\n")
        self.assertEqual(self.run_tool("--allow", allow).returncode, 0)
        self.put("OTHER", "Copyright acme-secret-client\n")
        self.assertEqual(self.run_tool("--allow", allow).returncode, 1)

    def test_binary_file_is_scanned(self):
        path = os.path.join(self.repo, "a.bin")
        with open(path, "wb") as fh:
            fh.write(b"\x00\xff acme-secret-client \x00")
        self.assertEqual(self.run_tool().returncode, 1)


if __name__ == "__main__":
    unittest.main()
