#!/usr/bin/env python3
"""Tests for the release-gate tools. Each test plants a defect (or a clean case) in a temp
fixture and asserts the exit code and the printed line. Strings that the scrub rules would
flag in this file are built from parts."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TOOLS = os.path.join(ROOT, "tools")


def dedent(text):
    return textwrap.dedent(text).lstrip("\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.mkdir("home")

    def mkdir(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def env(self, **extra):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home,
               "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        env.update(extra)
        return env

    def tool(self, name, *args, env=None, timeout=240):
        return subprocess.run([sys.executable, os.path.join(TOOLS, name), *args],
                              capture_output=True, text=True, env=env or self.env(),
                              timeout=timeout)

    def git(self, repo, *args):
        subprocess.run(["git", "-C", repo, *args], check=True, env=self.env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def put(self, base, rel, content="", mode=None):
        path = os.path.join(base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content if isinstance(content, bytes) else content.encode())
        if mode is not None:
            os.chmod(path, mode)
        return path

    def repo(self, files, commit=True, name="repo"):
        """files: {relpath: text | bytes | (text, mode)}. Returns the repo path."""
        repo = self.mkdir(name)
        self.git(repo, "init", "-q", "-b", "main")
        for rel, value in files.items():
            content, mode = value if isinstance(value, tuple) else (value, None)
            self.put(repo, rel, content, mode)
        if commit:
            self.git(repo, "add", "-A")
            self.git(repo, "commit", "-qm", "fixture")
        return repo


# --------------------------------------------------------------------------- sync_live
class SyncLive(Base):
    def setUp(self):
        super().setUp()
        self.live = self.mkdir("live")
        self.deny = self.put(self.tmp, "deny.txt", "acme-secret-client\n")
        self.allow = self.put(self.tmp, "allow.txt", "")

    def make(self, files, mapping, kit_only=("README.md",), repo_files=None):
        for rel, text in files.items():
            self.put(self.live, rel, text)
        repo_files = {"README.md": "kit\n", **(repo_files or {})}
        repo = self.repo(repo_files, commit=False)
        map_path = os.path.join(self.tmp, "map.json")
        with open(map_path, "w") as fh:
            json.dump({"files": {k: os.path.join(self.live, v) for k, v in mapping.items()},
                       "kit_only": list(kit_only)}, fh)
        return repo, map_path

    def check(self, repo, map_path):
        return self.tool("sync_live.py", "--check", "--repo", repo, "--map", map_path)

    def pull(self, repo, map_path):
        return self.tool("sync_live.py", "--pull", "--repo", repo, "--map", map_path,
                         "--deny", self.deny, "--allow", self.allow)

    def test_identical_files_print_the_exact_line(self):
        repo, m = self.make({"a.py": "x = 1\n", "b.py": "y = 2\n"},
                            {"core/a.py": "a.py", "core/b.py": "b.py"},
                            repo_files={"core/a.py": "x = 1\n", "core/b.py": "y = 2\n"})
        out = self.check(repo, m)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.strip(), "in sync: 2 files, 0 differ")

    def test_one_byte_difference_is_found_and_contents_never_print(self):
        repo, m = self.make({"a.py": "token = 'live-side-marker'\n"}, {"core/a.py": "a.py"},
                            repo_files={"core/a.py": "token = 'repo-side-marker'\n"})
        out = self.check(repo, m)
        self.assertEqual(out.returncode, 1)
        self.assertIn("core/a.py", out.stdout)
        self.assertIn("differs", out.stdout)
        self.assertNotIn("side-marker", out.stdout + out.stderr)

    def test_line_ending_change_counts_as_a_difference(self):
        repo, m = self.make({"a.py": "x\r\n"}, {"core/a.py": "a.py"},
                            repo_files={"core/a.py": "x\n"})
        self.assertEqual(self.check(repo, m).returncode, 1)

    def test_missing_live_file_is_a_difference(self):
        repo, m = self.make({}, {"core/a.py": "gone.py"}, repo_files={"core/a.py": "x\n"})
        out = self.check(repo, m)
        self.assertEqual(out.returncode, 1)
        self.assertRegex(out.stdout, r"core/a\.py.*missing")

    def test_missing_repo_file_is_a_difference(self):
        repo, m = self.make({"a.py": "x\n"}, {"core/a.py": "a.py"})
        out = self.check(repo, m)
        self.assertEqual(out.returncode, 1)
        self.assertRegex(out.stdout, r"core/a\.py.*missing")

    def test_unmapped_file_under_core_and_channels_is_a_difference(self):
        repo, m = self.make({"a.py": "x\n"}, {"core/a.py": "a.py"},
                            repo_files={"core/a.py": "x\n", "core/stray.py": "s\n",
                                        "channels/wa/extra.js": "e\n"})
        out = self.check(repo, m)
        self.assertEqual(out.returncode, 1)
        self.assertIn("core/stray.py", out.stdout)
        self.assertIn("channels/wa/extra.js", out.stdout)

    def test_kit_only_pattern_covers_a_file_but_not_a_deeper_one(self):
        repo, m = self.make({"a.py": "x\n"}, {"core/a.py": "a.py"},
                            kit_only=("README.md", "channels/*/channel.json"),
                            repo_files={"core/a.py": "x\n", "channels/wa/channel.json": "{}",
                                        "channels/wa/deep/channel.json": "{}"})
        out = self.check(repo, m)
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("channels/wa/channel.json", out.stdout)
        self.assertIn("channels/wa/deep/channel.json", out.stdout)

    def test_pycache_and_node_modules_are_not_unmapped_files(self):
        repo, m = self.make({"a.py": "x\n"}, {"core/a.py": "a.py"},
                            repo_files={"core/a.py": "x\n", "core/__pycache__/a.pyc": "z",
                                        "channels/wa/node_modules/p/i.js": "z"})
        self.assertEqual(self.check(repo, m).returncode, 0)

    def test_empty_missing_or_kit_only_map_exits_2(self):
        repo = self.repo({"README.md": "x\n"}, commit=False)
        for body in ('{"files": {}, "kit_only": ["README.md"]}', "{}", "not json"):
            with self.subTest(body=body):
                m = self.put(self.tmp, "m.json", body)
                self.assertEqual(self.check(repo, m).returncode, 2)
        self.assertEqual(self.check(repo, os.path.join(self.tmp, "none.json")).returncode, 2)

    def test_map_key_that_escapes_the_repo_exits_2(self):
        repo, m = self.make({"a.py": "x\n"}, {"../a.py": "a.py"})
        self.assertEqual(self.check(repo, m).returncode, 2)

    def test_pull_copies_all_clean_files_and_keeps_the_mode(self):
        repo, m = self.make({"a.py": "x = 1\n", "comms": "#!/bin/sh\n"},
                            {"core/a.py": "a.py", "core/bin/comms": "comms"})
        os.chmod(os.path.join(self.live, "comms"), 0o755)
        os.chmod(os.path.join(self.live, "a.py"), 0o640)
        out = self.pull(repo, m)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        with open(os.path.join(repo, "core/a.py")) as fh:
            self.assertEqual(fh.read(), "x = 1\n")
        mode = stat.S_IMODE(os.stat(os.path.join(repo, "core/bin/comms")).st_mode)
        self.assertEqual(mode, 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(repo, "core/a.py")).st_mode), 0o640)
        self.assertEqual(self.check(repo, m).returncode, 0)

    def test_pull_overwrites_an_existing_repo_file(self):
        repo, m = self.make({"a.py": "new\n"}, {"core/a.py": "a.py"},
                            repo_files={"core/a.py": "old\n"})
        self.assertEqual(self.pull(repo, m).returncode, 0)
        with open(os.path.join(repo, "core/a.py")) as fh:
            self.assertEqual(fh.read(), "new\n")

    def test_pull_refuses_everything_when_one_file_breaks_a_rule(self):
        repo, m = self.make({"a.py": "fine\n", "b.py": "c = 'acme-secret-client'\n"},
                            {"core/a.py": "a.py", "core/b.py": "b.py"})
        out = self.pull(repo, m)
        self.assertEqual(out.returncode, 1)
        self.assertIn("core/b.py:1", out.stdout)
        self.assertIn("deny#1", out.stdout)
        self.assertNotIn("acme-secret-client", out.stdout + out.stderr)
        self.assertFalse(os.path.exists(os.path.join(repo, "core/a.py")), "clean file copied")
        self.assertFalse(os.path.exists(os.path.join(repo, "core/b.py")))

    def test_pull_refuses_a_built_in_rule_hit_without_any_deny_entry(self):
        repo, m = self.make({"a.py": "p = '/Us" "ers/somebody/x'\n"}, {"core/a.py": "a.py"})
        out = self.pull(repo, m)
        self.assertEqual(out.returncode, 1)
        self.assertIn("home-path", out.stdout)

    def test_pull_with_missing_deny_file_exits_2_and_copies_nothing(self):
        repo, m = self.make({"a.py": "x\n"}, {"core/a.py": "a.py"})
        out = self.tool("sync_live.py", "--pull", "--repo", repo, "--map", m,
                        "--deny", os.path.join(self.tmp, "nope.txt"))
        self.assertEqual(out.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(repo, "core/a.py")))

    def test_pull_with_a_missing_live_file_copies_nothing(self):
        repo, m = self.make({"a.py": "x\n"}, {"core/a.py": "a.py", "core/b.py": "gone.py"})
        out = self.pull(repo, m)
        self.assertEqual(out.returncode, 1)
        self.assertFalse(os.path.exists(os.path.join(repo, "core/a.py")))

    def test_pull_with_empty_map_exits_2(self):
        repo = self.repo({"README.md": "x\n"}, commit=False)
        m = self.put(self.tmp, "m.json", '{"files": {}}')
        self.assertEqual(self.pull(repo, m).returncode, 2)

    # ---- rewrites: private text swaps applied to the live bytes, never written back live
    def make_rw(self, live_text, repo_text, rewrites, rel="channels/x/lib.js"):
        repo, m = self.make({"lib.js": live_text}, {rel: "lib.js"},
                            repo_files={rel: repo_text} if repo_text is not None else None)
        with open(m) as fh:
            data = json.load(fh)
        data["rewrites"] = {rel: rewrites}
        with open(m, "w") as fh:
            json.dump(data, fh)
        return repo, m, rel

    def test_check_sees_a_rewritten_file_as_in_sync(self):
        repo, m, _ = self.make_rw("const owner = 'acme-secret-client';\n",
                                  "const owner = 'the-owner';\n",
                                  [["acme-secret-client", "the-owner"]])
        out = self.check(repo, m)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.strip(), "in sync: 1 files, 0 differ")

    def test_check_without_the_rewrite_still_sees_the_difference(self):
        repo, m, rel = self.make_rw("v = 'acme-secret-client'\n", "v = 'the-owner'\n", [])
        self.assertEqual(self.check(repo, m).returncode, 1)

    def test_a_stale_rewrite_is_a_difference_so_drift_cannot_hide(self):
        # Live no longer holds the old text. The repo copy must not read as in sync.
        repo, m, rel = self.make_rw("v = 'brand-new-live-text'\n", "v = 'the-owner'\n",
                                    [["acme-secret-client", "the-owner"]])
        out = self.check(repo, m)
        self.assertEqual(out.returncode, 1)
        self.assertRegex(out.stdout, rf"{rel}.*rewrite")
        self.assertNotIn("acme-secret-client", out.stdout + out.stderr)

    def test_rewrite_applies_to_every_occurrence_in_order(self):
        repo, m, _ = self.make_rw("a acme-secret-client b acme-secret-client\n",
                                  "a NEXT b NEXT\n",
                                  [["acme-secret-client", "mid"], ["mid", "NEXT"]])
        self.assertEqual(self.check(repo, m).returncode, 0)

    def test_pull_copies_the_rewritten_bytes_and_leaves_live_alone(self):
        repo, m, rel = self.make_rw("v = 'acme-secret-client'\n", None,
                                    [["acme-secret-client", "the-owner"]])
        out = self.pull(repo, m)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        with open(os.path.join(repo, rel)) as fh:
            self.assertEqual(fh.read(), "v = 'the-owner'\n")
        with open(os.path.join(self.live, "lib.js")) as fh:
            self.assertEqual(fh.read(), "v = 'acme-secret-client'\n")
        self.assertEqual(self.check(repo, m).returncode, 0)

    def test_pull_refuses_a_file_that_still_fails_scrub_after_its_rewrites(self):
        repo, m, rel = self.make_rw("a = 'acme-secret-client'\nb = 'other-thing'\n", None,
                                    [["other-thing", "fine"]])
        out = self.pull(repo, m)
        self.assertEqual(out.returncode, 1)
        self.assertIn(f"{rel}:1", out.stdout)
        self.assertNotIn("acme-secret-client", out.stdout + out.stderr)
        self.assertFalse(os.path.exists(os.path.join(repo, rel)))

    def test_pull_scrubs_the_rewritten_text_not_the_old_text(self):
        # A rewrite may not smuggle a denied word in as the replacement.
        repo, m, rel = self.make_rw("v = 'plain'\n", None, [["plain", "acme-secret-client"]])
        out = self.pull(repo, m)
        self.assertEqual(out.returncode, 1)
        self.assertFalse(os.path.exists(os.path.join(repo, rel)))

    def test_pull_with_a_stale_rewrite_copies_nothing(self):
        repo, m, rel = self.make_rw("v = 'plain'\n", None, [["not-in-live", "x"]])
        out = self.pull(repo, m)
        self.assertEqual(out.returncode, 1)
        self.assertRegex(out.stdout, rf"{rel}.*rewrite")
        self.assertFalse(os.path.exists(os.path.join(repo, rel)))

    def test_bad_rewrite_shapes_exit_2(self):
        for bad in ({"channels/x/lib.js": "text"},               # not a list of pairs
                    {"channels/x/lib.js": [["only-one"]]},       # pair of one
                    {"channels/x/lib.js": [["", "x"]]},          # empty old text matches everywhere
                    {"channels/x/lib.js": [["a", 5]]},           # new is not text
                    {"channels/other.js": [["a", "b"]]},         # file is not in the map
                    ["not", "an", "object"]):
            repo, m, _ = self.make_rw("v = 1\n", "v = 1\n", [])
            with open(m) as fh:
                data = json.load(fh)
            data["rewrites"] = bad
            with open(m, "w") as fh:
                json.dump(data, fh)
            self.assertEqual(self.check(repo, m).returncode, 2, bad)
            shutil.rmtree(repo, True)


# --------------------------------------------------------------------------- clean_clone_test
class CleanClone(Base):
    def test_core_tests_and_a_nested_lock_file_are_discovered(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import clean_clone_test as CC
        clone = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, clone, True)
        for rel in ("core/tests/test_a.py", "channels/x/test_b.py", "channels/x/bridge/package-lock.json"):
            path = os.path.join(clone, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        suites, node = CC.discover(clone)
        self.assertIn("core/tests/test_a.py", suites)
        self.assertEqual(node, ["channels/x/bridge"])

    def test_worktree_mode_sees_an_uncommitted_file_and_skips_an_ignored_one(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import clean_clone_test as CC
        repo, dest = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo, True)
        self.addCleanup(shutil.rmtree, dest, True)
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        for rel, text in ((".gitignore", "secret.json\n"), ("new.py", "x\n"), ("secret.json", "y\n")):
            with open(os.path.join(repo, rel), "w") as fh:
                fh.write(text)
        target = os.path.join(dest, "clone")
        CC.copy_worktree(repo, target)
        self.assertTrue(os.path.exists(os.path.join(target, "new.py")))
        self.assertFalse(os.path.exists(os.path.join(target, "secret.json")))

    def setUp(self):
        super().setUp()
        self.example = '{"data_dir": "", "_external_keys": []}\n'

    def clone_repo(self, files, commit=True):
        base = {"config.example.json": self.example}
        base.update(files)
        return self.repo(base, commit=commit)

    def run_clean(self, repo, *extra):
        real_home = self.mkdir("realhome")
        return self.tool("clean_clone_test.py", "--repo", repo, "--real-home", real_home,
                         *extra)

    def test_passing_suite_prints_one_line_and_no_test_output(self):
        repo = self.clone_repo({"tests/test_ok.py": dedent("""
            import os
            print("noisy output line")
            """)})
        out = self.run_clean(repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("PASS tests/test_ok.py", out.stdout)
        self.assertNotIn("noisy output line", out.stdout)
        self.assertIn("clean clone: OK (1 suites)", out.stdout)

    def test_the_suite_sees_the_clean_environment(self):
        repo = self.mkdir("envrepo")
        body = dedent("""
            import os
            home = os.environ["HOME"]
            assert os.listdir(home) == [], os.listdir(home)
            cfg = os.environ["COMMS_KIT_CONFIG"]
            assert open(cfg).read() == open("config.example.json").read(), "config copy"
            first = os.environ["PATH"].split(os.pathsep)[0]
            for name in ("gws", "gws-as", "ssh", "scp", "imessage", "imsg", "npm-publish",
                         "swiftc"):
                assert os.access(os.path.join(first, name), os.X_OK), name
            assert os.path.exists("tests/test_env.py"), os.getcwd()
            assert os.path.realpath(os.getcwd()) != os.path.realpath(%r), "ran in the source"
            """) % repo
        self.git(repo, "init", "-q", "-b", "main")
        self.put(repo, "config.example.json", self.example)
        self.put(repo, "tests/test_env.py", body)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-qm", "x")
        out = self.run_clean(repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_uncommitted_files_cannot_make_it_pass_or_fail(self):
        repo = self.clone_repo({"tests/test_ok.py": "pass\n"})
        self.put(repo, "tests/test_untracked.py", "raise SystemExit(1)\n")
        out = self.run_clean(repo)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("(1 suites)", out.stdout)

    def test_a_file_present_only_in_the_working_tree_is_missing_in_the_clone(self):
        repo = self.clone_repo({"tests/test_dep.py": dedent("""
            import os
            assert os.path.exists("helper_data.txt")
            """)})
        self.put(repo, "helper_data.txt", "only here\n")
        out = self.run_clean(repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("FAIL tests/test_dep.py", out.stdout)

    def test_failing_suite_prints_its_last_15_lines_only(self):
        repo = self.clone_repo({"tests/test_bad.py": dedent("""
            for i in range(1, 21):
                print("line-%02d" % i)
            raise SystemExit(1)
            """)})
        out = self.run_clean(repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("FAIL tests/test_bad.py", out.stdout)
        self.assertIn("line-20", out.stdout)
        self.assertIn("line-06", out.stdout)
        self.assertNotIn("line-05", out.stdout)

    def test_a_suite_that_calls_a_real_tool_name_fails_even_if_it_exits_0(self):
        repo = self.clone_repo({"tests/test_gws.py": dedent("""
            import subprocess
            try:
                subprocess.run(["gws", "drive"])
            except Exception:
                pass
            """), "tests/test_fine.py": "pass\n"})
        out = self.run_clean(repo)
        self.assertEqual(out.returncode, 1)
        self.assertRegex(out.stdout, r"FAIL tests/test_gws\.py.*gws")
        self.assertIn("PASS tests/test_fine.py", out.stdout)

    def test_each_forbidden_name_is_caught(self):
        for name in ("ssh", "scp", "imessage", "imsg"):
            with self.subTest(name=name):
                repo = self.repo({"config.example.json": self.example,
                                  "tests/test_x.py": "import subprocess\n"
                                                     "subprocess.run([%r])\n" % name},
                                 name="r_" + name)
                out = self.run_clean(repo)
                self.assertEqual(out.returncode, 1, out.stdout)
                self.assertIn(name, out.stdout)

    def test_swiftc_is_recorded_but_allowed(self):
        repo = self.clone_repo({"tests/test_swift.py": dedent("""
            import subprocess
            r = subprocess.run(["swiftc", "--version"])
            assert r.returncode == 99
            """)})
        out = self.run_clean(repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_a_fake_tool_placed_earlier_on_path_is_allowed(self):
        repo = self.clone_repo({"tests/test_fake.py": dedent("""
            import os, subprocess, tempfile
            d = tempfile.mkdtemp()
            p = os.path.join(d, "gws")
            with open(p, "w") as fh:
                fh.write("#!/bin/sh\\nexit 0\\n")
            os.chmod(p, 0o755)
            env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"])
            assert subprocess.run(["gws"], env=env).returncode == 0
            """)})
        out = self.run_clean(repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_channel_test_files_are_discovered(self):
        repo = self.clone_repo({"tests/test_a.py": "pass\n", "channels/wa/test_b.py": "pass\n",
                                "channels/wa/tests/test_c.py": "pass\n",
                                "channels/wa/other.py": "raise SystemExit(1)\n"})
        out = self.run_clean(repo)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("(3 suites)", out.stdout)

    def test_a_suite_that_runs_zero_unittest_tests_fails(self):
        repo = self.clone_repo({"tests/test_empty.py": dedent("""
            import unittest
            class T(unittest.TestCase):
                pass
            if __name__ == "__main__":
                unittest.main()
            """)})
        out = self.run_clean(repo)
        self.assertEqual(out.returncode, 1, out.stdout)

    def test_timeout_fails_the_suite_and_does_not_hang(self):
        repo = self.clone_repo({"tests/test_slow.py": "import time\ntime.sleep(60)\n"})
        out = self.run_clean(repo, "--timeout", "2")
        self.assertEqual(out.returncode, 1)
        self.assertRegex(out.stdout, r"FAIL tests/test_slow\.py.*time")

    def test_no_suites_exits_2(self):
        self.assertEqual(self.run_clean(self.clone_repo({"core/x.py": "x\n"})).returncode, 2)

    def test_repo_without_a_commit_exits_2(self):
        repo = self.clone_repo({"tests/test_ok.py": "pass\n"}, commit=False)
        self.assertEqual(self.run_clean(repo).returncode, 2)

    def test_missing_config_example_exits_2(self):
        repo = self.repo({"tests/test_ok.py": "pass\n"})
        self.assertEqual(self.run_clean(repo).returncode, 2)

    def test_a_write_into_the_real_config_dir_fails_the_run(self):
        real_home = self.mkdir("realhome")
        cfg = os.path.join(real_home, ".config", "comms-kit")
        os.makedirs(cfg)
        self.put(cfg, "live-map.json", "{}")
        repo = self.mkdir("leakrepo")
        self.git(repo, "init", "-q", "-b", "main")
        self.put(repo, "config.example.json", self.example)
        self.put(repo, "tests/test_leak.py", dedent("""
            with open(%r, "a") as fh:
                fh.write("more bytes")
            """) % os.path.join(cfg, "live-map.json"))
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-qm", "x")
        out = self.tool("clean_clone_test.py", "--repo", repo, "--real-home", real_home)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("comms-kit", out.stdout)

    def test_a_new_file_in_the_real_data_dir_fails_the_run(self):
        real_home = self.mkdir("realhome")
        repo = self.mkdir("leakrepo2")
        self.git(repo, "init", "-q", "-b", "main")
        self.put(repo, "config.example.json", self.example)
        self.put(repo, "tests/test_leak.py", dedent("""
            import os
            d = %r
            os.makedirs(d)
            open(os.path.join(d, "x"), "w").close()
            """) % os.path.join(real_home, ".local", "share", "comms-kit"))
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-qm", "x")
        out = self.tool("clean_clone_test.py", "--repo", repo, "--real-home", real_home)
        self.assertEqual(out.returncode, 1, out.stdout)

    @unittest.skipUnless(shutil.which("npm") and shutil.which("node"), "needs node and npm")
    def test_node_channel_runs_npm_ci_and_node_test(self):
        lock = {"name": "x", "version": "1.0.0", "lockfileVersion": 3, "requires": True,
                "packages": {"": {"name": "x", "version": "1.0.0"}}}
        files = {"channels/x/package.json": '{"name": "x", "version": "1.0.0"}',
                 "channels/x/package-lock.json": json.dumps(lock),
                 "channels/x/a.test.js": "const t = require('node:test');\n"
                                         "t('ok', () => {});\n"}
        out = self.run_clean(self.clone_repo(files))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("(1 suites)", out.stdout)
        files["channels/x/a.test.js"] = ("const t = require('node:test');\n"
                                         "t('bad', () => { throw new Error('boom'); });\n")
        out = self.run_clean(self.repo({"config.example.json": self.example, **files},
                                       name="nodebad"))
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("FAIL channels/x", out.stdout)


# --------------------------------------------------------------------------- check_data_paths
COMMS_OK = dedent("""
    #!/usr/bin/env python3
    import json, os, sys
    cfg = json.load(open(os.environ["COMMS_KIT_CONFIG"]))
    if sys.argv[1:3] == ["contact", "list"]:
        os.makedirs(cfg["data_dir"], exist_ok=True)
        with open(os.path.join(cfg["data_dir"], "contacts.json"), "w") as fh:
            fh.write("[]")
        print("none")
    """)


class DataPaths(Base):
    def static_repo(self, source, name="core/mod.py", extra=None):
        files = {name: source, "core/bin/comms": (COMMS_OK, 0o755),
                 "config.example.json": '{"data_dir": ""}'}
        files.update(extra or {})
        return self.repo(files, commit=False)

    def static(self, repo):
        return self.tool("check_data_paths.py", "--static", "--repo", repo)

    def test_data_file_next_to_the_code_is_listed_with_its_line(self):
        repo = self.static_repo(dedent("""
            import os
            def load():
                return open(os.path.join(os.path.dirname(__file__), "notes.txt")).read()
            """))
        out = self.static(repo)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("core/mod.py:3", out.stdout)

    def test_pathlib_data_dir_through_a_variable_is_listed(self):
        repo = self.static_repo(dedent("""
            from pathlib import Path
            DATA = Path(__file__).parent / "data"
            def save():
                DATA.mkdir(exist_ok=True)
            """))
        out = self.static(repo)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("core/mod.py", out.stdout)

    def test_variable_chain_to_a_non_code_file_is_listed(self):
        repo = self.static_repo(dedent("""
            import os
            HERE = os.path.dirname(os.path.abspath(__file__))
            DB = os.path.join(HERE, "state.db")
            def get():
                return open(DB)
            """))
        self.assertEqual(self.static(repo).returncode, 1)

    def test_unknown_join_component_is_listed(self):
        repo = self.static_repo(dedent("""
            import os
            def p(name):
                return os.path.join(os.path.dirname(__file__), name)
            """))
        self.assertEqual(self.static(repo).returncode, 1)

    def test_directory_used_for_anything_but_imports_is_listed(self):
        repo = self.static_repo(dedent("""
            import os
            HERE = os.path.dirname(__file__)
            def names():
                return os.listdir(HERE)
            """))
        self.assertEqual(self.static(repo).returncode, 1)

    def test_code_file_uses_are_allowed(self):
        repo = self.static_repo(dedent("""
            import os, sys
            from pathlib import Path
            HERE = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, HERE)
            sys.path.append(str(Path(__file__).resolve().parent.parent))
            HELPER = os.path.join(HERE, "helper.py")
            SWIFT = Path(__file__).parent / "tool.swift"
            MANIFEST = os.path.join(HERE, "channel.json")
            COMMS = os.path.join(HERE, "bin", "comms")
            ME = os.path.basename(__file__)
            SELF = Path(__file__).name
            """), extra={"core/bin/comms": (COMMS_OK, 0o755)})
        out = self.static(repo)
        self.assertEqual(out.returncode, 0, out.stdout)

    def test_inline_comment_allows_a_line(self):
        repo = self.static_repo(dedent("""
            import os
            NAMES = os.listdir(os.path.dirname(__file__))  # kit: code path
            """))
        self.assertEqual(self.static(repo).returncode, 0)

    def test_test_files_are_not_scanned(self):
        repo = self.static_repo("x = 1\n", extra={
            "core/test_mod.py": 'import os\nopen(os.path.join(os.path.dirname(__file__), "a.txt"))\n',
            "channels/wa/tests/test_z.py": 'open(__file__ + ".txt")\n'})
        self.assertEqual(self.static(repo).returncode, 0)

    def test_extensionless_python_script_is_scanned(self):
        repo = self.static_repo("x = 1\n", extra={"core/bin/comms": (
            "#!/usr/bin/env python3\nimport os\nopen(os.path.dirname(__file__) + '/x.txt')\n",
            0o755)})
        out = self.static(repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("core/bin/comms:3", out.stdout)

    def test_no_comms_script_exits_2(self):
        repo = self.repo({"core/mod.py": "x = 1\n", "config.example.json": "{}"}, commit=False)
        self.assertEqual(self.tool("check_data_paths.py", "--static", "--repo", repo).returncode, 2)

    # behavior part
    def commands(self, argvs, expect=None):
        body = {"commands": [{"name": " ".join(a[1:]), "argv": a,
                              **({"expect_exit": expect} if expect is not None else {})}
                             for a in argvs]}
        return self.put(self.tmp, "cmds.json", json.dumps(body))

    def behavior(self, repo, cmds):
        real_home = self.mkdir("realhome")
        return self.tool("check_data_paths.py", "--behavior", "--repo", repo, "--commands", cmds,
                         "--real-home", real_home)

    def test_commands_that_write_only_under_the_data_dir_pass(self):
        repo = self.static_repo("x = 1\n")
        out = self.behavior(repo, self.commands([["core/bin/comms", "contact", "list"]]))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.strip(),
                         "data paths ok: 1 files written under the data dir, code tree unchanged")

    def test_commands_that_write_nothing_prove_nothing(self):
        script = "#!/usr/bin/env python3\nprint('hi')\n"
        repo = self.static_repo("x = 1\n", extra={"core/bin/comms": (script, 0o755)})
        out = self.behavior(repo, self.commands([["core/bin/comms", "contact", "list"]]))
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("no command wrote", out.stdout)

    def test_a_write_next_to_the_code_is_a_defect(self):
        script = dedent("""
            #!/usr/bin/env python3
            import os
            open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.json"), "w").write("{}")
            """)
        repo = self.static_repo("x = 1\n", extra={"core/bin/comms": (script, 0o755)})
        out = self.behavior(repo, self.commands([["core/bin/comms", "contact", "list"]]))
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("core/bin/cache.json", out.stdout)

    def test_a_changed_code_file_is_a_defect(self):
        script = dedent("""
            #!/usr/bin/env python3
            import os
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mod.py")
            open(p, "a").write("# touched\\n")
            """)
        repo = self.static_repo("x = 1\n", extra={"core/bin/comms": (script, 0o755)})
        out = self.behavior(repo, self.commands([["core/bin/comms", "contact", "list"]]))
        self.assertEqual(out.returncode, 1)
        self.assertIn("core/mod.py", out.stdout)

    def test_a_write_in_home_outside_the_data_dir_is_a_defect(self):
        script = dedent("""
            #!/usr/bin/env python3
            import os
            d = os.path.join(os.environ["HOME"], ".local", "share", "elsewhere")
            os.makedirs(d)
            open(os.path.join(d, "x.db"), "w").write("1")
            """)
        repo = self.static_repo("x = 1\n", extra={"core/bin/comms": (script, 0o755)})
        out = self.behavior(repo, self.commands([["core/bin/comms", "contact", "list"]]))
        self.assertEqual(out.returncode, 1)
        self.assertIn(".local", out.stdout)

    def test_a_write_into_the_real_data_dir_is_a_defect(self):
        real_home = self.mkdir("realhome")
        script = dedent("""
            #!/usr/bin/env python3
            import os
            d = %r
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "x"), "w").write("1")
            """) % os.path.join(real_home, ".local", "share", "comms-kit")
        repo = self.static_repo("x = 1\n", extra={"core/bin/comms": (script, 0o755)})
        out = self.behavior(repo, self.commands([["core/bin/comms", "contact", "list"]]))
        self.assertEqual(out.returncode, 1, out.stdout)

    def test_a_command_that_fails_is_a_defect(self):
        script = "#!/usr/bin/env python3\nraise SystemExit(3)\n"
        repo = self.static_repo("x = 1\n", extra={"core/bin/comms": (script, 0o755)})
        out = self.behavior(repo, self.commands([["core/bin/comms", "contact", "list"]]))
        self.assertEqual(out.returncode, 1)
        self.assertIn("exit 3", out.stdout)

    def test_a_command_that_calls_gws_is_a_defect(self):
        script = "#!/usr/bin/env python3\nimport subprocess\nsubprocess.run(['gws'])\n"
        repo = self.static_repo("x = 1\n", extra={"core/bin/comms": (script, 0o755)})
        out = self.behavior(repo, self.commands([["core/bin/comms", "contact", "list"]]))
        self.assertEqual(out.returncode, 1)
        self.assertIn("gws", out.stdout)

    def test_absent_comms_or_empty_command_list_exits_2(self):
        repo = self.repo({"core/mod.py": "x = 1\n"}, commit=False)
        out = self.behavior(repo, self.commands([["core/bin/comms", "contact", "list"]]))
        self.assertEqual(out.returncode, 2)
        repo2 = self.static_repo("x = 1\n")
        out = self.behavior(repo2, self.put(self.tmp, "empty.json", '{"commands": []}'))
        self.assertEqual(out.returncode, 2)

    def test_shipped_command_file_lists_contact_list(self):
        with open(os.path.join(TOOLS, "data_path_commands.json")) as fh:
            data = json.load(fh)
        argvs = [c["argv"] for c in data["commands"]]
        self.assertIn(["core/bin/comms", "contact", "list"], argvs)


# --------------------------------------------------------------------------- capabilities
CLI = dedent("""
    import argparse
    ap = argparse.ArgumentParser(prog="wa")
    sub = ap.add_subparsers(dest="cmd")
    %s
    ap.parse_args()
    """)


def cli(*verbs):
    return CLI % "\n".join('sub.add_parser(%r)' % v for v in verbs)


def manifest(**over):
    base = {"name": "wa", "entry": "channels/wa/cli.py", "kind": "python-cli",
            "verbs": {"send": {"direction": "send", "dry_run_unless_yes": True},
                      "read": {"direction": "read"}},
            "blocked": {}, "config_keys": [], "requires": []}
    base.update(over)
    return json.dumps(base)


class Capabilities(Base):
    def run_caps(self, repo, *extra):
        return self.tool("capabilities.py", "--repo", repo, *extra)

    def test_the_summary_puts_each_verb_in_its_own_column(self):
        manifest = json.dumps({"name": "wa", "entry": "cli.py", "kind": "external", "verbs": {
            "history": {"direction": "read"}, "send": {"direction": "send"},
            "status": {"direction": "none"}}, "blocked": {"unblock": "no call for it"}})
        out = self.run_caps(self.chan(manifest), "--summary")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("| wa | `history` | `send` | `status` | `unblock` |", out.stdout)

    def chan(self, channel_json, cli_src=None, name="wa", extra=None):
        files = {"channels/%s/channel.json" % name: channel_json}
        if cli_src is not None:
            files["channels/%s/cli.py" % name] = cli_src
        files.update(extra or {})
        return self.repo(files, commit=False)

    def test_matching_cli_and_manifest_pass_with_exact_line(self):
        repo = self.chan(manifest(), cli("send", "read"))
        out = self.run_caps(repo, "--check")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("capabilities ok: 1 channels, 2 verbs (1 checked against a real CLI)",
                      out.stdout)

    def test_cli_subcommand_missing_from_the_manifest_is_a_defect(self):
        repo = self.chan(manifest(), cli("send", "read", "purge"))
        out = self.run_caps(repo, "--check")
        self.assertEqual(out.returncode, 1)
        self.assertIn("purge", out.stdout)

    def test_listed_verb_missing_from_the_cli_is_a_defect(self):
        repo = self.chan(manifest(), cli("send"))
        out = self.run_caps(repo, "--check")
        self.assertEqual(out.returncode, 1)
        self.assertIn("read", out.stdout)

    def test_ignored_list_and_blocked_cover_extra_subcommands(self):
        m = json.loads(manifest())
        m["ignored"] = ["version"]
        m["blocked"] = {"block": "account bans risk"}
        repo = self.chan(json.dumps(m), cli("send", "read", "version", "block"))
        self.assertEqual(self.run_caps(repo, "--check").returncode, 0)

    def test_blocked_verb_need_not_exist_in_the_cli(self):
        repo = self.chan(manifest(blocked={"delete": "never"}), cli("send", "read"))
        self.assertEqual(self.run_caps(repo, "--check").returncode, 0)

    def test_send_verb_without_dry_run_statement_is_a_defect(self):
        m = json.loads(manifest())
        del m["verbs"]["send"]["dry_run_unless_yes"]
        repo = self.chan(json.dumps(m), cli("send", "read"))
        out = self.run_caps(repo, "--check")
        self.assertEqual(out.returncode, 1)
        self.assertIn("dry_run_unless_yes", out.stdout)

    def test_node_channel_is_shape_checked_and_the_summary_says_so(self):
        m = json.loads(manifest(kind="node", entry="channels/wa/index.js"))
        repo = self.chan(json.dumps(m))
        out = self.run_caps(repo, "--check")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("(0 checked against a real CLI)", out.stdout)
        self.assertIn("manifest shape only", out.stdout)

    def test_bad_manifest_shapes_are_defects(self):
        cases = {"direction": manifest(kind="node", verbs={"x": {"direction": "sideways"}}),
                 "kind": manifest(kind="ruby"), "name": manifest(name="other"),
                 "missing keys": json.dumps({"name": "wa"}), "not json": "{oops"}
        for label, body in cases.items():
            with self.subTest(label):
                repo = self.chan(body, cli("send", "read"), name="wa")
                out = self.run_caps(repo, "--check")
                self.assertEqual(out.returncode, 1, out.stdout)

    def test_channel_folder_without_a_manifest_is_a_defect(self):
        repo = self.chan(manifest(kind="node"), extra={"channels/zz/readme.txt": "x"})
        out = self.run_caps(repo, "--check")
        self.assertEqual(out.returncode, 1)
        self.assertIn("channels/zz", out.stdout)

    def test_missing_entry_file_is_a_defect(self):
        repo = self.chan(manifest())
        self.assertEqual(self.run_caps(repo, "--check").returncode, 1)

    def test_help_that_calls_a_real_tool_is_a_defect(self):
        src = "import subprocess\ntry:\n    subprocess.run(['gws'])\nexcept OSError:\n    pass\n"
        repo = self.chan(manifest(), src + cli("send", "read"))
        out = self.run_caps(repo, "--check")
        self.assertEqual(out.returncode, 1)
        self.assertIn("gws", out.stdout)

    def test_cli_with_no_subcommand_group_fails_when_verbs_are_listed(self):
        repo = self.chan(manifest(), "import argparse\nargparse.ArgumentParser().parse_args()\n")
        self.assertEqual(self.run_caps(repo, "--check").returncode, 1)

    def test_option_choices_before_the_subcommands_are_not_mistaken_for_them(self):
        src = dedent("""
            import argparse
            ap = argparse.ArgumentParser(prog="wa")
            ap.add_argument("--format", choices=["json", "text"])
            sub = ap.add_subparsers(dest="cmd")
            sub.add_parser("send")
            sub.add_parser("read")
            ap.parse_args()
            """)
        repo = self.chan(manifest(), src)
        out = self.run_caps(repo, "--check")
        self.assertEqual(out.returncode, 0, out.stdout)

    def test_single_subcommand_cli_is_parsed(self):
        repo = self.chan(manifest(verbs={"send": {"direction": "send",
                                                  "dry_run_unless_yes": True}}), cli("send"))
        self.assertEqual(self.run_caps(repo, "--check").returncode, 0)

    def test_zero_channels_exits_2(self):
        repo = self.repo({"README.md": "x"}, commit=False)
        self.assertEqual(self.run_caps(repo, "--check").returncode, 2)

    def test_table_lists_channel_verb_direction_and_blocked_reason(self):
        repo = self.chan(manifest(blocked={"block": "bans accounts"}), cli("send", "read"))
        out = self.run_caps(repo, "--table")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("| channel | verb | direction | blocked reason |", out.stdout)
        self.assertIn("| wa | send | send | - |", out.stdout)
        self.assertIn("| wa | block | blocked | bans accounts |", out.stdout)


# --------------------------------------------------------------------------- check_licenses
def lock(packages):
    return json.dumps({"name": "x", "lockfileVersion": 3,
                       "packages": {"": {"name": "x"}, **packages}})


class Licenses(Base):
    def mit(self):
        with open(os.path.join(ROOT, "LICENSE")) as fh:
            return fh.read()

    def lic_repo(self, files=None, license_text=None):
        base = {"LICENSE": self.mit() if license_text is None else license_text}
        base.update(files or {})
        return self.repo(base, commit=False)

    def run_lic(self, repo, *extra):
        return self.tool("check_licenses.py", "--repo", repo, *extra)

    def test_our_own_cli_may_name_the_bridge_it_talks_to(self):
        out = self.run_lic(self.lic_repo({"channels/whatsapp/whatsapp_mautrix.py": "x\n"}))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_the_same_file_name_elsewhere_stays_banned(self):
        out = self.run_lic(self.lic_repo({"channels/other/whatsapp_mautrix.py": "x\n"}))
        self.assertEqual(out.returncode, 1)

    def test_clean_repo_without_a_lock_file_says_zero_packages(self):
        out = self.run_lic(self.lic_repo({"a.py": "x\n"}))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.strip(), "licenses ok: 0 packages")

    def test_license_text_must_be_mit(self):
        for label, text in (("missing", None), ("other", "Apache License\nVersion 2.0\n"),
                            ("cut", self.mit().split("THE SOFTWARE IS PROVIDED")[0])):
            with self.subTest(label):
                files = {"a.py": "x\n"}
                if text is not None:
                    files["LICENSE"] = text
                repo = self.repo(files, commit=False, name="r_" + label)
                self.assertEqual(self.run_lic(repo).returncode, 1)

    def test_forbidden_names_are_found_in_files_and_folders(self):
        names = ["x_mautrix_y.py", "whatsmeow/main.go", "lib/Baileys/index.js", "bin/imsg",
                 "bin/imsg-wrapper.sh"]
        for rel in names:
            with self.subTest(rel):
                out = self.run_lic(self.lic_repo({rel: "x\n"}))
                self.assertEqual(out.returncode, 1, rel)
            shutil.rmtree(os.path.join(self.tmp, "repo"))

    def test_similar_but_different_names_pass(self):
        repo = self.lic_repo({"imsgx/a.py": "x\n", "notes/message.py": "x\n"})
        self.assertEqual(self.run_lic(repo).returncode, 0)

    def test_channel_docs_may_carry_the_names(self):
        repo = self.lic_repo({"channels/wa/docs/whatsmeow-notes.md": "x\n",
                              "channels/wa/docs/imsg/readme.md": "x\n"})
        self.assertEqual(self.run_lic(repo).returncode, 0)
        repo2 = self.lic_repo({"channels/wa/whatsmeow-notes.md": "x\n"})
        self.assertEqual(self.run_lic(repo2).returncode, 1)

    def test_native_binaries_are_found_by_magic_bytes_whatever_the_name(self):
        samples = {"macho64": b"\xcf\xfa\xed\xfe" + b"\0" * 20,
                   "macho32": b"\xce\xfa\xed\xfe" + b"\0" * 20,
                   "machofat": b"\xca\xfe\xba\xbe\x00\x00\x00\x02" + b"\0" * 20,
                   "elf": b"\x7fELF" + b"\x02\x01\x01" + b"\0" * 20}
        for label, data in samples.items():
            with self.subTest(label):
                repo = self.lic_repo({"data/notes.txt": data}, None)
                out = self.run_lic(repo)
                self.assertEqual(out.returncode, 1, label)
                self.assertIn("data/notes.txt", out.stdout)
            shutil.rmtree(os.path.join(self.tmp, "repo"))

    def test_java_class_file_is_not_mistaken_for_a_fat_binary(self):
        repo = self.lic_repo({"A.class": b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\0" * 20})
        self.assertEqual(self.run_lic(repo).returncode, 0)

    def lockrepo(self, packages, exceptions=None, name="repo"):
        files = {"channels/x/package-lock.json": lock(packages), "LICENSE": self.mit()}
        if exceptions is not None:
            files["tools/license-exceptions.json"] = json.dumps(exceptions)
        return self.repo(files, commit=False, name=name)

    def test_allowed_licenses_pass_and_are_counted(self):
        pk = {"node_modules/a": {"license": "MIT"}, "node_modules/b": {"license": "ISC"},
              "node_modules/c": {"license": "(MIT OR GPL-3.0-only)"},
              "node_modules/@s/d": {"license": "Apache-2.0"}}
        out = self.run_lic(self.lockrepo(pk))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.strip(), "licenses ok: 4 packages")

    def test_disallowed_missing_or_and_combined_licenses_fail(self):
        for label, entry in (("gpl", {"license": "GPL-3.0-only"}), ("none", {}),
                             ("and", {"license": "(MIT AND GPL-3.0-only)"}),
                             ("obj", {"license": {"type": "GPL-2.0"}})):
            with self.subTest(label):
                repo = self.lockrepo({"node_modules/bad": entry}, name="r_" + label)
                out = self.run_lic(repo)
                self.assertEqual(out.returncode, 1, label)
                self.assertIn("bad", out.stdout)

    def test_exception_with_a_reason_passes_and_without_one_fails(self):
        pk = {"node_modules/odd": {"license": "LGPL-3.0"}}
        ok = self.run_lic(self.lockrepo(pk, {"odd": "dynamic link only, reviewed"}))
        self.assertEqual(ok.returncode, 0, ok.stdout)
        bad = self.run_lic(self.lockrepo(pk, {"odd": ""}, name="r2"))
        self.assertEqual(bad.returncode, 1)

    def test_lockfile_v1_without_license_data_exits_2(self):
        files = {"channels/x/package-lock.json": json.dumps(
            {"lockfileVersion": 1, "dependencies": {"a": {"version": "1.0.0"}}}),
            "LICENSE": self.mit()}
        self.assertEqual(self.run_lic(self.repo(files, commit=False)).returncode, 2)

    def test_malformed_lockfile_exits_2(self):
        files = {"channels/x/package-lock.json": "{nope", "LICENSE": self.mit()}
        self.assertEqual(self.run_lic(self.repo(files, commit=False)).returncode, 2)

    def test_lock_files_inside_node_modules_are_ignored(self):
        repo = self.lic_repo({"channels/x/node_modules/q/package-lock.json":
                              lock({"node_modules/z": {"license": "GPL-3.0"}})})
        self.assertEqual(self.run_lic(repo).returncode, 0)

    def test_not_a_git_repo_exits_2(self):
        plain = self.mkdir("plain")
        self.put(plain, "LICENSE", self.mit())
        self.assertEqual(self.run_lic(plain).returncode, 2)


# --------------------------------------------------------------------------- check_config_keys
class ConfigKeys(Base):
    EXAMPLE = json.dumps({"data_dir": "", "poll": 5, "ext": 1, "_external_keys": ["ext"]})

    def keys_repo(self, source, example=None, extra=None, name="core/m.py"):
        files = {name: source, "config.example.json": example or self.EXAMPLE}
        files.update(extra or {})
        return self.repo(files, commit=False)

    def run_keys(self, repo):
        return self.tool("check_config_keys.py", "--repo", repo)

    def test_all_keys_known_and_used_passes_with_exact_line(self):
        repo = self.keys_repo(dedent("""
            import kitconfig
            a = kitconfig.get("data_dir")
            b = kitconfig.require("poll", 3)
            """))
        out = self.run_keys(repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.strip(), "config keys ok: 3 keys")

    def test_a_module_loaded_by_name_still_counts_as_using_its_keys(self):
        repo = self.keys_repo('kc = _comms_module("kitconfig")\nkc.get("data_dir")\nkc.get("poll")\n')
        out = self.run_keys(repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_a_helper_that_returns_the_module_counts_too(self):
        repo = self.keys_repo('_kitconfig().path("data_dir")\n_kitconfig().path("poll")\n')
        out = self.run_keys(repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_a_key_read_through_a_loaded_module_must_be_in_the_example(self):
        repo = self.keys_repo('kc = _comms_module("kitconfig")\nkc.get("nope")\n'
                              'kc.get("data_dir")\nkc.get("poll")\n')
        out = self.run_keys(repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("nope", out.stdout)

    WRAPPER = ('def _conf(key, default=None):\n'
               '    kc = _comms_module("kitconfig")\n'
               '    return kc.get(key, default)\n')

    def test_a_small_wrapper_is_an_accessor_and_its_callers_count(self):
        repo = self.keys_repo(self.WRAPPER + '_conf("data_dir")\n_conf("poll", 3)\n')
        out = self.run_keys(repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_a_wrapper_call_with_a_computed_key_is_a_defect(self):
        repo = self.keys_repo(self.WRAPPER + 'n = "data_dir"\n_conf(n)\n_conf("data_dir")\n_conf("poll")\n')
        out = self.run_keys(repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("not a string literal", out.stdout)

    def test_a_wrapper_call_with_an_unknown_key_is_a_defect(self):
        repo = self.keys_repo(self.WRAPPER + '_conf("nope")\n_conf("data_dir")\n_conf("poll")\n')
        out = self.run_keys(repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("nope", out.stdout)

    def test_non_literal_keys_are_defects_with_their_line(self):
        for expr in ("name", "'a' + name", "f'x{name}'", "'k_%s' % name"):
            with self.subTest(expr):
                repo = self.keys_repo("import kitconfig\nname = 'a'\n"
                                      "kitconfig.get(data_dir_name)\n"
                                      .replace("data_dir_name", expr)
                                      + "kitconfig.get('data_dir')\nkitconfig.get('poll')\n",
                                      name="core/m.py")
                out = self.run_keys(repo)
                self.assertEqual(out.returncode, 1, expr)
                self.assertIn("core/m.py:3", out.stdout)
            shutil.rmtree(os.path.join(self.tmp, "repo"))

    def test_unknown_key_is_a_defect_for_every_accessor(self):
        for fn in ("get", "require", "path", "get_list", "set_value"):
            with self.subTest(fn):
                repo = self.keys_repo("import kitconfig\nkitconfig.%s('nope', 1)\n"
                                      "kitconfig.get('data_dir')\nkitconfig.get('poll')\n" % fn)
                out = self.run_keys(repo)
                self.assertEqual(out.returncode, 1, fn)
                self.assertIn("core/m.py:2", out.stdout)
            shutil.rmtree(os.path.join(self.tmp, "repo"))

    def test_unused_example_key_is_a_defect_unless_listed_external(self):
        repo = self.keys_repo("import kitconfig\nkitconfig.get('data_dir')\n")
        out = self.run_keys(repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("poll", out.stdout)
        self.assertNotIn("ext\n", out.stdout)

    def test_calls_on_other_objects_are_ignored(self):
        repo = self.keys_repo("import kitconfig\nd = {}\nd.get('zzz')\nx.require('qq')\n"
                              "kitconfig.get('data_dir')\nkitconfig.get('poll')\n")
        self.assertEqual(self.run_keys(repo).returncode, 0)

    def test_alias_and_from_import_forms_are_followed(self):
        repo = self.keys_repo(dedent("""
            import kitconfig as kc
            from kitconfig import get
            kc.get("nope1")
            get("nope2")
            """))
        out = self.run_keys(repo)
        self.assertIn("core/m.py:3", out.stdout)
        self.assertIn("core/m.py:4", out.stdout)

    def test_keyword_key_is_read(self):
        repo = self.keys_repo("import kitconfig\nkitconfig.get(key='nope')\n"
                              "kitconfig.get('data_dir')\nkitconfig.get('poll')\n")
        out = self.run_keys(repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("core/m.py:2", out.stdout)

    def test_test_files_are_not_scanned(self):
        repo = self.keys_repo("import kitconfig\nkitconfig.get('data_dir')\n"
                              "kitconfig.get('poll')\n",
                              extra={"core/test_m.py": "import kitconfig\nkitconfig.get('zz')\n",
                                     "channels/wa/tests/test_q.py":
                                         "import kitconfig\nkitconfig.get(v)\n"})
        self.assertEqual(self.run_keys(repo).returncode, 0)

    def test_nested_keys_use_dotted_paths(self):
        example = json.dumps({"slack": {"token": "", "chan": ""}})
        repo = self.keys_repo("import kitconfig\nkitconfig.get('slack.token')\n", example)
        out = self.run_keys(repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("slack.chan", out.stdout)
        repo2 = self.keys_repo("import kitconfig\nkitconfig.get('slack.token')\n"
                               "kitconfig.get('slack.chan')\n", example, name="core/n.py")
        self.assertEqual(self.run_keys(repo2).stdout.strip(), "config keys ok: 2 keys")

    def test_no_example_file_or_no_python_exits_2(self):
        repo = self.repo({"core/m.py": "x = 1\n"}, commit=False)
        self.assertEqual(self.run_keys(repo).returncode, 2)
        repo2 = self.repo({"config.example.json": "{}", "README.md": "x"}, commit=False,
                          name="r2")
        self.assertEqual(self.run_keys(repo2).returncode, 2)

    def test_unparseable_source_exits_2(self):
        repo = self.keys_repo("def broken(:\n")
        self.assertEqual(self.run_keys(repo).returncode, 2)


# --------------------------------------------------------------------------- live_proof
class LiveProof(Base):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.tmp, "state", "baseline.json")
        self.flag = os.path.join(self.tmp, "flag")
        self.count = os.path.join(self.tmp, "count")

    def cmd(self, name, code, expect=None, timeout=30):
        c = {"name": name, "argv": [sys.executable, "-c", code], "timeout": timeout}
        if expect:
            c["expect"] = expect
        return c

    def spec(self, commands):
        return self.put(self.tmp, "spec.json", json.dumps({"commands": commands}))

    def run_proof(self, spec, mode):
        return self.tool("live_proof.py", mode, "--spec", spec, "--baseline-file", self.state)

    def exit_cmd(self):
        return self.cmd("probe", "import os,sys; sys.exit(1 if os.path.exists(%r) else 0)"
                        % self.flag)

    def test_same_results_print_the_exact_line(self):
        spec = self.spec([self.exit_cmd(), self.cmd("two", "print('hello')", "hel+o")])
        self.assertEqual(self.run_proof(spec, "--baseline").returncode, 0)
        out = self.run_proof(spec, "--compare")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "live same as baseline: 2 commands")

    def test_changed_exit_code_fails_and_names_the_command(self):
        spec = self.spec([self.exit_cmd()])
        self.run_proof(spec, "--baseline")
        open(self.flag, "w").close()
        out = self.run_proof(spec, "--compare")
        self.assertEqual(out.returncode, 1)
        self.assertIn("probe", out.stdout)
        self.assertIn("exit code", out.stdout)

    def test_expect_that_stops_matching_fails(self):
        code = "import os; print('ready' if not os.path.exists(%r) else 'broken')" % self.flag
        spec = self.spec([self.cmd("ex", code, "^ready$")])
        self.run_proof(spec, "--baseline")
        open(self.flag, "w").close()
        out = self.run_proof(spec, "--compare")
        self.assertEqual(out.returncode, 1)
        self.assertIn("expect", out.stdout)

    def counted(self, line):
        return self.cmd("suite", "print(%r %% int(open(%r).read()))" % (line, self.count))

    def test_dropped_test_count_fails_and_new_tests_are_fine(self):
        for line in ("Ran %d tests in 0.1s", "# pass %d"):
            with self.subTest(line):
                spec = self.spec([self.counted(line)])
                with open(self.count, "w") as fh:
                    fh.write("10")
                self.assertEqual(self.run_proof(spec, "--baseline").returncode, 0)
                with open(self.count, "w") as fh:
                    fh.write("12")
                self.assertEqual(self.run_proof(spec, "--compare").returncode, 0)
                with open(self.count, "w") as fh:
                    fh.write("9")
                out = self.run_proof(spec, "--compare")
                self.assertEqual(out.returncode, 1)
                self.assertIn("test count", out.stdout)

    def test_a_count_that_disappears_counts_as_a_drop(self):
        code = "import os; print('' if os.path.exists(%r) else 'Ran 4 tests')" % self.flag
        spec = self.spec([self.cmd("c", code)])
        self.run_proof(spec, "--baseline")
        open(self.flag, "w").close()
        self.assertEqual(self.run_proof(spec, "--compare").returncode, 1)

    def test_command_output_never_prints_or_gets_stored(self):
        secret = "PRIVATE-MESSAGE-BODY"
        spec = self.spec([self.cmd("noisy", "import sys; print(%r); print(%r, file=sys.stderr)"
                                   % (secret, secret), secret)])
        out = self.run_proof(spec, "--baseline")
        out2 = self.run_proof(spec, "--compare")
        for text in (out.stdout, out.stderr, out2.stdout, out2.stderr):
            self.assertNotIn(secret, text)
        with open(self.state) as fh:
            self.assertNotIn(secret, fh.read())
        self.assertEqual(stat_mode(self.state), 0o600)

    def test_a_hanging_command_fails_the_compare_without_hanging_the_tool(self):
        code = "import os,time; time.sleep(30 if os.path.exists(%r) else 0)" % self.flag
        spec = self.spec([self.cmd("hang", code, timeout=2)])
        self.run_proof(spec, "--baseline")
        open(self.flag, "w").close()
        out = self.run_proof(spec, "--compare")
        self.assertEqual(out.returncode, 1)
        self.assertIn("hang", out.stdout)

    def test_missing_baseline_zero_commands_or_bad_spec_exit_2(self):
        spec = self.spec([self.exit_cmd()])
        self.assertEqual(self.run_proof(spec, "--compare").returncode, 2)
        empty = self.put(self.tmp, "empty.json", '{"commands": []}')
        self.assertEqual(self.run_proof(empty, "--baseline").returncode, 2)
        self.assertEqual(self.run_proof(os.path.join(self.tmp, "none.json"),
                                        "--baseline").returncode, 2)

    def test_baseline_refuses_to_store_when_expect_does_not_match(self):
        spec = self.spec([self.cmd("e", "print('x')", "never-matches")])
        out = self.run_proof(spec, "--baseline")
        self.assertEqual(out.returncode, 1)
        self.assertFalse(os.path.exists(self.state))

    def test_command_added_after_the_baseline_is_a_defect(self):
        self.run_proof(self.spec([self.exit_cmd()]), "--baseline")
        spec2 = self.spec([self.exit_cmd(), self.cmd("newone", "pass")])
        out = self.run_proof(spec2, "--compare")
        self.assertEqual(out.returncode, 1)
        self.assertIn("newone", out.stdout)

    def test_command_removed_from_the_spec_is_a_defect(self):
        self.run_proof(self.spec([self.exit_cmd(), self.cmd("two", "pass")]), "--baseline")
        out = self.run_proof(self.spec([self.exit_cmd()]), "--compare")
        self.assertEqual(out.returncode, 1)
        self.assertIn("two", out.stdout)

    def test_program_that_does_not_exist_is_recorded_not_a_crash(self):
        spec = self.spec([{"name": "ghost", "argv": ["/nonexistent/prog-xyz"], "timeout": 5}])
        self.assertEqual(self.run_proof(spec, "--baseline").returncode, 0)
        self.assertEqual(self.run_proof(spec, "--compare").returncode, 0)


def stat_mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


if __name__ == "__main__":
    unittest.main()
