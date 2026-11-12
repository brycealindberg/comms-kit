#!/usr/bin/env python3
"""Supply-chain and malware-pattern scan for a mautrix Go bridge.

Reads a source checkout as data only. It never builds or runs code from it.

Checks:
  1. buildinfo  (optional, --buildinfo FILE from `go version -m BINARY`):
                the binary's main path, commit and every linked module hash
                match the checkout's go.sum.
  2. sumdb      every go.sum line matches sum.golang.org.
  3. zips       for the --review modules: the proxy.golang.org zip hashes to
                the go.sum h1: value; the zip source joins the pattern scan.
  4. osv        known vulnerabilities (api.osv.dev) for the linked modules.
  5. patterns   in the checkout's own packages that the --main package links,
                plus the --review module sources: hostnames outside the
                allowlist, IP literals, os/exec, home-dir/ssh/env reads,
                go:linkname, plugin, go:embed, long base64/hex blobs, init().

Exit 0 = PASS (no hard failure; findings to read may remain), 1 = FAIL.

Run it again for every bridge update, before the new image runs. Example (mautrix-instagram):
  git clone https://github.com/mautrix/meta src && git -C src checkout <tag>
  docker create --name ig <image@digest> && docker cp ig:/usr/bin/mautrix-meta ig.bin   # then: go version -m ig.bin > bi.txt
  python3 -I tools/bridge_scan.py src --main go.mau.fi/mautrix-meta/cmd/mautrix-instagram \
      --buildinfo bi.txt --commit <tag commit> --review go.mau.fi/ --report report.json
Run `go version -m` in a throwaway container, never on the binary's own host. --offline skips the three
network checks (2-4); use it only for tests.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "comms-kit-bridge-scan/1"}

# Hosts a Meta/Instagram/LinkedIn bridge is expected to name. A host that ends
# in one of these (or equals it) is not a finding.
ALLOW_SUFFIXES = (
    # Meta / Instagram / Messenger / WhatsApp
    "instagram.com", "cdninstagram.com", "facebook.com", "facebook.net", "fbcdn.net",
    "fbsbx.com", "fb.com", "fb.me", "messenger.com", "m.me", "whatsapp.net", "whatsapp.com",
    "wa.me", "meta.com", "meta.ai", "threads.net", "threads.com", "oculus.com", "workplace.com",
    "facebookmail.com", "instagr.am", "ig.me", "metacdn.com",
    # LinkedIn
    "linkedin.com", "licdn.com", "lnkd.in",
    # mautrix / Matrix / Beeper
    "mau.fi", "maunium.net", "mau.dev", "matrix.org", "matrix.to", "beeper.com", "beeper-tools.com",
    # Push, standards, docs and common libraries
    "googleapis.com", "google.com", "gstatic.com", "android.com", "firebaseio.com",
    "apple.com", "icloud.com", "w3.org", "ietf.org", "github.com", "githubusercontent.com",
    "golang.org", "go.dev", "example.com", "example.org", "example.net", "localhost",
    "mozilla.org", "wikipedia.org", "schema.org", "json-schema.org", "spdx.org",
)
TLDS = (r"com|net|org|io|fi|me|dev|app|ai|co|tv|ly|am|in|to|xyz|ru|cn|su|top|info|biz|cc|"
        r"ws|pw|tk|onion|link|site|online|club|vip|shop|live|cloud|host|world")
HOST_RE = re.compile(r"(?<![A-Za-z0-9_.-])((?:[a-z0-9-]+\.)+(?:%s))(?![A-Za-z0-9-])" % TLDS, re.I)
URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://([^/\"'`\s:?#]+)", re.I)
IP_RE = re.compile(r"(?<![\d.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\d.])")
STR_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"|`[^`]*`')
BLOB_RE = re.compile(r"^[A-Za-z0-9+/=_-]{200,}$")
IMPORT_BLOCK = re.compile(r"^import\s*\((.*?)^\)", re.S | re.M)
IMPORT_LINE = re.compile(r'^import\s+(?:[\w.]+\s+)?"([^"]+)"', re.M)
QUOTED = re.compile(r'"([^"]+)"')

RISKY = [
    ("exec", re.compile(r'"os/exec"|syscall\.Exec|syscall\.ForkExec')),
    ("homedir/ssh", re.compile(r"os\.UserHomeDir|\.ssh/|id_rsa|id_ed25519|\.aws/|\.kube/|keychain", re.I)),
    ("env-dump", re.compile(r"os\.Environ\(\)")),
    ("linkname", re.compile(r"//go:linkname")),
    ("plugin", re.compile(r'"plugin"')),
    ("embed", re.compile(r"//go:embed")),
    ("init", re.compile(r"^func init\(\)", re.M)),
]


RETRY_SLEEP = 2.0


def http_get(url: str, timeout: int = 60, tries: int = 3) -> bytes:
    """GET with retries on a network error or a 5xx. A 4xx is an answer, so it is not retried."""
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == tries:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == tries:
                raise
        time.sleep(RETRY_SLEEP * attempt)
    raise RuntimeError("unreachable")


def escape(path: str) -> str:
    """Go module path escaping: an upper-case letter becomes '!' + lower case."""
    return "".join("!" + c.lower() if c.isupper() else c for c in path)


def read_gosum(path: str) -> dict[tuple[str, str], str]:
    out = {}
    for line in open(path, encoding="utf-8"):
        parts = line.split()
        if len(parts) == 3:
            out[(parts[0], parts[1])] = parts[2]
    return out


def h1_of_zip(data: bytes) -> str:
    """golang.org/x/mod/sumdb/dirhash Hash1 over the zip's file names."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = sorted(n for n in zf.namelist() if not n.endswith("/"))
    summary = "".join(f"{hashlib.sha256(zf.read(n)).hexdigest()}  {n}\n" for n in names)
    return "h1:" + base64.b64encode(hashlib.sha256(summary.encode()).digest()).decode()


