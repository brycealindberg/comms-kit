#!/usr/bin/env python3
"""Ways personal config can fail. A guard must never go quiet because its value is missing."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import kitconfig  # noqa: E402


class KitConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "config.json")
        self.old = os.environ.get("COMMS_KIT_CONFIG")
        os.environ["COMMS_KIT_CONFIG"] = self.path
        self.addCleanup(self.restore)

    def restore(self):
        if self.old is None:
            os.environ.pop("COMMS_KIT_CONFIG", None)
        else:
            os.environ["COMMS_KIT_CONFIG"] = self.old

    def write(self, value):
        with open(self.path, "w") as fh:
            fh.write(value if isinstance(value, str) else json.dumps(value))

    def test_missing_file_gives_the_default(self):
        self.assertEqual(kitconfig.get("a.b", "d"), "d")

    def test_require_on_a_missing_key_names_the_key_and_file(self):
        self.write({})
        with self.assertRaises(kitconfig.ConfigError) as ctx:
            kitconfig.require("whatsapp.self_number")
        self.assertIn("whatsapp.self_number", str(ctx.exception))
        self.assertIn(self.path, str(ctx.exception))

    def test_require_refuses_an_empty_value(self):
        self.write({"a": ""})
        with self.assertRaises(kitconfig.ConfigError):
            kitconfig.require("a")

    def test_bad_json_raises_and_never_means_empty(self):
        self.write('{"secret-value": ')
        with self.assertRaises(kitconfig.ConfigError) as ctx:
            kitconfig.get("a", "default")
        self.assertNotIn("secret-value", str(ctx.exception))

    def test_top_level_list_is_refused(self):
        self.write([1, 2])
        with self.assertRaises(kitconfig.ConfigError):
            kitconfig.get("a")

    def test_dotted_key_through_a_scalar_gives_the_default(self):
        self.write({"a": 1})
        self.assertEqual(kitconfig.get("a.b", "d"), "d")

    def test_a_change_on_disk_is_seen(self):
        self.write({"a": 1})
        self.assertEqual(kitconfig.get("a"), 1)
        self.write({"a": 22})
        os.utime(self.path, (1, 1))
        self.assertEqual(kitconfig.get("a"), 22)

    def test_path_expands_home_and_defaults(self):
        self.write({"data_dir": "~/x"})
        self.assertEqual(kitconfig.path("data_dir"), os.path.expanduser("~/x"))
        self.assertEqual(kitconfig.path("nope", "~/y"), os.path.expanduser("~/y"))
        self.assertIsNone(kitconfig.path("nope"))

    def test_data_dir_default_is_outside_the_code(self):
        self.write({})
        self.assertEqual(kitconfig.data_dir(),
                         os.path.expanduser("~/.local/share/comms-kit"))

    def test_get_list_refuses_a_string(self):
        self.write({"a": "one"})
        with self.assertRaises(kitconfig.ConfigError):
            kitconfig.get_list("a")
        self.assertEqual(kitconfig.get_list("b"), [])

    def test_set_keeps_other_keys_and_makes_the_file_private(self):
        self.write({"keep": 1})
        kitconfig.set_value("x.y", "z")
        self.assertEqual(kitconfig.get("keep"), 1)
        self.assertEqual(kitconfig.get("x.y"), "z")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_set_through_a_scalar_is_refused(self):
        self.write({"x": 1})
        with self.assertRaises(kitconfig.ConfigError):
            kitconfig.set_value("x.y", "z")

    def test_set_on_bad_json_does_not_destroy_the_file(self):
        self.write("{broken")
        with self.assertRaises(kitconfig.ConfigError):
            kitconfig.set_value("a", "b")
        with open(self.path) as fh:
            self.assertEqual(fh.read(), "{broken")

    def test_concurrent_set_loses_no_key(self):
        code = os.path.dirname(HERE)
        procs = [subprocess.Popen(
            [sys.executable, os.path.join(code, "kitconfig.py"), "set", f"k{i}", "v"],
            env=dict(os.environ), stdout=subprocess.DEVNULL) for i in range(12)]
        self.assertEqual([p.wait() for p in procs], [0] * 12)
        self.assertEqual(sorted(json.load(open(self.path))), sorted(f"k{i}" for i in range(12)))

    def test_cli_set_never_prints_the_value(self):
        code = os.path.dirname(HERE)
        out = subprocess.run(
            [sys.executable, os.path.join(code, "kitconfig.py"), "set", "a", "PRIVATE-VALUE"],
            capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(out.returncode, 0)
        self.assertNotIn("PRIVATE-VALUE", out.stdout + out.stderr)

    def test_cli_keys_lists_names_without_values(self):
        self.write({"a": {"b": "PRIVATE-VALUE"}, "c": 1})
        code = os.path.dirname(HERE)
        out = subprocess.run([sys.executable, os.path.join(code, "kitconfig.py"), "keys"],
                             capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(out.stdout.split(), ["a.b", "c"])


if __name__ == "__main__":
    unittest.main()
