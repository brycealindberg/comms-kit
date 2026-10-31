#!/usr/bin/env python3
"""License gate for the public repo.

1. LICENSE must hold the MIT text.
2. No file or folder may be named for a banned project (names below), except under a
   channel's docs folder (channels/<name>/docs/).
3. No Mach-O or ELF binary may exist in the tree (magic bytes, whatever the file is called).
4. Every package in every package-lock.json needs a license on the allow list, or an entry
   with a reason in tools/license-exceptions.json ({"package name": "reason"}). A package
   with no license field fails. Only the lock file is read; there is no network call.

Exit codes: 0 ok, 1 defect, 2 could not check.
"""
import argparse
import json
import os
import re
import sys

import kit_common as kc

ALLOWED = {"MIT", "ISC", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "0BSD", "BlueOak-1.0.0",
           "Unlicense", "CC0-1.0"}
MIT_PHRASES = (
    "MIT License",
    "Permission is hereby granted, free of charge, to any person obtaining a copy",
    "to deal in the Software without restriction, including without limitation the rights",
    "The above copyright notice and this permission notice shall be included in all copies",
    'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND',
    "IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES",
)
BANNED_NAME = re.compile(r"mautrix|whatsmeow|baileys|(?<![a-z0-9])imsg(?![a-z0-9])", re.I)
MAGIC = (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
         b"\x7fELF")
FAT_MAGIC = b"\xca\xfe\xba\xbe"
# Our own code that talks to a bridge over the network and copies none of it.
OWN_FILES_NAMED_FOR_A_BRIDGE = {"channels/whatsapp/whatsapp_mautrix.py"}


def normalise(text):
    text = text.replace("“", '"').replace("”", '"').replace("’", "'")
    return re.sub(r"\s+", " ", text)


def check_license_file(repo):
    path = os.path.join(repo, "LICENSE")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = normalise(fh.read())
    except OSError:
        return ["LICENSE: missing"]
    missing = [p for p in MIT_PHRASES if normalise(p) not in text]
    return [f"LICENSE: not the MIT text (missing {len(missing)} standard passages)"] if missing \
        else []


def in_channel_docs(rel):
    parts = rel.split("/")
    return len(parts) > 3 and parts[0] == "channels" and parts[2] == "docs"


def check_names(files):
    problems, seen = [], set()
    for rel in files:
        if in_channel_docs(rel) or rel in OWN_FILES_NAMED_FOR_A_BRIDGE:
            continue
        parts = rel.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            if prefix not in seen and BANNED_NAME.search(parts[depth - 1]):
                seen.add(prefix)
                problems.append(f"{prefix}: banned name")
    return problems


def is_native_binary(path):
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return False
    if head[:4] in MAGIC:
        return True
    # Fat Mach-O and a Java class file share cafebabe. A class file has a version >= 45 next.
    return head[:4] == FAT_MAGIC and len(head) == 8 and int.from_bytes(head[4:8], "big") < 45


def check_binaries(repo, files):
    return [f"{rel}: native binary (Mach-O or ELF)" for rel in files
            if not os.path.islink(os.path.join(repo, rel))
            and os.path.isfile(os.path.join(repo, rel))
            and is_native_binary(os.path.join(repo, rel))]


def term_ok(term):
    term = term.strip().strip("()").strip()
    return all(part.strip().strip("()").strip() in ALLOWED for part in re.split(r"\s+AND\s+", term))


def license_ok(value):
    if isinstance(value, dict):
        value = value.get("type")
    if not isinstance(value, str) or not value.strip():
        return False
    return any(term_ok(t) for t in re.split(r"\s+OR\s+", value.strip().strip("()")))


def package_name(path, info):
    if isinstance(info.get("name"), str):
        return info["name"]
    return path.rsplit("node_modules/", 1)[-1]


def load_exceptions(path):
    if not os.path.isfile(path):
        return {}
    data = kc.load_json(path, "the license exceptions file")
    if not isinstance(data, dict):
        kc.die("ERROR: license-exceptions.json must map package names to reasons")
    return data


def check_locks(repo, files, exceptions):
    problems, count = [], 0
    locks = [f for f in files if os.path.basename(f) == "package-lock.json" and not kc.skipped(f)]
    for rel in locks:
        try:
            with open(os.path.join(repo, rel), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            kc.die(f"ERROR: {rel} is not readable JSON")
        packages = data.get("packages") if isinstance(data, dict) else None
        if not isinstance(packages, dict):
            kc.die(f"ERROR: {rel} has no 'packages' map (lockfile v1 holds no license data)")
        for path, info in packages.items():
            if path == "" or not isinstance(info, dict) or info.get("link"):
                continue
            count += 1
            name = package_name(path, info)
            reason = exceptions.get(name)
            if isinstance(reason, str) and reason.strip():
                continue
            if name in exceptions:
                problems.append(f"{rel}: {name}: exception has no reason")
            elif "license" not in info:
                problems.append(f"{rel}: {name}: no license field")
            elif not license_ok(info["license"]):
                problems.append(f"{rel}: {name}: license not allowed")
    return count, problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo")
    ap.add_argument("--exceptions", help="default: tools/license-exceptions.json in the repo")
    args = ap.parse_args()
    repo = kc.repo_arg(args.repo)
    if not kc.is_git_repo(repo):
        kc.die("ERROR: not a git repo")
    files = [f for f in kc.repo_files(repo) if not kc.skipped(f)]
    if not files:
        kc.die("ERROR: the repo has no files to check")
    count, lock_problems = check_locks(repo, files, load_exceptions(
        args.exceptions or os.path.join(repo, "tools", "license-exceptions.json")))
    problems = (check_license_file(repo) + check_names(files) + check_binaries(repo, files)
                + lock_problems)
    for line in problems[:300]:
        print(line)
    if problems:
        print(f"licenses FAILED: {len(problems)} problems")
        return kc.EXIT_DEFECT
    print(f"licenses ok: {count} packages")
    return kc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