# ---------- 1. build info ----------
def parse_buildinfo(path: str) -> dict:
    info = {"path": None, "deps": [], "revision": None, "modified": None, "replaced": []}
    for raw in open(path, encoding="utf-8", errors="replace"):
        p = raw.strip().split("\t")
        if p[0] == "path" and len(p) > 1:
            info["path"] = p[1]
        elif p[0] in ("dep", "=>") and len(p) >= 4:
            info["deps"].append((p[1], p[2], p[3]))
            if p[0] == "=>":
                info["replaced"].append(p[1])
        elif p[0] == "build" and len(p) > 1:
            if p[1].startswith("vcs.revision="):
                info["revision"] = p[1].split("=", 1)[1]
            elif p[1].startswith("vcs.modified="):
                info["modified"] = p[1].split("=", 1)[1]
    return info


# ---------- 2. sumdb ----------
def sumdb_lookup(mod: str, ver: str) -> dict[str, str]:
    v = ver[:-len("/go.mod")] if ver.endswith("/go.mod") else ver
    body = http_get(f"https://sum.golang.org/lookup/{escape(mod)}@{escape(v)}").decode()
    out = {}
    for line in body.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == mod:
            out[parts[1]] = parts[2]
    return out


# ---------- 5. patterns ----------
def go_files(d: str) -> list[str]:
    try:
        return [os.path.join(d, f) for f in os.listdir(d)
                if f.endswith(".go") and not f.endswith("_test.go")]
    except FileNotFoundError:
        return []


def imports_of(src: str) -> list[str]:
    found = IMPORT_LINE.findall(src)
    for block in IMPORT_BLOCK.findall(src):
        found += QUOTED.findall(block)
    return found


def linked_local_dirs(root: str, module: str, main_pkg: str) -> list[str]:
    start = main_pkg[len(module):].lstrip("/")
    seen, todo = set(), [start]
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        for f in go_files(os.path.join(root, rel)):
            for imp in imports_of(open(f, encoding="utf-8", errors="replace").read()):
                if imp == module or imp.startswith(module + "/"):
                    todo.append(imp[len(module):].lstrip("/"))
    return sorted(os.path.join(root, r) for r in seen)


def allowed(host: str) -> bool:
    h = host.lower().rstrip(".")
    return any(h == s or h.endswith("." + s) for s in ALLOW_SUFFIXES)


