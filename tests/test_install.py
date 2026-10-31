#!/usr/bin/env python3
"""install.sh under a temp HOME. Each test names a way the installer can hurt a machine."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO, "install.sh")


def tree(root):
    found = []
    for folder, _dirs, files in os.walk(root):
        for name in files:
            found.append(os.path.relpath(os.path.join(folder, name), root))
    return sorted(found)


class Install(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="kit install home ")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.config = os.path.join(self.home, ".config", "comms-kit", "config.json")

    def run_install(self, *args, path=None, cwd=None):
        env = {"HOME": self.home, "PATH": path or os.environ["PATH"]}
        return subprocess.run(["/bin/sh", INSTALL, *args], env=env, cwd=cwd or self.home,
                              capture_output=True, text=True, timeout=120)

    def test_new_config_is_private_and_is_valid_json(self):
        done = self.run_install()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), 0o600)
        with open(self.config, encoding="utf-8") as fh:
            self.assertIsInstance(json.load(fh), dict)

    def test_an_existing_config_is_never_replaced(self):
        os.makedirs(os.path.dirname(self.config))
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write('{"mine": true}\n')
        done = self.run_install()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        with open(self.config, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"mine": true}\n')

    def test_a_foreign_command_is_never_replaced(self):
        bin_dir = os.path.join(self.home, ".local", "bin")
        os.makedirs(bin_dir)
        foreign = os.path.join(bin_dir, "comms")
        with open(foreign, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho someone else\n")
        done = self.run_install()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("not replaced", done.stderr)
        self.assertFalse(os.path.islink(foreign))
        with open(foreign, encoding="utf-8") as fh:
            self.assertIn("someone else", fh.read())

    def test_commands_link_to_this_checkout_and_a_rerun_is_clean(self):
        for _round in (1, 2):
            done = self.run_install()
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        link = os.path.join(self.home, ".local", "bin", "comms")
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(REPO, "core", "bin", "comms")))

    def test_it_writes_nothing_outside_its_three_places(self):
        done = self.run_install()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        allowed = (".config/comms-kit/", ".local/bin/", ".local/share/comms-kit/", "Library/Caches/")
        for path in tree(self.home):
            self.assertTrue(path.startswith(allowed), path)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".zshrc")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude")))

    def test_a_path_with_a_space_works_from_another_folder(self):
        # setUp puts a space in HOME, and this cwd is not the repo.
        with tempfile.TemporaryDirectory() as other:
            done = self.run_install(cwd=other)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(os.path.exists(self.config))

    def test_an_old_python_stops_before_any_write(self):
        fake = os.path.join(self.home, "fakebin")
        os.makedirs(fake)
        script = os.path.join(fake, "python3")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(script, 0o755)
        done = self.run_install(path=f"{fake}:/usr/bin:/bin")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("3.10", done.stderr)
        self.assertFalse(os.path.exists(self.config))

    def test_the_path_hint_appears_only_when_needed(self):
        done = self.run_install()
        self.assertIn("is not on your PATH", done.stdout)
        bin_dir = os.path.join(self.home, ".local", "bin")
        done = self.run_install(path=f"{bin_dir}:{os.environ['PATH']}")
        self.assertNotIn("is not on your PATH", done.stdout)

    def safe_path(self):
        """A PATH with python and node, stubs for ssh and scp, and no gws. A real gws under a fake
        HOME deletes the real login, so this run must never be able to find it."""
        bin_dir = os.path.join(self.home, "safebin")
        os.makedirs(bin_dir)
        os.symlink(sys.executable, os.path.join(bin_dir, "python3"))
        node = shutil.which("node")
        if node:
            os.symlink(node, os.path.join(bin_dir, "node"))
        for name in ("ssh", "scp"):
            stub = os.path.join(bin_dir, name)
            with open(stub, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexit 99\n")
            os.chmod(stub, 0o755)
        path = f"{bin_dir}:/usr/bin:/bin"
        self.assertIsNone(shutil.which("gws", path=path))
        return path

    def test_gws_as_finds_its_library_through_the_installed_link(self):
        path = self.safe_path()
        self.run_install(path=path)
        ran = subprocess.run([os.path.join(self.home, ".local", "bin", "gws-as")],
                             env={"HOME": self.home, "PATH": path}, cwd=self.home,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(ran.returncode, 2, ran.stdout + ran.stderr)
        self.assertIn("usage: gws-as", ran.stderr)

    def test_a_new_user_gets_a_clean_doctor_and_selftest_with_nothing_set_up(self):
        path = self.safe_path()
        done = self.run_install(path=path)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        comms = os.path.join(self.home, ".local", "bin", "comms")
        for verb in ("doctor", "selftest"):
            ran = subprocess.run([comms, verb], env={"HOME": self.home, "PATH": path},
                                 cwd=self.home, capture_output=True, text=True, timeout=280)
            self.assertNotIn("Traceback", ran.stdout + ran.stderr, verb)
            self.assertEqual(ran.returncode, 0, verb + ":\n" + ran.stdout + ran.stderr)


if __name__ == "__main__":
    unittest.main()
