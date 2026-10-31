#!/usr/bin/env python3
"""LIVE proof of the Slack verbs that reach no other person. It runs in the owner's own workspace only.

Config keys (all required): slack.e2e_workspace (your own workspace, never a client's), slack.self_dm
(your DM with yourself), slack.owner_user_id (your user id). The run refuses any other chat.

Hard cases, not the easy ones:
  * a file upload to the self-DM, read back by file name from Slack
  * a thread reply, read back as a reply under its own root message
  * a private channel that holds only the owner: create, read the members, rename, then leave.
    Slack refuses the last member's leave (`last_member`), and the tool must say so and exit 4.
    Then archive it, and prove the archive with Slack's own `already_archived` on a second try.
Every read-back is a fresh live read of Slack, never the CLI's own words.
Prints PASS/FAIL per step and ends with `RESULT: PASS` only when every step passed.

Run: python3 -I e2e_gaps_selfdm.py   (from this folder)
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "slack_bridge.py")


def _config(key):
    """A required config value. kitconfig.py is found by COMMS_KIT_CORE, the kit layout, then the live Mac."""
    folders = [os.environ.get("COMMS_KIT_CORE") or "", os.path.join(HERE, "..", "..", "core"),  # kit: code path
               os.path.expanduser("~/.claude/communications")]
    for folder in folders:
        path = os.path.abspath(os.path.join(os.path.expanduser(folder), "kitconfig.py")) if folder else ""
        if path and os.path.exists(path):
            spec = importlib.util.spec_from_file_location("kitconfig", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            try:
                return str(module.require(key))
            except module.ConfigError as err:
                sys.exit(f"refusing: {err}")
    sys.exit("refusing: kitconfig.py not found (set COMMS_KIT_CORE)")


WS, DM, SELF = _config("slack.e2e_workspace"), _config("slack.self_dm"), _config("slack.owner_user_id")
STAMP = str(int(time.time()))
MARK = f"gaps e2e {STAMP}"


def cli(*args, timeout=180):
    p = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def room():
    """The self-DM as Slack has it right now, with replies."""
    code, out = cli("threads", DM, "--workspace", WS, "--json", "-n", "15")
    if code != 0:
        sys.exit(f"refusing: cannot read the self-DM back: {out[:300]}")
    data = json.loads(out.strip().splitlines()[-1])
    if data.get("channel") != DM:
        sys.exit("refusing: the read did not come back from the self-DM")
    return data.get("messages", [])


def since_of(out):
    found = re.search(r"--since ([0-9]+\.[0-9]+)\)", out)
    return found.group(1) if found else "empty"


def ts_of(out):
    found = re.search(r'"ts":\s*"([0-9.]+)"', out)
    return found.group(1) if found else None


def main():
    results = []

    def step(name, ok, out=""):
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print("      " + str(out).strip().replace("\n", "\n      ")[:900])

    # A file upload
    with tempfile.TemporaryDirectory() as tmp:
        name = f"gaps-e2e-{STAMP}.txt"
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("comms-kit live proof file. Safe to delete.\n")
        code, out = cli("upload", DM, path, "--workspace", WS, "--caption", MARK)
        step("upload without --yes is a dry run", code == 0 and name not in cli("files", DM, "--workspace", WS)[1], out)
        code, out = cli("upload", DM, path, "--workspace", WS, "--caption", MARK, "--yes")
        step("upload --yes answers ok", code == 0, out)
    listed = ""
    for _ in range(8):               # the bridge's file list follows its poll of the channel
        listed = cli("files", DM, "--workspace", WS)[1]
        if name in listed:
            break
        time.sleep(10)
    step("Slack lists the uploaded file by name", name in listed, listed)
    caption_ts = next((m["ts"] for m in room() if (m.get("text") or "").startswith(MARK)), None)

    # A thread reply
    code, out = cli("send", DM, f"{MARK} root", "--workspace", WS)
    code, out = cli("send", DM, f"{MARK} root", "--workspace", WS, "--since", since_of(out), "--yes")
    root = ts_of(out)
    step("the root message lands", code == 0 and root, out)
    code, out = cli("send", DM, f"{MARK} reply", "--workspace", WS, "--thread-ts", root or "0")
    code, out = cli("send", DM, f"{MARK} reply", "--workspace", WS, "--thread-ts", root or "0",
                    "--since", since_of(out), "--yes")
    reply = ts_of(out)
    step("the thread reply answers ok", code == 0 and reply, out)
    seen = room()
    step("Slack shows the reply under its root",
         any(m.get("ts") == reply and m.get("thread_ts") == root for m in seen), json.dumps(seen[-4:])[:600])

    # Clean up the self-DM: this script's own messages only
    for ts in (reply, root, caption_ts):
        if ts:
            code, out = cli("delete", DM, ts, "--workspace", WS, "--yes")
            step(f"delete {ts}", code == 0, out)

    # A private channel that holds only the owner
    name_a, name_b = f"kit-proof-{STAMP}", f"kit-proof-{STAMP}-renamed"
    code, out = cli("channel-create", name_a, "--workspace", WS, "--private")
    step("channel-create without --yes is a dry run", code == 0 and "Dry run" in out, out)
    code, out = cli("channel-create", name_a, "--workspace", WS, "--private", "--yes")
    found = re.search(r"\b([CG][A-Z0-9]{8,14})\b", out)
    channel = found.group(1) if found else None
    step("channel-create --private --yes makes it", code == 0 and channel, out)
    if channel:
        code, out = cli("channel-members", channel, "--workspace", WS)
        ids = re.findall(r"\bU[A-Z0-9]{8,14}\b", out)
        step("the owner is the only member", code == 0 and ids == [SELF], out)
        code, out = cli("channel-rename", channel, name_b, "--workspace", WS, "--yes")
        step("channel-rename --yes answers ok and prints the new name", code == 0 and name_b in out, out)
        code, out = cli("channel-leave", channel, "--workspace", WS)
        step("channel-leave without --yes is a dry run", code == 0 and "Dry run" in out, out)
        code, out = cli("channel-leave", channel, "--workspace", WS, "--yes")
        step("channel-leave as the last member: Slack refuses, exit 4, and the tool names channel-archive",
             code == 4 and "last_member" in out and "channel-archive" in out, out)
        code, out = cli("channel-archive", channel, "--workspace", WS)
        step("channel-archive without --yes is a dry run", code == 0 and "Dry run" in out, out)
        code, out = cli("channel-archive", channel, "--workspace", WS, "--yes")
        step("channel-archive --yes answers ok", code == 0 and "Archived" in out, out)
        code, out = cli("channel-archive", channel, "--workspace", WS, "--yes")
        step("Slack itself says it is archived on a second try", code == 4 and "already_archived" in out, out)

    ok = all(results)
    print(f"\n{sum(results)}/{len(results)} passed")
    print("RESULT: PASS" if ok else "RESULT: FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