def scan_sources(files: list[tuple[str, str]]) -> dict[str, list[str]]:
    """files: (label, text). Returns finding kind -> list of 'label:line: text'."""
    found: dict[str, list[str]] = {}

    def add(kind, label, src, pos, text):
        line = src.count("\n", 0, pos) + 1
        found.setdefault(kind, []).append(f"{label}:{line}: {text[:160]}")

    for label, src in files:
        for kind, rx in RISKY:
            for m in rx.finditer(src):
                end = src.find("\n", m.start())
                add(kind, label, src, m.start(), src[m.start():end if end > 0 else None].strip())
        for m in STR_RE.finditer(src):
            s = m.group(0)[1:-1]
            hosts = set(URL_RE.findall(s)) | set(HOST_RE.findall(s))
            for h in hosts:
                h = h.split("@")[-1]
                if "." in h and not allowed(h) and not re.fullmatch(r"[\d.]+", h):
                    add("host", label, src, m.start(), h)
            for ip in IP_RE.findall(s):
                if not ip.startswith(("0.", "127.", "255.")) and all(int(x) < 256 for x in ip.split(".")):
                    add("ip", label, src, m.start(), ip)
            if BLOB_RE.match(s):
                add("blob", label, src, m.start(), f"{len(s)} chars: {s[:60]}...")
    return found


