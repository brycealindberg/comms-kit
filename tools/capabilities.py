#!/usr/bin/env python3
"""Check that each channel's channel.json matches what its CLI really does, or print a table.

channel.json: {"name", "entry", "kind": "python-cli"|"node"|"external",
  "verbs": {"<verb>": {"direction": "send"|"read"|"none", "dry_run_unless_yes": true}},
  "blocked": {"<verb>": "reason"}, "ignored": [...], "config_keys": [...], "requires": [...]}

For a python-cli channel the tool runs `python3 <entry> --help` under the stub harness and reads
the subcommands from argparse's {a,b,c} group. Every subcommand must be in verbs, blocked or
ignored. Every verb must exist in the CLI (blocked verbs may be absent: they are refusals).
Node and external channels get a manifest shape check only.

Exit codes: 0 ok, 1 defect, 2 could not check (no channels).
"""
import argparse
import json
import os
import re
import shutil
import sys

import kit_common as kc

KINDS = ("python-cli", "node", "external")
DIRECTIONS = ("send", "read", "none")
REQUIRED = ("name", "entry", "kind", "verbs")


def channel_dirs(repo):
    base = os.path.join(repo, "channels")
    if not os.path.isdir(base):
        return []
    return sorted(d for d in os.listdir(base)
                  if os.path.isdir(os.path.join(base, d)) and d not in kc.SKIP_DIRS
                  and not d.startswith("."))


def is_str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def shape_problems(folder, data):
    """Problems with the manifest on its own, no CLI needed."""
    if not isinstance(data, dict):
        return ["channel.json is not an object"]
    problems = [f"missing key '{k}'" for k in REQUIRED if k not in data]
    if problems:
        return problems
    if data["name"] != folder:
        problems.append(f"name '{data['name']}' does not match the folder '{folder}'")
    if not isinstance(data["entry"], str) or not data["entry"]:
        problems.append("entry must be a non-empty string")
    if data["kind"] not in KINDS:
        problems.append(f"kind must be one of {', '.join(KINDS)}")
    verbs = data["verbs"]
    if not isinstance(verbs, dict):
        return problems + ["verbs must be an object"]
    for verb, info in verbs.items():
        if not isinstance(info, dict) or info.get("direction") not in DIRECTIONS:
            problems.append(f"verb '{verb}': direction must be one of {', '.join(DIRECTIONS)}")
            continue
        if info["direction"] == "send" or verb == "send":
            if not isinstance(info.get("dry_run_unless_yes"), bool):
                problems.append(f"verb '{verb}': a send verb must state dry_run_unless_yes")
    blocked = data.get("blocked", {})
    if not isinstance(blocked, dict) or not all(isinstance(v, str) and v.strip()
                                                for v in blocked.values()):
        problems.append("blocked must map each verb to a non-empty reason")
    for key in ("ignored", "config_keys", "requires"):
        if key in data and not is_str_list(data[key]):
            problems.append(f"{key} must be a list of strings")
    return problems


def cli_subcommands(text):
    """Subcommands of the CLI, from argparse help; None when there is no group.

    Prefer the group listed under "positional arguments:". Otherwise take the first {a,b,c}
    in the usage block that does not follow an option flag (that one is an option's choices).
    """
    positional = re.search(r"^positional arguments:\s*\n\s*(\{[^}\n]*\})", text, re.M)
    if positional:
        return kc.SUBCOMMAND_GROUP.match(positional.group(1)).group(1).split(",")
    usage = re.search(r"^usage:.*?(?:\n\s*\n|\Z)", text, re.S | re.M | re.I)
    block = usage.group(0) if usage else text
    for match in kc.SUBCOMMAND_GROUP.finditer(block):
        before = block[:match.start()].rstrip().rstrip("[").rstrip()
        if not before.split()[-1:] or not before.split()[-1].startswith("-"):
            return match.group(1).split(",")
    return None


def check_cli(repo, folder, data, env, log):
    entry = data["entry"]
    path = next((p for p in (os.path.join(repo, entry),
                             os.path.join(repo, "channels", folder, entry))
                 if os.path.isfile(p)), None)
    if path is None:
        return [f"entry '{entry}' does not exist"]
    offset = kc.log_size(log)
    code, out = kc.run([sys.executable, path, "--help"], env, repo, 60)
    problems = [f"--help reached a real tool: {name}"
                for name in kc.forbidden(kc.stub_calls(log, offset))]
    if code != 0:
        return problems + [f"'{entry} --help' did not exit 0 "
                           f"({'timeout' if code is None else 'exit ' + str(code)})"]
    found = cli_subcommands(out)
    if found is None:
        found = []
    listed = set(data["verbs"])
    covered = listed | set(data.get("blocked", {})) | set(data.get("ignored", []))
    problems += [f"CLI subcommand '{sub}' is not in verbs, blocked or ignored"
                 for sub in found if sub not in covered]
    problems += [f"verb '{verb}' is not a subcommand of the CLI"
                 for verb in sorted(listed) if verb not in found]
    return problems


