#!/usr/bin/env python3
"""LIVE end-to-end proof of reply / edit / delete / mark-read / find, in the owner's OWN self-DM only.

Medium case, not the easy one: send, reply, edit, edit the SAME message a second time, read each
edit back from Matrix, mark read, delete, read the redaction back, then delete it AGAIN (must not
report success), and prove the refusals (the owner's phone's message, an edit of a deleted message).
Groups and blocks are NOT touched here: they reach real people (mock tests only).

Prints PASS or FAIL per step. The last line is exactly `RESULT: PASS` only when every step passed,
else `RESULT: FAIL`.

Run: python3 -P e2e_message_admin_selfdm.py   (from this directory; needs `whatsapp.self_number` in the kit config)
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
    p = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr


def main():
    token, mgmt = W._require_session()
    chat = W._resolve_chat(token, mgmt, SELF_DM)
    room = chat["portal_id"]
    if chat["is_group"] or chat["label"] not in SELF_LABELS:
        sys.exit(f"refusing: {SELF_DM} resolved to {chat['label']!r}, not the owner's self-DM")
    enc = W._encode_room(room)
    stamp = f"zq{int(time.time())}"
    results = []

    def step(name, ok, out=""):
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print("      " + str(out).strip().replace("\n", "\n      "))

    def timeline(limit=40):
        return W._request("GET", f"/_matrix/client/v3/rooms/{enc}/messages", token=token,
                          params={"dir": "b", "limit": limit}).get("chunk", [])

    def since():
        msgs = [e for e in timeline() if e.get("type") == "m.room.message"]
        return f"{max(e['origin_server_ts'] for e in msgs) / 1000:.3f}" if msgs else "empty"

    def event(eid):
        return W._request("GET", f"/_matrix/client/v3/rooms/{enc}/event/"
                                 f"{urllib.parse.quote(eid, safe='')}", token=token)

    def replaces(eid):
        return W._request("GET", f"/_matrix/client/v1/rooms/{enc}/relations/"
                                 f"{urllib.parse.quote(eid, safe='')}/m.replace",
                          token=token, params={"limit": 50}).get("chunk", [])

    def event_id(out, word):
        m = re.search(rf"{word}\. event_id=(\S+)", out)
        return m.group(1) if m else None

    no_send_line = lambda out: not any(l.startswith("About to send to") for l in out.splitlines())  # noqa: E731

    # 1. send the message the rest of the run works on
    text = f"admin e2e {stamp}"
    code, out = cli("send", room, text, "--since", since(), "--yes")
    ev = event_id(out, "Sent")
    step("send the test message", code == 0 and ev, out)
    if not ev:
        print("RESULT: FAIL")
        sys.exit(1)
    time.sleep(1)

    # 2. reply to it
    code, out = cli("reply", room, ev, f"reply {stamp}", "--since", since(), "--yes")
    reply_id = event_id(out, "Sent")
    content = event(reply_id)["content"] if reply_id else {}
    step("reply lands and points at the message (m.in_reply_to read back)",
         code == 0 and reply_id and content.get("m.relates_to", {}).get("m.in_reply_to", {}).get("event_id") == ev
         and content.get("body") == f"reply {stamp}", out + str(content))
    time.sleep(1)

    # 3. dry run changes nothing and never says "About to send to"
    before = len(replaces(ev))
    code, out = cli("edit", room, ev, f"dry {stamp}")
    step("edit dry run: prints About to edit, no About to send to, writes nothing",
         code == 0 and "About to edit your message" in out and no_send_line(out)
         and len(replaces(ev)) == before, out)

    # 4. first edit, read back
    code, out = cli("edit", room, ev, f"edited once {stamp}", "--since", since(), "--yes")
    time.sleep(2)
    reps = replaces(ev)
    bodies = [(r.get("content", {}).get("m.new_content") or {}).get("body") for r in reps]
    step("edit lands (m.replace read back from Matrix)",
         code == 0 and "Edited. event_id=" in out and f"edited once {stamp}" in bodies and no_send_line(out),
         out + str(bodies))

    # 5. second edit on the already-edited message
    code, out = cli("edit", room, ev, f"edited twice {stamp}", "--since", since(), "--yes")
    time.sleep(2)
    reps = replaces(ev)
    newest = max(reps, key=lambda r: r["origin_server_ts"]) if reps else {}
    step("SECOND edit on an already-edited message works (newest replace has the new text)",
         code == 0 and "Edited. event_id=" in out and len(reps) >= 2
         and (newest.get("content", {}).get("m.new_content") or {}).get("body") == f"edited twice {stamp}"
         and (newest.get("content", {}).get("m.relates_to") or {}).get("event_id") == ev, out)

    # 6. find sees the latest text (server search, whole words)
    found = ""
    for _ in range(8):
        code, found = cli("find", f"{stamp}", "--chat", room, "-n", "5")
        if ev in found and f"edited twice {stamp}" in found:     # the search index lags a few seconds
            break
        time.sleep(2)
    step("find (read-only) returns the message and its edited text",
         code == 0 and ev in found and f"edited twice {stamp}" in found and "(edited)" in found, found)

    # 7. refusals that must write nothing
    phone = next((e for e in timeline(60) if e.get("type") == "m.room.message"
                  and e.get("sender", "").startswith("@whatsapp_lid-5914824327362")), None)
    if phone:
        n = len(timeline(60))
        code, out = cli("edit", room, phone["event_id"], f"nope {stamp}", "--since", since(), "--yes")
        step("edit of the owner's phone's message is refused and writes nothing",
             code == 1 and "NOT EDITED" in out and len(timeline(60)) == n, out)
        code, out = cli("delete", room, phone["event_id"], "--yes")
        step("delete of the owner's phone's message is refused and writes nothing",
             code == 1 and "NOT DELETED" in out and not event(phone["event_id"])["content"] == {}, out)
    else:
        print("SKIP  no phone-sent message in the self-DM to test the refusal on")

    code, out = cli("group-members", room)
    step("group verbs refuse a DM (read-only)", code == 1 and "not a group" in out, out)
    code, out = cli("block", SELF_DM)
    step("block refuses his own number (dry run, nothing sent)", code == 1 and "own number" in out, out)
    code, out = cli("unblock", SELF_DM)
    step("unblock exits 5 with the reason", code == 5 and "block-user" in out, out)

    # 8. delete the reply first (cleanup, and a second real delete), then mark read
    if reply_id:
        code, out = cli("delete", room, reply_id, "--yes")
        time.sleep(1)
        step("delete the reply message", code == 0 and "Deleted. event_id=" in out
             and not event(reply_id).get("content"), out)

    code, out = cli("mark-read", room, "--event", ev)
    step("mark-read dry run sends nothing", code == 0 and "About to mark read" in out
         and "Dry run" in out and no_send_line(out), out)
    code, out = cli("mark-read", room, "--event", ev, "--yes")
    step("mark-read: the homeserver stored our receipt (read back)",
         code == 0 and "Marked read" in out and W._receipt_seen(token, room, ev), out)

    # 9. delete the test message, read the redaction back
    code, out = cli("delete", room, ev, "--yes")
    time.sleep(2)
    gone = event(ev)
    redactions = [e for e in timeline(60) if e.get("type") == "m.room.redaction"
                  and (e.get("redacts") or e.get("content", {}).get("redacts")) == ev]
    step("delete lands (message blanked and an m.room.redaction exists)",
         code == 0 and "Deleted. event_id=" in out and not gone.get("content") and redactions
         and no_send_line(out), out + str(gone))

    # 10. medium case: delete it AGAIN, and edit it after deletion
    n = len(timeline(60))
    code, out = cli("delete", room, ev, "--yes")
    step("second delete does NOT report success and writes nothing",
         code == 0 and "already deleted" in out and "Deleted. event_id" not in out
         and len(timeline(60)) == n, out)
    code, out = cli("edit", room, ev, f"too late {stamp}", "--since", since(), "--yes")
    step("edit of a deleted message is refused", code == 1 and "NOT EDITED" in out
         and "Edited. event_id" not in out, out)

    ok = all(results)
    print(f"\n{sum(results)}/{len(results)} passed")
    print("RESULT: PASS" if ok else "RESULT: FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