def buildinfo_fails(bi: dict, main_pkg: str, commit, gosum: dict) -> list[str]:
    """Hard failures from `go version -m` output against the checkout's go.sum."""
    fails = []
    if bi["path"] != main_pkg:
        fails.append(f"buildinfo: binary main package is {bi['path']}, expected {main_pkg}")
    if commit and bi["revision"] != commit:
        fails.append(f"buildinfo: vcs.revision {bi['revision']} != {commit}")
    for m, v, h in bi["deps"]:
        if gosum.get((m, v)) != h:
            fails.append(f"buildinfo: linked {m} {v} hash not in go.sum")
    return fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("checkout", help="source checkout (data only; nothing in it is run)")
    ap.add_argument("--main", required=True, help="main package import path, e.g. go.mau.fi/mautrix-meta/cmd/mautrix-instagram")
    ap.add_argument("--buildinfo", help="output of `go version -m` on the image binary")
    ap.add_argument("--commit", help="commit the binary must be built from")
    ap.add_argument("--review", nargs="*", default=[], help="module path prefixes whose source joins the pattern scan")
    ap.add_argument("--report", help="write findings as JSON here")
    ap.add_argument("--offline", action="store_true", help="skip sumdb, zips and OSV (tests only)")
    a = ap.parse_args(argv)

    fails: list[str] = []
    notes: list[str] = []
    root = os.path.abspath(a.checkout)
    with open(os.path.join(root, "go.mod"), encoding="utf-8") as fh:
        mod_line = re.search(r"^module\s+(\S+)", fh.read(), re.M)
    if mod_line is None:
        print("FAIL go.mod has no module line\nRESULT: FAIL")
        return 1
    module = mod_line.group(1)
    gosum = read_gosum(os.path.join(root, "go.sum"))
    print(f"module {module}: {len(gosum)} go.sum lines")

    # 1. build info
    linked: list[tuple[str, str]] = []
    if a.buildinfo:
        bi = parse_buildinfo(a.buildinfo)
        bi_fails = buildinfo_fails(bi, a.main, a.commit, gosum)
        fails += bi_fails
        bad = [f for f in bi_fails if f.startswith("buildinfo: linked ")]
        linked = [(m, v) for m, v, _ in bi["deps"]]
        print(f"[1] buildinfo: main={bi['path']} revision={bi['revision']} modified={bi['modified']} "
              f"linked={len(linked)} mismatches={len(bad)}")
        if bi["modified"] == "true":
            notes.append("buildinfo: vcs.modified=true (the CI build dir had untracked files, e.g. its .cache)")
    else:
        mods = sorted({(m, v) for (m, v) in gosum if not v.endswith("/go.mod")})
        linked = mods
        print(f"[1] buildinfo: skipped (no binary); treating all {len(linked)} go.sum modules as linked")

    sources: list[tuple[str, str]] = []
    vulns: dict[str, list[str]] = {}
    if a.offline:
        notes.append("offline: sumdb, zip hashes and OSV were NOT checked")
        print("[2-4] offline: sumdb, zips and OSV skipped")
    else:
        # 2. sumdb
        pairs = sorted({(m, v[:-7] if v.endswith("/go.mod") else v) for (m, v) in gosum})
        def check(pair):
            m, v = pair
            try:
                return pair, sumdb_lookup(m, v), None
            except Exception as e:  # noqa: BLE001 - report every lookup error
                return pair, {}, str(e)
        mism = 0
        with ThreadPoolExecutor(12) as ex:
            for (m, v), got, err in ex.map(check, pairs):
                if err:
                    fails.append(f"sumdb: {m}@{v}: lookup failed: {err}")
                    continue
                for key in (v, v + "/go.mod"):
                    if (m, key) in gosum and got.get(key) != gosum[(m, key)]:
                        mism += 1
                        fails.append(f"sumdb: {m} {key}: go.sum {gosum[(m, key)]} != sumdb {got.get(key)}")
        print(f"[2] sumdb: {len(pairs)} module versions checked, mismatches={mism}")

        # 3. review zips
        review = [(m, v) for (m, v) in linked if any(m == p or m.startswith(p) for p in a.review)]
        def fetch(pair):
            m, v = pair
            try:
                data = http_get(f"https://proxy.golang.org/{escape(m)}/@v/{escape(v)}.zip", timeout=120)
                return pair, data, None
            except Exception as e:  # noqa: BLE001
                return pair, b"", str(e)
        with ThreadPoolExecutor(6) as ex:
            for (m, v), data, err in ex.map(fetch, review):
                if err:
                    fails.append(f"zip: {m}@{v}: download failed: {err}")
                    continue
                h = h1_of_zip(data)
                if gosum.get((m, v)) != h:
                    fails.append(f"zip: {m}@{v}: zip hash {h} != go.sum {gosum.get((m, v))}")
                zf = zipfile.ZipFile(io.BytesIO(data))
                for n in zf.namelist():
                    if n.endswith(".go") and not n.endswith("_test.go") and "/testdata/" not in n \
                            and "/_examples/" not in n and "/example/" not in n:
                        sources.append((n, zf.read(n).decode("utf-8", "replace")))
        print(f"[3] zips: {len(review)} review modules downloaded and hash-checked")

        # 4. OSV
        q = {"queries": [{"package": {"name": m, "ecosystem": "Go"}, "version": v[1:] if v.startswith("v") else v} for m, v in linked]}
        try:
            req = urllib.request.Request("https://api.osv.dev/v1/querybatch", data=json.dumps(q).encode(),
                                         headers={**UA, "Content-Type": "application/json"})
            res = json.loads(urllib.request.urlopen(req, timeout=60).read())
            for (m, v), r in zip(linked, res.get("results", [])):
                ids = [x["id"] for x in r.get("vulns", [])]
                if ids:
                    vulns[f"{m}@{v}"] = ids
            print(f"[4] osv: {len(linked)} modules queried, with known vulns: {len(vulns)}")
            for k, ids in vulns.items():
                print(f"    {k}: {', '.join(ids)}")
        except Exception as e:  # noqa: BLE001
            notes.append(f"osv: query failed: {e}")
            print(f"[4] osv: query failed: {e}")

    # 5. patterns
    dirs = linked_local_dirs(root, module, a.main)
    for d in dirs:
        for f in go_files(d):
            sources.append((os.path.relpath(f, root), open(f, encoding="utf-8", errors="replace").read()))
    found = scan_sources(sources)
    print(f"[5] patterns: {len(dirs)} local packages + review modules, {len(sources)} files")
    for kind in ("host", "ip", "exec", "homedir/ssh", "env-dump", "plugin", "linkname", "embed", "blob", "init"):
        items = found.get(kind, [])
        print(f"    {kind}: {len(items)}")
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            json.dump({"fails": fails, "notes": notes, "vulns": vulns, "found": found,
                       "local_packages": [os.path.relpath(d, root) for d in dirs]}, fh, indent=1)
        print(f"report: {a.report}")
    for n in notes:
        print("NOTE", n)
    for f in fails:
        print("FAIL", f)
    print("RESULT:", "FAIL" if fails else "PASS (read the pattern findings before you trust it)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
