#!/usr/bin/env python3
"""A broken kit config must stop capture with one clear line and write nowhere.

Ways it goes wrong: the capture hook crashes a tool call; it falls back to a default folder and
archives rows where the owner never looks; it prints a traceback or nothing at all; the data
folder moves when the config is fine (the live Mac keeps its old folder); a module loaded by path
from another folder cannot find the config reader.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)  # the folder holding commslib.py; tests import their parent


def _find_hook():
    """The capture hook, at its live path or at its repo path."""
    for rel in (("..", "hooks", "comms-log.py"),
                ("..", "integrations", "claude-code", "comms-log.py")):
        path = os.path.abspath(os.path.join(CODE, *rel))
        if os.path.exists(path):
            return path
    raise unittest.SkipTest("capture hook not found")


HOOK = _find_hook()

LOAD_BY_SPEC = (
    "import importlib.util,sys\n"
    "spec = importlib.util.spec_from_file_location('commslib', sys.argv[1])\n"
    "m = importlib.util.module_from_spec(spec); sys.modules['commslib'] = m\n"
    "spec.loader.exec_module(m)\n"
    "print(m.ROOT)\n")

SEND = {"tool_name": "Bash", "cwd": "/tmp", "session_id": "abcd1234efgh",
        "tool_input": {"command": "gws gmail +send --to client@example.com --subject Hi "
                                  "--body 'hello there'"},
        "tool_response": {"stdout": '{"id": "18c0ffee", "threadId": "18c0ffee"}', "stderr": ""}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="kit-cfg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.cfgdir = os.path.join(self.tmp, "cfg")
        os.makedirs(self.cfgdir)

    def env(self, config=None):
        e = {"HOME": self.home, "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8",
             "COMMS_KIT_CORE": CODE, "PYTHONDONTWRITEBYTECODE": "1"}
        if config is not None:
            e["COMMS_KIT_CONFIG"] = config
        return e

    def config(self, text):
        path = os.path.join(self.cfgdir, "config.json")
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def run_hook(self, config):
        return subprocess.run([sys.executable, HOOK], input=json.dumps(SEND), text=True,
                              capture_output=True, env=self.env(config), cwd="/", timeout=60)

    def home_files(self):
        found = []
        for base, dirs, files in os.walk(self.home):
            found += [os.path.join(base, n) for n in dirs + files]
        return found


class BrokenConfig(Base):
    def check(self, config):
        r = self.run_hook(config)
        self.assertEqual(r.returncode, 0, r.stderr)  # a tool call is never failed
        lines = [x for x in r.stderr.splitlines() if x.strip()]
        self.assertEqual(len(lines), 1, r.stderr)    # one clear line, no traceback
        self.assertTrue(lines[0].startswith("comms-kit:"), lines[0])
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.home_files(), [])      # no default folder was created or written

    def test_invalid_json(self):
        self.check(self.config("{ not json"))

    def test_json_that_is_not_an_object(self):
        self.check(self.config("[1, 2]"))

    def test_config_path_is_a_folder(self):
        self.check(self.cfgdir)

    def test_the_line_does_not_echo_the_file_text(self):
        secret = "TOPSECRET-VALUE"
        r = self.run_hook(self.config('{"x": "%s" oops' % secret))
        self.assertNotIn(secret, r.stderr)

    def test_comms_cli_stops_with_one_line_and_writes_nothing(self):
        comms = os.path.join(CODE, "bin", "comms")
        r = subprocess.run([sys.executable, comms, "doctor"], text=True, capture_output=True,
                           env=self.env(self.config("{ not json")), cwd="/", timeout=60)
        self.assertNotEqual(r.returncode, 0)
        lines = [x for x in r.stderr.splitlines() if x.strip()]
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith("comms-kit:"), lines[0])
        self.assertEqual(self.home_files(), [])


class GoodConfig(Base):
    def test_hook_archives_into_the_configured_folder_only(self):
        data = os.path.join(self.tmp, "data")
        r = self.run_hook(self.config(json.dumps({"data_dir": data})))
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        rows = []
        store = os.path.join(data, "store")
        for name in os.listdir(store):
            with open(os.path.join(store, name)) as fh:
                rows += [json.loads(x) for x in fh if x.strip()]
        self.assertEqual([(x["channel"], x["dir"], x["target"]) for x in rows],
                         [("gmail", "send", "client@example.com")])
        self.assertEqual(self.home_files(), [])


class RootChoice(Base):
    def root(self, config=None):
        r = subprocess.run([sys.executable, "-c", LOAD_BY_SPEC, os.path.join(CODE, "commslib.py")],
                           text=True, capture_output=True, env=self.env(config), cwd="/",
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_data_dir_wins(self):
        data = os.path.join(self.tmp, "mine")
        self.assertEqual(self.root(self.config(json.dumps({"data_dir": data}))), data)
        self.assertEqual(self.home_files(), [])  # importing creates nothing

    def test_data_dir_wins_over_an_old_folder_with_routes(self):
        old = os.path.join(self.home, ".claude", "communications")
        os.makedirs(old)
        with open(os.path.join(old, "routes.json"), "w") as fh:
            fh.write("{}")
        data = os.path.join(self.tmp, "mine")
        self.assertEqual(self.root(self.config(json.dumps({"data_dir": data}))), data)

    def test_old_folder_with_routes_is_kept_when_data_dir_is_unset(self):
        old = os.path.join(self.home, ".claude", "communications")
        os.makedirs(old)
        with open(os.path.join(old, "routes.json"), "w") as fh:
            fh.write("{}")
        self.assertEqual(self.root(self.config("{}")), old)

    def test_no_config_file_and_no_old_folder_gives_the_kit_default(self):
        self.assertEqual(self.root(os.path.join(self.cfgdir, "absent.json")),
                         os.path.join(self.home, ".local", "share", "comms-kit"))
        self.assertEqual(self.home_files(), [])

    def test_old_folder_without_routes_is_not_taken(self):
        os.makedirs(os.path.join(self.home, ".claude", "communications"))
        self.assertEqual(self.root(self.config("{}")),
                         os.path.join(self.home, ".local", "share", "comms-kit"))


if __name__ == "__main__":
    unittest.main()
