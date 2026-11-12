#!/usr/bin/env python3
"""tools/bridge_scan.py decides whether a bridge image is safe to run. Ways it can fail:

  1. a binary built from another commit passes
  2. a binary that links a module whose hash is not in go.sum (a swapped dependency) passes
  3. a binary whose main package is another program passes
  4. a zip that differs by one byte gets the same h1 hash, so a swapped module zip passes
  5. the pattern scan misses os/exec or an IP literal in a package the main program links
  6. the pattern scan reads a package the main program does not link (noise that hides real findings)
  7. a go.mod with no module line crashes instead of failing
  8. one network reset fails the whole scan, or a 404 is retried as if it were a network error

All offline: the network checks (sumdb, zips, OSV) are skipped with --offline.
"""
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("bridge_scan", os.path.join(HERE, "..", "tools", "bridge_scan.py"))
assert spec is not None and spec.loader is not None
BS = importlib.util.module_from_spec(spec)
spec.loader.exec_module(BS)

MOD = "example.com/bridge"
MAIN = MOD + "/cmd/bridge"
COMMIT = "a" * 40
DEP_HASH = "h1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def buildinfo(path=MAIN, commit=COMMIT, dep_hash=DEP_HASH):
    return (f"bridge.bin: go1.25.0\n\tpath\t{path}\n\tmod\t{MOD}\t(devel)\t\n"
            f"\tdep\texample.com/dep\tv1.2.3\t{dep_hash}\n"
            f"\tbuild\tvcs.revision={commit}\n\tbuild\tvcs.modified=false\n")


class Checkout:
    def __init__(self, tmp, module_line=True):
        self.root = os.path.join(tmp, "src")
        files = {
            "go.mod": (f"module {MOD}\n" if module_line else "go 1.25\n"),
            "go.sum": f"example.com/dep v1.2.3 {DEP_HASH}\nexample.com/dep v1.2.3/go.mod h1:x=\n",
            "cmd/bridge/main.go": f'package main\n\nimport (\n\t"os/exec"\n\t"{MOD}/pkg/net"\n)\n\n'
                                  'func main() { exec.Command("true"); net.Dial() }\n',
            "pkg/net/net.go": 'package net\n\nfunc Dial() string { return "10.20.30.40:443" }\n',
            "pkg/unused/unused.go": 'package unused\n\nfunc X() string { return "203.0.113.9:22" }\n',
        }
        for rel, body in files.items():
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)

    def bi(self, tmp, **kw):
        path = os.path.join(tmp, "bi.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(buildinfo(**kw))
        return path


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = BS.main(argv)
    return code, out.getvalue()


class BuildInfo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.co = Checkout(self.tmp)

    def scan(self, **kw):
        return run([self.co.root, "--main", MAIN, "--buildinfo", self.co.bi(self.tmp, **kw),
                    "--commit", COMMIT, "--offline", "--report", os.path.join(self.tmp, "r.json")])

    def test_a_matching_binary_passes_and_says_the_network_checks_were_skipped(self):
        code, out = self.scan()
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT: PASS", out)
        self.assertIn("NOT checked", out)

    def test_another_commit_fails(self):
        code, out = self.scan(commit="b" * 40)
        self.assertEqual(code, 1)
        self.assertIn("vcs.revision", out)
        self.assertIn("RESULT: FAIL", out)

    def test_a_swapped_dependency_fails(self):
        code, out = self.scan(dep_hash="h1:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=")
        self.assertEqual(code, 1)
        self.assertIn("example.com/dep v1.2.3 hash not in go.sum", out)

    def test_another_main_program_fails(self):
        code, out = self.scan(path=MOD + "/cmd/other")
        self.assertEqual(code, 1)
        self.assertIn("main package", out)


class Patterns(unittest.TestCase):
    def test_exec_and_ip_in_linked_code_are_found_and_unlinked_code_is_not_read(self):
        tmp = tempfile.mkdtemp()
        co = Checkout(tmp)
        report = os.path.join(tmp, "r.json")
        code, out = run([co.root, "--main", MAIN, "--buildinfo", co.bi(tmp), "--offline", "--report", report])
        self.assertEqual(code, 0, out)
        import json
        with open(report, encoding="utf-8") as fh:
            found = json.load(fh)["found"]
        joined = json.dumps(found)
        self.assertTrue(found.get("exec"), out)
        self.assertIn("10.20.30.40", joined)
        self.assertNotIn("203.0.113.9", joined)

    def test_a_go_mod_without_a_module_line_fails_cleanly(self):
        tmp = tempfile.mkdtemp()
        co = Checkout(tmp, module_line=False)
        code, out = run([co.root, "--main", MAIN, "--offline"])
        self.assertEqual(code, 1)
        self.assertIn("RESULT: FAIL", out)


class ZipHash(unittest.TestCase):
    @staticmethod
    def zip_of(files):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, body in files.items():
                zf.writestr(name, body)
        return buf.getvalue()

    def test_one_changed_byte_changes_the_hash(self):
        clean = self.zip_of({"m@v1/a.go": "package a\n", "m@v1/b.go": "package b\n"})
        swapped = self.zip_of({"m@v1/a.go": "package a\n", "m@v1/b.go": "package c\n"})
        self.assertNotEqual(BS.h1_of_zip(clean), BS.h1_of_zip(swapped))

    def test_an_added_file_changes_the_hash_and_file_order_does_not(self):
        a = self.zip_of({"m@v1/a.go": "x", "m@v1/b.go": "y"})
        b = self.zip_of({"m@v1/b.go": "y", "m@v1/a.go": "x"})
        c = self.zip_of({"m@v1/a.go": "x", "m@v1/b.go": "y", "m@v1/c.go": ""})
        self.assertEqual(BS.h1_of_zip(a), BS.h1_of_zip(b))
        self.assertNotEqual(BS.h1_of_zip(a), BS.h1_of_zip(c))
        self.assertTrue(BS.h1_of_zip(a).startswith("h1:"))


class Download(unittest.TestCase):
    def setUp(self):
        self.calls = 0
        p = mock.patch.object(BS, "RETRY_SLEEP", 0)
        p.start()
        self.addCleanup(p.stop)

    def urlopen(self, failures):
        test = self

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"ok"

        def fake(req, timeout=None):
            test.calls += 1
            if test.calls <= len(failures):
                raise failures[test.calls - 1]
            return Resp()
        return mock.patch.object(BS.urllib.request, "urlopen", fake)

    def test_a_reset_is_retried(self):
        with self.urlopen([ConnectionResetError(54, "Connection reset by peer")]):
            self.assertEqual(BS.http_get("https://proxy.example/x.zip"), b"ok")
        self.assertEqual(self.calls, 2)

    def test_a_404_is_not_retried(self):
        err = urllib.error.HTTPError("https://proxy.example/x.zip", 404, "Not Found", {}, None)
        with self.urlopen([err]), self.assertRaises(urllib.error.HTTPError):
            BS.http_get("https://proxy.example/x.zip")
        self.assertEqual(self.calls, 1)

    def test_three_resets_give_up(self):
        resets = [ConnectionResetError(54, "reset")] * 3
        with self.urlopen(resets), self.assertRaises(ConnectionResetError):
            BS.http_get("https://proxy.example/x.zip")
        self.assertEqual(self.calls, 3)


if __name__ == "__main__":
    unittest.main()
