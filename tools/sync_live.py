#!/usr/bin/env python3
"""Compare the repo with the live files it was copied from, or copy live files into the repo.

The map (default ~/.config/comms-kit/live-map.json) says which repo file mirrors which live
file. It is personal data, so it lives outside the repo. Output names repo paths and reasons
only, never file contents or live paths.

The map may also hold "rewrites": {"<repo path>": [["old", "new"], ...]}. They are applied, in
order, to the live bytes before the scrub check, the copy and the compare, so a live file that
cannot change (it matches production) can still ship a clean copy. The live file is never
written. A rewrite whose old text is missing from the live file counts as a difference, so a
stale rewrite cannot hide drift.

Exit codes: 0 ok, 1 a difference or a refused file, 2 could not check.
"""
import argparse
import os
import re
import shutil
import sys

import kit_common as kc
import scrub_check

DEFAULT_MAP = os.path.join(scrub_check.CONFIG_DIR, "live-map.json")
ROOTS = ("core", "channels")


def glob_to_regex(pattern):
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        out.append("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch))
        i += 1
    return re.compile("".join(out) + r"\Z")


def load_map(path):
    data = kc.load_json(path, "the live map")
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict) or not files:
        kc.die("ERROR: the live map has no files")
    kit_only = data.get("kit_only", [])
    if not isinstance(kit_only, list) or not all(isinstance(p, str) for p in kit_only):
        kc.die("ERROR: kit_only must be a list of patterns")
    mapping = {}
    for rel, live in files.items():
        norm = os.path.normpath(rel).replace(os.sep, "/")
        if (not isinstance(live, str) or not live or os.path.isabs(rel)
                or norm.startswith("../") or norm in ("..", ".")):
            kc.die("ERROR: the live map holds a path that is not valid")
        mapping[norm] = os.path.expanduser(live)
    return mapping, [glob_to_regex(p) for p in kit_only], load_rewrites(data.get("rewrites"), mapping)


def load_rewrites(raw, mapping):
    """{repo path: [(old bytes, new bytes), ...]}. A bad shape is a map error, not a skip."""
    if raw is None:
        return {}
    bad = "ERROR: rewrites must map a mapped repo path to a list of [old, new] text pairs"
    if not isinstance(raw, dict):
        kc.die(bad)
    out = {}
    for rel, pairs in raw.items():
        norm = os.path.normpath(rel).replace(os.sep, "/")
        if norm not in mapping or not isinstance(pairs, list):
            kc.die(bad)
        parsed = []
        for pair in pairs:
            if (not isinstance(pair, list) or len(pair) != 2
                    or not all(isinstance(t, str) for t in pair) or not pair[0]):
                kc.die(bad)
            parsed.append((pair[0].encode("utf-8"), pair[1].encode("utf-8")))
        out[norm] = parsed
    return out


def apply_rewrites(data, pairs):
    """(rewritten bytes, True when every old text was found). Each pair sees the last one's output."""
    complete = True
    for old, new in pairs:
        if old not in data:
            complete = False
        data = data.replace(old, new)
    return data, complete


def read_bytes(path):
    """File bytes, or None when the path is missing, a folder or unreadable."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def check(repo, mapping, kit_only, rewrites):
    diffs = []
    for rel in sorted(mapping):
        repo_path, live_path = os.path.join(repo, rel), mapping[rel]
        repo_data, live_data = read_bytes(repo_path), read_bytes(live_path)
        if repo_data is None:
            diffs.append((rel, "missing in repo"))
        elif live_data is None:
            diffs.append((rel, "missing live"))
        else:
            live_data, complete = apply_rewrites(live_data, rewrites.get(rel, []))
            if not complete:
                diffs.append((rel, "rewrite text missing in live (stale rewrite)"))
            elif repo_data != live_data:
                diffs.append((rel, "differs from live"))
    for rel in kc.repo_files(repo):
        if rel.split("/")[0] not in ROOTS or kc.skipped(rel) or rel in mapping:
            continue
        if rel.endswith(".pyc") or os.path.basename(rel) == ".DS_Store":
            continue
        if not any(rx.match(rel) for rx in kit_only):
            diffs.append((rel, "unmapped: not in the map and not kit_only"))
    for rel, reason in diffs:
        print(f"{rel}: {reason}")
    if diffs:
        print(f"NOT in sync: {len(mapping)} files, {len(diffs)} differ")
        return kc.EXIT_DEFECT
    print(f"in sync: {len(mapping)} files, 0 differ")
    return kc.EXIT_OK


def pull(repo, mapping, deny, allow, rewrites):
    staged, problems = [], []
    for rel in sorted(mapping):
        data = read_bytes(mapping[rel])
        if data is None:
            problems.append(f"{rel}: missing live")
            continue
        data, complete = apply_rewrites(data, rewrites.get(rel, []))
        if not complete:
            problems.append(f"{rel}: rewrite text missing in live (stale rewrite)")
            continue
        text = data.decode("latin-1")
        hits = [(f"{rel}:0", r) for r in scrub_check.line_rules(rel, deny)]
        for number, line in enumerate(text.split("\n"), 1):
            if f"{rel}:{line}" in allow:
                continue
            hits.extend((f"{rel}:{number}", r) for r in scrub_check.line_rules(line, deny))
        problems.extend(f"{where} {rule}" for where, rule in hits)
        staged.append((rel, data))
    if problems:
        for line in problems[:300]:
            print(line)
        print("refused: nothing copied")
        return kc.EXIT_DEFECT
    temps = []
    try:
        for rel, data in staged:
            target = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + ".kit-pull-tmp"
            temps.append(tmp)
            with open(tmp, "wb") as fh:
                fh.write(data)
            shutil.copymode(mapping[rel], tmp)
        for (rel, _), tmp in zip(staged, temps):
            os.replace(tmp, os.path.join(repo, rel))
    except OSError as err:
        for tmp in temps:
            try:
                os.remove(tmp)
            except OSError:
                pass
        kc.die(f"ERROR: copy failed: {err.strerror}")
    print(f"pulled {len(staged)} files")
    return kc.EXIT_OK


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="compare bytes, repo against live")
    mode.add_argument("--pull", action="store_true", help="copy live files into the repo")
    ap.add_argument("--repo")
    ap.add_argument("--map", default=DEFAULT_MAP)
    ap.add_argument("--deny", default=scrub_check.DEFAULT_DENY)
    ap.add_argument("--allow", default=scrub_check.DEFAULT_ALLOW)
    args = ap.parse_args()
    repo = kc.repo_arg(args.repo)
    mapping, kit_only, rewrites = load_map(args.map)
    if args.check:
        return check(repo, mapping, kit_only, rewrites)
    deny = scrub_check.load_deny(args.deny)
    return pull(repo, mapping, deny, scrub_check.load_allow(args.allow), rewrites)


if __name__ == "__main__":
    sys.exit(main())
