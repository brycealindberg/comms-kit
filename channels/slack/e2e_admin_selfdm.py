#!/usr/bin/env python3
"""LIVE end-to-end proof of the Slack admin verbs, in the owner's OWN Slack self-DM only.

Config keys (all required): slack.e2e_workspace (your own workspace, never a client's),
slack.self_dm (your DM with yourself), slack.owner_user_id (your user id). The run refuses any
other chat.
Hard case, not the easy one: it opens the DM by user id, posts through `send` to the DM that
`open-dm` printed, edits it, reads the edit back from Slack, hits the money lock, finds the
message by search (Slack's index can lag, so it retries), deletes it, reads back that it is gone,
and lists the members. Every read-back is a fresh live read of Slack, never the CLI's own words.
Prints PASS/FAIL per step and ends with exactly `RESULT: PASS` only if every step passed.
Channel create/invite/remove/rename/leave are NOT run here: those workspaces belong to clients,
so they are covered by mocks in test_slack_admin.py only.

Run: python3 -I e2e_admin_selfdm.py   (from this folder)
"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "slack_bridge.py")


def _config(key):
    """A required config value. The kit's kitconfig.py is found by COMMS_KIT_CORE, the kit layout, then the live Mac."""
    import importlib.util  # noqa: PLC0415
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
MARK = f"admin e2e {int(time.time())}"


def cli(*args, timeout=180):
    p = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def live():
    """The self-DM as Slack has it right now: {ts: text}."""
    code, out = cli("threads", DM, "--workspace", WS, "--json", "-n", "15")
    if code != 0:
        sys.exit(f"refusing: cannot read the self-DM back: {out[:300]}")
    data = json.loads(out.strip().splitlines()[-1])
    if data.get("channel") != DM:
        sys.exit("refusing: the read did not come back from the self-DM")
    return {m["ts"]: m.get("text") or "" for m in data.get("messages", [])}


def since_of(out):
    m = re.search(r"--since ([0-9]+\.[0-9]+)\)", out)
    return m.group(1) if m else None


def main():
    results = []
    state = {}

    def step(name, ok, out=""):
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print("      " + str(out).strip().replace("\n", "\n      ")[:900])

    # leftovers from a failed earlier run: only messages carrying this script's own marker prefix
    for ts, text in live().items():
        if text.startswith("admin e2e "):
            cli("delete", DM, ts, "--workspace", WS, "--yes")

    code, out = cli("open-dm", SELF, "--workspace", WS)
    state["dm"] = (re.search(r"\b(D[A-Z0-9]{8,14})\b", out) or [None, None])[1]
    step("open-dm on the owner's own id prints the self-DM, sends nothing, and says send works",
         code == 0 and state["dm"] == DM and "send works now" in out and MARK not in "".join(live().values()), out)
    if state["dm"] != DM:
        print("RESULT: FAIL")
        sys.exit(1)

    code, out = cli("send", DM, MARK, "--workspace", WS)
    since = since_of(out)
    step("send preview to the opened DM writes nothing", code == 0 and "DRY RUN" in out and MARK not in "".join(live().values()), out)

    code, out = cli("send", DM, MARK, "--workspace", WS, "--since", since or "empty", "--yes")
    m = re.search(r'"ts":\s*"([0-9.]+)"', out)
    state["ts"] = m.group(1) if m else None
    step("send --yes lands in the DM that open-dm printed", code == 0 and state["ts"] and live().get(state["ts"]) == MARK, out)
    if not state["ts"]:
        print("RESULT: FAIL")
        sys.exit(1)
    ts = state["ts"]

    new_text = f"{MARK} (edited)"
    code, out = cli("edit", DM, ts, new_text, "--workspace", WS)
    step("edit dry run writes nothing", code == 0 and "Dry run" in out and live().get(ts) == MARK, out)

    code, out = cli("edit", DM, ts, new_text, "--workspace", WS, "--yes")
    step("edit --yes without --since refuses (exit 2), text unchanged", code == 2 and live().get(ts) == MARK, out)

    code, out = cli("edit", DM, ts, f"{MARK} costs $500", "--workspace", WS, "--since", ts, "--yes")
    step("a price in the new text hits the money lock (exit 6), text unchanged", code == 6 and live().get(ts) == MARK, out)

    code, out = cli("edit", DM, ts, new_text, "--workspace", WS, "--since", ts, "--yes")
    step("edit --yes --since lands, and Slack shows the new text", code == 0 and "Edited" in out and live().get(ts) == new_text, out)

    stale = str(float(ts) - 60)
    code, out = cli("edit", DM, ts, f"{MARK} stale", "--workspace", WS, "--since", stale, "--yes")
    step("edit with an old --since refuses (exit 5), text unchanged", code == 5 and live().get(ts) == new_text, out)

    found = ""
    for _ in range(12):                          # Slack's search index lags a fresh post
        code, found = cli("find", MARK, "--workspace", WS, "-n", "5")
        if code == 0 and ts in found:
            break
        time.sleep(10)
    step("find returns the message (retries while the search index catches up)", code == 0 and ts in found, found)

    # One word of letters. Slack splits on a hyphen and also matches inside long blobs by digits.
    nonce = "".join(chr(97 + int(ch)) for ch in str(int(time.time())))
    code, out = cli("find", "zzzqqqnomatch" + nonce, "--workspace", WS)
    step("find with no hit says so and exits 0", code == 0 and "no match" in out, out)

    code, out = cli("mentions", "--workspace", WS, "-n", "3")
    step("mentions runs read-only", code == 0, out)

    code, out = cli("delete", DM, ts, "--workspace", WS)
    step("delete dry run writes nothing", code == 0 and "Dry run" in out and ts in live(), out)

    code, out = cli("delete", DM, ts, "--workspace", WS, "--yes")
    step("delete --yes removes it, and Slack no longer shows it", code == 0 and "Deleted" in out and ts not in live(), out)

    code, out = cli("delete", DM, ts, "--workspace", WS, "--yes")
    step("deleting it again fails non-zero (nothing to delete)", code != 0, out)

    code, out = cli("channel-members", DM, "--workspace", WS)
    step("channel-members lists the self-DM members read-only", code == 0 and SELF in out, out)

    code, out = cli("channel-create", "e2e-never-created", "--workspace", WS)
    step("channel-create without --yes is a dry run", code == 0 and "Dry run" in out, out)

    ok = all(results)
    print(f"\n{sum(results)}/{len(results)} passed")
    print("RESULT: PASS" if ok else "RESULT: FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