def load_channels(repo):
    channels, problems = [], []
    for folder in channel_dirs(repo):
        manifest = os.path.join(repo, "channels", folder, "channel.json")
        if not os.path.isfile(manifest):
            problems.append(f"channels/{folder}: no channel.json")
            continue
        try:
            with open(manifest, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            problems.append(f"channels/{folder}/channel.json: not valid JSON")
            continue
        channels.append((folder, data))
    return channels, problems


def table(channels):
    rows = ["| channel | verb | direction | blocked reason |", "| --- | --- | --- | --- |"]
    for folder, data in channels:
        if not isinstance(data, dict) or not isinstance(data.get("verbs"), dict):
            continue
        for verb, info in data["verbs"].items():
            direction = info.get("direction", "?") if isinstance(info, dict) else "?"
            rows.append(f"| {folder} | {verb} | {direction} | - |")
        blocked = data.get("blocked")
        for verb, reason in (blocked.items() if isinstance(blocked, dict) else []):
            rows.append(f"| {folder} | {verb} | blocked | {reason} |")
    return "\n".join(rows)


def summary(channels):
    """One row per channel, for the README."""
    rows = ["| Channel | Read | Send | Other | Cannot do |", "|---|---|---|---|---|"]
    for folder, data in channels:
        verbs = data.get("verbs") if isinstance(data, dict) else None
        if not isinstance(verbs, dict):
            continue

        def column(direction):
            found = [v for v, info in verbs.items()
                     if isinstance(info, dict) and info.get("direction") == direction]
            return ", ".join(f"`{v}`" for v in found) or "-"

        blocked = data.get("blocked")
        cannot = ", ".join(f"`{v}`" for v in blocked) if isinstance(blocked, dict) and blocked else "-"
        rows.append(f"| {folder} | {column('read')} | {column('send')} | {column('none')} | {cannot} |")
    return "\n".join(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--table", action="store_true")
    ap.add_argument("--summary", action="store_true", help="one row per channel, for the README")
    ap.add_argument("--only", help="comma list of channel folders, for a check before all exist")
    args = ap.parse_args()
    repo = kc.repo_arg(args.repo)
    channels, problems = load_channels(repo)
    if args.only:
        wanted = set(args.only.split(","))
        channels = [c for c in channels if os.path.basename(c[0]) in wanted]
        problems = [p for p in problems if any(name in p for name in wanted)]
    if not channels and not problems:
        kc.die("ERROR: no channels found under channels/")
    if args.summary:
        print(summary(channels))
        return kc.EXIT_OK
    if args.table:
        print(table(channels))
        if not args.check:
            return kc.EXIT_OK

    work = kc.make_workdir("comms-caps-")
    try:
        home = os.path.join(work, "home")
        os.makedirs(home)
        log = os.path.join(work, "stub.log")
        stubs = kc.make_stubs(os.path.join(work, "stubs"), log)
        example = os.path.join(repo, "config.example.json")
        config = kc.copy_config(work, example) if os.path.isfile(example) else None
        env = kc.clean_env(home, stubs, config)
        verbs_total = real_cli = shape_only = 0
        for folder, data in channels:
            found = shape_problems(folder, data)
            problems += [f"channels/{folder}: {p}" for p in found]
            if found:
                continue
            verbs_total += len(data["verbs"])
            if data["kind"] == "python-cli":
                real_cli += 1
                problems += [f"channels/{folder}: {p}"
                             for p in check_cli(repo, folder, data, env, log)]
            else:
                shape_only += 1
    finally:
        shutil.rmtree(work, ignore_errors=True)

    for line in problems[:300]:
        print(line)
    if problems:
        print(f"capabilities FAILED: {len(problems)} problems")
        return kc.EXIT_DEFECT
    if shape_only:
        print(f"note: {shape_only} node/external channels checked for manifest shape only")
    print(f"capabilities ok: {len(channels)} channels, {verbs_total} verbs "
          f"({real_cli} checked against a real CLI)")
    return kc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
