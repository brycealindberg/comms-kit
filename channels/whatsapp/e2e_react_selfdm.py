#!/usr/bin/env python3
"""LIVE end-to-end proof of `react`, in the owner's OWN self-DM only.

Medium case, not the easy one: heart, then swap to thumbs up (WhatsApp keeps one
reaction per person), then the same thumbs up again (must send nothing), then
remove it. Every step is checked by reading the reactions back from Matrix, and
by the CLI's own bridge-refusal check. Prints PASS/FAIL per step; exit 0 = all pass.

Run: python3 -P e2e_react_selfdm.py   (from this directory; needs `whatsapp.self_number` in the kit config)
"""
import importlib.util
import os
import re
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "whatsapp_mautrix.py")

spec = importlib.util.spec_from_file_location("wam", CLI)
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)
# The owner's own number, from `whatsapp.self_number`. Missing = stop; never default to anyone.
SELF_DM = str(W.kitconfig.require("whatsapp.self_number"))
SELF_LABELS = (SELF_DM, *W.kitconfig.get_list("whatsapp.self_labels"))


def cli(*args):
    p = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, timeout=90)
    return p.returncode, p.stdout + p.stderr


def tool_reactions(token, room, event_id):
    chunk = W._request("GET", f"/_matrix/client/v1/rooms/{W._encode_room(room)}/relations/"
                              f"{urllib.parse.quote(event_id, safe='')}/m.annotation",
                       token=token, params={"limit": 50}).get("chunk", [])
    return sorted(W._norm_key(r["key"]) for r in W._owner_reactions(chunk) if r["via"] == "tool")


def main():
    token, mgmt = W._require_session()
    chat = W._resolve_chat(token, mgmt, SELF_DM)
    room = chat["portal_id"]
    if chat["is_group"] or chat["label"] not in SELF_LABELS:
        sys.exit(f"refusing: {SELF_DM} resolved to {chat['label']!r}, not the owner's self-DM")
    # Its own message, removed at the end. Nothing else in the self-DM can be counted on.
    chunk = W._request("GET", f"/_matrix/client/v3/rooms/{W._encode_room(room)}/messages",
                       token=token, params={"dir": "b", "limit": 20}).get("chunk", [])
    msgs = [e for e in chunk if e.get("type") == "m.room.message"]
    since = f"{max(e['origin_server_ts'] for e in msgs) / 1000:.3f}" if msgs else "empty"
    code, out = cli("send", room, f"react e2e {int(time.time())}", "--since", since, "--yes")
    found = re.search(r"Sent\. event_id=(\$\S+)", out)
    if code != 0 or not found:
        sys.exit("could not send the test message:\n" + out)
    ev = found.group(1)
    time.sleep(1)
    print(f"self-DM {chat['label']} [room {room}], target {ev}")
    results = []

    def step(name, ok, out):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print("      " + out.strip().replace("\n", "\n      "))

    # clean start: drop a reaction a failed earlier run left behind
    if tool_reactions(token, room, ev):
        cli("react", room, "--remove", "--event", ev, "--yes")
        time.sleep(2)

    code, out = cli("react", room, "heart", "--event", ev, "--yes")
    time.sleep(2)
    step("heart lands", code == 0 and "Reacted ❤️" in out
         and tool_reactions(token, room, ev) == [W._norm_key("❤️")], out)

    code, out = cli("react", room, "thumbsup", "--event", ev, "--yes")
    time.sleep(3)
    now = tool_reactions(token, room, ev)
    step("swap to thumbs up (one reaction left, and it is 👍)",
         code == 0 and "replaces your ❤️" in out and now == [W._norm_key("👍")], out + f"\nnow={now}")

    code, out = cli("react", room, "👍", "--event", ev, "--yes")
    step("same thumbs up again sends nothing", code == 0 and "already reacted" in out, out)

    code, out = cli("react", room, "heart", "--yes")
    step("--yes without --event refused (exit 2 or no target)", code in (1, 2) and "NOT SENT" in out, out)

    code, out = cli("react", room, "--remove", "--event", ev, "--yes")
    time.sleep(3)
    step("remove leaves no reaction", code == 0 and tool_reactions(token, room, ev) == [], out)

    code, out = cli("delete", room, ev, "--yes")
    step("the test message is removed", code == 0, out)

    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
