#!/usr/bin/env python3
"""A new channel plugs into the core by dropping in a folder, with no core edit.

Ways it goes wrong: doctor never looks at the folder; an unlisted subcommand passes as fine (its
sends would never be classified); one bad manifest stops the others, or crashes doctor; a manifest
that reuses a built-in name replaces that channel; an edit to a manifest is not seen; the new
channel's sends cannot be archived, or are archived twice because the capture hook also saw them.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# COMMS_KIT_CORE lets this run against the live core before the first pull
CORE = os.path.abspath(os.environ.get("COMMS_KIT_CORE") or os.path.join(REPO, "core"))
COMMS = os.path.join(CORE, "bin", "comms")

CLI = textwrap.dedent('''\
    import argparse
    p = argparse.ArgumentParser(prog="acme")
    sub = p.add_subparsers(dest="cmd")
    for name in %r:
        sub.add_parser(name)
    p.parse_args()
''')
MANIFEST = {
    "name": "acme", "entry": "acme.py", "kind": "python-cli",
    "verbs": {"list": {"direction": "read"}, "show": {"direction": "read"},
              "send": {"direction": "send", "dry_run_unless_yes": True}},
    "ignored": ["status"], "blocked": {},
}

LOG_EVENT = textwrap.dedent('''\
    import sys
    sys.path.insert(0, %(core)r)
    import commslib as C
    wrote = C.log_event("acme", "send", "lead@example.com", text="hello there",
                        message_id=%(mid)r, verb="send", hook_covers=%(covers)s,
                        command="acme send lead")
    print("WROTE" if wrote else "SKIPPED")
''')
LIST_NAMES = textwrap.dedent('''\
    import sys
    sys.path.insert(0, %(core)r)
    import commslib as C
    print(",".join(m["name"] for m in C.channel_manifests() if m["dir"].startswith(%(root)r)))
''')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="kit-plugin-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.data = os.path.join(self.tmp, "data")
        self.plugins = os.path.join(self.tmp, "plugins")
        for d in (self.home, self.plugins):
            os.makedirs(d)
        self.write_config([])

    def write_config(self, dirs):
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w") as fh:
            json.dump({"data_dir": self.data, "channel_dirs": dirs}, fh)

    def env(self, **extra):
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8",
               "COMMS_KIT_CONFIG": self.config, "COMMS_KIT_CORE": CORE,
               "PYTHONDONTWRITEBYTECODE": "1"}
        env.update(extra)
        return env

    def channel(self, name="acme", verbs=("list", "show", "send", "status"), manifest=None,
                raw=None):
        folder = os.path.join(self.plugins, name)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name + ".py"), "w") as fh:
            fh.write(CLI % (list(verbs),))
        with open(os.path.join(folder, "channel.json"), "w") as fh:
            fh.write(raw if raw is not None else json.dumps(manifest or MANIFEST))
        return folder

    def doctor(self):
        r = subprocess.run([sys.executable, COMMS, "doctor"], text=True, capture_output=True,
                           env=self.env(), cwd=self.tmp, timeout=180)
        return r.stdout + r.stderr

    def line(self, out, name):
        lines = [x for x in out.splitlines() if x.startswith(name + " ")]
        return lines[0] if lines else ""

    def py(self, code, **env):
        return subprocess.run([sys.executable, "-c", code], text=True, capture_output=True,
                              env=self.env(**env), cwd=self.tmp, timeout=60)


class Doctor(Base):
    def test_doctor_sees_a_new_channel_without_a_core_edit(self):
        self.channel()
        self.write_config([self.plugins + "/acme"])
        line = self.line(self.doctor(), "acme")
        self.assertIn(" ok ", line)
        self.assertIn("3 logged + 1 skipped on purpose / 4 in CLI", line)

    def test_a_subcommand_nobody_classified_is_a_check(self):
        self.channel(verbs=("list", "show", "send", "status", "purge"))
        self.write_config([self.plugins + "/acme"])
        out = self.doctor()
        self.assertIn("CHECK", self.line(out, "acme"))
        self.assertIn("UNCLASSIFIED (never logged): purge", out)

    def test_a_verb_the_cli_lost_is_reported(self):
        self.channel(verbs=("list", "send", "status"))
        self.write_config([self.plugins + "/acme"])
        out = self.doctor()
        self.assertIn("CHECK", self.line(out, "acme"))
        self.assertIn("in map but gone from CLI: show", out)

    def test_a_bad_manifest_warns_and_stops_nothing(self):
        self.channel()
        self.channel(name="broken", raw="{ not json")
        self.channel(name="noverbs", raw=json.dumps({"name": "noverbs", "entry": "x.py"}))
        self.write_config([self.plugins + "/broken", self.plugins + "/noverbs",
                           self.plugins + "/acme", self.tmp + "/missing-folder"])
        out = self.doctor()
        self.assertIn(" ok ", self.line(out, "acme"))
        warns = [x for x in out.splitlines() if x.startswith("plugin") and "CHECK" in x]
        self.assertTrue(any("broken" in w for w in warns), out)
        self.assertTrue(any("noverbs" in w for w in warns), out)
        self.assertNotIn("Traceback", out)


class Discovery(Base):
    def names(self):
        r = self.py(LIST_NAMES % {"core": CORE, "root": self.tmp})
        self.assertEqual(r.returncode, 0, r.stderr)
        return [x for x in r.stdout.strip().split(",") if x]

    def test_a_manifest_named_like_a_built_in_channel_is_skipped(self):
        self.channel(name="slack", manifest=dict(MANIFEST, name="slack", entry="slack.py"))
        self.channel()
        self.write_config([self.plugins + "/slack", self.plugins + "/acme"])
        self.assertEqual(self.names(), ["acme"])

    def test_listing_never_raises_on_garbage(self):
        self.write_config([self.tmp + "/nope", 7, None, "~/does/not/exist"])
        self.assertEqual(self.names(), [])

    def test_an_edited_manifest_is_seen_in_the_same_process(self):
        folder = self.channel(raw="{ not json")
        self.write_config([folder])
        code = textwrap.dedent('''\
            import json, os, sys, time
            sys.path.insert(0, %(core)r)
            import commslib as C
            first = [m["name"] for m in C.channel_manifests() if m["dir"].startswith(%(root)r)]
            path = os.path.join(%(folder)r, "channel.json")
            with open(path, "w") as fh:
                json.dump(%(manifest)r, fh)
            os.utime(path, (time.time() + 5, time.time() + 5))
            second = [m["name"] for m in C.channel_manifests() if m["dir"].startswith(%(root)r)]
            print(json.dumps([first, second]))
        ''') % {"core": CORE, "folder": folder, "manifest": MANIFEST, "root": self.tmp}
        r = self.py(code)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [[], ["acme"]])


class Archive(Base):
    def rows(self):
        found = []
        store = os.path.join(self.data, "store")
        if os.path.isdir(store):
            for name in sorted(os.listdir(store)):
                with open(os.path.join(store, name)) as fh:
                    found += [json.loads(x) for x in fh if x.strip()]
        return found

    def log(self, covers, **env):
        r = self.py(LOG_EVENT % {"core": CORE, "mid": "m-1", "covers": covers}, **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_log_event_writes_a_row_for_the_new_channel(self):
        self.assertEqual(self.log(False), "WROTE")
        rows = self.rows()
        self.assertEqual([(x["channel"], x["dir"], x["target"], x["text"]) for x in rows],
                         [("acme", "send", "lead@example.com", "hello there")])

    def test_a_plugin_send_is_archived_even_inside_the_capture_hook_runtime(self):
        # the hook never parses a plugin's command, so the plugin must write its own row
        self.assertEqual(self.log(False, CLAUDECODE="1"), "WROTE")
        self.assertEqual(len(self.rows()), 1)

    def test_the_default_still_leaves_a_receiptless_send_to_the_hook(self):
        r = self.py(LOG_EVENT % {"core": CORE, "mid": None, "covers": True}, CLAUDECODE="1")
        self.assertEqual(r.stdout.strip(), "SKIPPED")
        self.assertEqual(self.rows(), [])

    def test_the_hook_does_not_parse_a_plugin_command(self):
        code = textwrap.dedent('''\
            import sys
            sys.path.insert(0, %(core)r)
            import commslib as C
            print(len(list(C.detect_all("python3 /x/acme/acme.py send lead --yes"))))
            print("acme" in C.VERBS)
        ''') % {"core": CORE}
        r = self.py(code)
        self.assertEqual(r.stdout.split(), ["0", "False"], r.stderr)


if __name__ == "__main__":
    unittest.main()
