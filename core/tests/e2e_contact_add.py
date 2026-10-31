#!/usr/bin/env python3
"""Live proof of `comms contact` on this Mac's real tools. It reaches no person.

It makes one iCloud card for a 555-01xx number and an example.invalid address, checks it through
the real helper, runs one WhatsApp send preview (no --yes) to the owner's own self-DM number, reads one
Slack name from the bridge without saving it, and deletes all of it, even on a failure.

It also proves the edit paths on the real books: a replaced number reaches the real card and the
real Google contact while a second number added by hand (and a Google note) survives; a rename
reaches both; `find` and `list` read each book; a card from before the tool changes only with
--update-card. Every row is a ZZ name with a 555 number and an example.invalid address. After the
run the directory, the iCloud store and Google must hold no ZZ row: the script checks that.

Run from your terminal app (launchd has no Contacts grant):

    python3 e2e_contact_add.py [SLACK_WORKSPACE "SLACK NAME"]

Prints PASS or FAIL per step. Exit 0 only when no step failed.
"""
import hashlib
import json
import re
import os
import subprocess
import sys

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # kit: code path
sys.path.insert(0, CODE)
import commslib as C  # noqa: E402
import contactbook  # noqa: E402
import kitconfig  # noqa: E402

COMMS = os.path.join(CODE, "bin", "comms")
HELPER = contactbook._apple_bin()
WHATSAPP = C.channel_cli("whatsapp")
ROUTES = C.ROUTES
CARD = "ZZ Test Delete"
RENAMED = "ZZ Test Renamed"
OLD_CARD = "ZZ Old Card"
SELF = "ZZ Self Test"
PHONE = "+12065550142"
SECOND = "+12065550143"       # added by hand, must survive the edit
EDITED = "+12065550144"
OLD_PHONE, OLD_EDITED = "+12065550145", "+12065550146"
EMAIL = "zz-test@example.invalid"
# the owner's own number, from the kit config. Missing = stop: never default to anyone
SELF_NUMBER = re.sub(r"\D", "", str(kitconfig.require("whatsapp.self_number")))

failures = 0


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180)


def comms(*args):
    return run("python3", COMMS, "contact", *args)


def check(label, ok, detail=""):
    global failures
    failures += 0 if ok else 1
    print("%s  %s%s" % ("PASS" if ok else "FAIL", label, "  [%s]" % detail if detail and not ok else ""))


def routes_hash():
    with open(ROUTES, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def cards():
    return json.loads(run(HELPER, "find", "--phone", PHONE, "--email", EMAIL).stdout)["by_handle"]


def apple_rows(text):
    return json.loads(run(HELPER, "search", "--text", text).stdout).get("rows", [])


def google_person(resource):
    done = run("gws", "people", "people", "get", "--params", json.dumps(
        {"resourceName": resource, "personFields": "names,phoneNumbers,emailAddresses,biographies"}))
    return json.loads(done.stdout or "{}")


def google_numbers(person):
    return sorted(p.get("value", "").replace(" ", "").replace("-", "") for p in person.get("phoneNumbers") or [])


def find_rows(text, source):
    out = comms("find", text, "--source", source, "--json")
    return json.loads(out.stdout or "{}").get("rows", [])


def leftovers():
    """Every ZZ row left in any book, as text."""
    left = []
    for text in ("ZZ Test", "ZZ Old", "ZZ Self", "2065550142", "2065550143", "2065550144",
                 "2065550145", "2065550146"):
        left += ["apple %s" % r["name"] for r in apple_rows(text)]
        left += ["google %s" % r["name"] for r in find_rows(text, "google")]
        left += ["directory %s" % r["name"] for r in find_rows(text, "directory")]
    return sorted(set(left))


before = routes_hash()
status = json.loads(run(HELPER, "status").stdout)
check("Contacts access is on and an iCloud container exists",
      status.get("auth") == "authorized" and bool(status.get("icloud_container")), str(status))
if cards() or leftovers():
    sys.exit("A ZZ test row or a 555-01 number is already in a book (%s). Stopped, nothing touched."
             % leftovers())
current, old_card_id = CARD, None

try:
    out = comms("add", CARD, "--phone", "(206) 555-0142", "--email", EMAIL, "--google",
                "--upwork-room", "room_zztest", "--linkedin", "https://www.linkedin.com/in/zz-test-delete/")
    print(out.stdout.rstrip())
    check("add saves the directory and the card (exit 0, or 3 for Google only)",
          out.returncode in (0, 3), out.stdout + out.stderr)
    found = cards()
    check("exactly one real card holds the number and the email", len(found) == 1, str(found))
    check("the card has the right name", bool(found) and found[0]["name"] == CARD, str(found))
    scopes = json.loads(run("gws", "auth", "status").stdout).get("scopes") or []
    google_id = (json.loads(comms("list", CARD, "--json").stdout)[0] or {}).get("google_id")
    if "https://www.googleapis.com/auth/contacts" in scopes:
        check("Google saved a real contact", bool(google_id), out.stdout)
        read = json.loads(run("gws", "people", "people", "get", "--params", json.dumps(
            {"resourceName": google_id, "personFields": "names,emailAddresses"})).stdout or "{}")
        check("Google reads back the right name",
              (read.get("names") or [{}])[0].get("displayName") == CARD, str(read)[:200])
        check("the add exits 0 with Google on", out.returncode == 0, out.stdout)
    else:
        check("no contacts scope prints the one login line", "gws auth login --scopes" in out.stdout, out.stdout)
        print("note  Google live save: blocked by the missing contacts scope")

    expected = {"whatsapp": "12065550142", "imessage": PHONE, "gmail": EMAIL, "upwork": "room_zztest",
                "linkedin": "https://www.linkedin.com/in/zz-test-delete/"}
    for channel, value in expected.items():
        got = comms("get", CARD, "--channel", channel).stdout.strip()
        check("get --channel %s" % channel, got == value, got)
    check("a partial name returns no handle", comms("get", "ZZ Test", "--channel", "gmail").returncode == 1)
    check("a second name for that number is refused", comms("add", "ZZ Other", "--phone", PHONE).returncode == 1)
    comms("add", CARD, "--phone", PHONE)
    check("adding again makes no second card", len(cards()) == 1, str(cards()))

    comms("add", SELF, "--phone", "+" + SELF_NUMBER, "--no-apple")
    wa = run("python3", "-P", WHATSAPP, "search", SELF)
    check("WhatsApp search shows a saved contact", SELF_NUMBER in wa.stdout, wa.stdout + wa.stderr)
    wa = run("python3", "-P", WHATSAPP, "send", SELF, "dry run, never sent")
    first = next((l for l in wa.stdout.splitlines() if "About to send to" in l), "")
    check("WhatsApp send by saved name previews the right number, group-free",
          "About to send to %s (%s)" % (SELF_NUMBER, SELF) in first, wa.stdout + wa.stderr)


    # ---- edit: the number moves on the real card and the real Google contact; the rest stays
    card_id = cards()[0]["id"]
    made = run(HELPER, "update", "--id", card_id, "--expect-name", CARD, "--add-phone", SECOND)
    check("a second number added by hand through the helper", SECOND.replace("+1", "") in
          "".join(re.sub(r"\D", "", p) for p in (apple_rows(CARD) or [{"phones": []}])[0]["phones"]), made.stdout)
    google_id = (json.loads(comms("list", CARD, "--json").stdout)[0] or {}).get("google_id")
    if google_id:
        person = google_person(google_id)
        put = run("gws", "people", "people", "updateContact", "--params", json.dumps(
            {"resourceName": google_id, "updatePersonFields": "phoneNumbers,biographies"}), "--json",
            json.dumps({"etag": person["etag"], "biographies": [{"value": "zz note"}],
                        "phoneNumbers": person.get("phoneNumbers", []) + [{"value": SECOND, "type": "work"}]}))
        check("a second number and a note added by hand on the Google contact", put.returncode == 0, put.stdout[:200])
    edit = comms("add", CARD, "--phone", EDITED, "--replace")
    print(edit.stdout.rstrip())
    check("the edit exits 0", edit.returncode == 0, edit.stdout + edit.stderr)
    row = (apple_rows(CARD) or [{}])[0]
    phones = sorted(re.sub(r"\D", "", p) for p in row.get("phones", []))
    check("the real card now has the new number and the hand-added second one, not the old one",
          phones == ["12065550143", "12065550144"] or phones == ["2065550143", "2065550144"], str(phones))
    check("the real card kept its email", EMAIL in row.get("emails", []), str(row))
    if google_id:
        person = google_person(google_id)
        check("the real Google contact has the new number and the second one, not the old one",
              google_numbers(person) == sorted([EDITED, SECOND]), str(google_numbers(person)))
        check("the Google note survived the edit",
              (person.get("biographies") or [{}])[0].get("value") == "zz note", str(person.get("biographies")))
        check("the Google email survived the edit",
              (person.get("emailAddresses") or [{}])[0].get("value") == EMAIL, str(person.get("emailAddresses")))

    # ---- find and list read each real book
    for source in ("directory", "apple", "google"):
        if source == "google" and not google_id:
            continue
        got = find_rows("(206) 555-0144", source)
        check("find by number in %s" % source,
              [r["name"] for r in got] == [CARD] and got[0]["source"] == source, str(got))
    many = comms("list", "--source", "apple", "--limit", "2", "--json")
    data = json.loads(many.stdout or "{}")
    check("list --source apple shows 2 rows and the true total",
          len(data.get("rows", [])) == 2 and data.get("total", 0) > 2, many.stdout[:200])
    if google_id:
        data = json.loads(comms("list", "--source", "google", "--limit", "2", "--json").stdout or "{}")
        check("list --source google shows 2 rows and the true total",
              len(data.get("rows", [])) == 2 and data.get("total", 0) > 2, str(data)[:200])
    check("find with one source named leaves the others unasked",
          all(r["source"] == "apple" for r in find_rows("ZZ Test", "apple")))

    # ---- rename
    out = comms("rename", CARD, RENAMED)
    print(out.stdout.rstrip())
    current = RENAMED
    check("rename exits 0", out.returncode == 0, out.stdout + out.stderr)
    check("rename onto an existing name is refused",
          comms("rename", RENAMED, SELF).returncode == 1)
    listed = json.loads(comms("list", "zz test", "--json").stdout or "[]")
    check("the directory row moved", [r["name"] for r in listed] == [RENAMED], str(listed))
    check("the real card has the new name", [r["name"] for r in apple_rows("2065550144")] == [RENAMED])
    if google_id:
        check("the real Google contact has the new name",
              (google_person(google_id).get("names") or [{}])[0].get("displayName") == RENAMED)
        check("the rename kept the Google numbers", google_numbers(google_person(google_id)) == sorted([EDITED, SECOND]))

    # ---- a card from before this tool changes only with --update-card
    container = json.loads(run(HELPER, "status").stdout)["icloud_container"]
    old_card_id = json.loads(run(HELPER, "add", "--given", "ZZ Old", "--family", "Card", "--container",
                                 container, "--phone", OLD_PHONE).stdout)["id"]
    comms("add", OLD_CARD, "--phone", OLD_PHONE)
    out = comms("add", OLD_CARD, "--phone", OLD_EDITED, "--replace")
    check("a card from before the tool stays unchanged and says so",
          "--update-card" in out.stdout and OLD_PHONE.replace("+1", "") in
          "".join(re.sub(r"\D", "", p) for p in apple_rows("ZZ Old")[0]["phones"]), out.stdout)
    out = comms("add", OLD_CARD, "--phone", OLD_EDITED, "--update-card")
    nums = ["".join(re.sub(r"\D", "", p)) for p in apple_rows("ZZ Old")[0]["phones"]]
    check("--update-card then changes it", len(nums) == 1 and nums[0].endswith("2065550146"), out.stdout + str(nums))

    if len(sys.argv) == 3:
        sl = comms("add", sys.argv[2], "--slack", sys.argv[1], "--no-apple", "--dry-run")
        check("the real Slack bridge finds the exact person, nothing saved",
              sl.returncode == 0 and "slack" in sl.stdout, sl.stdout + sl.stderr)
finally:
    comms("rm", SELF)
    google_id = next((r.get("google_id") for r in json.loads(comms("list", "ZZ Test", "--json").stdout or "[]")), None)
    for name in (RENAMED, CARD):
        rm = comms("rm", name, "--apple", "--google")
        print(rm.stdout.rstrip())
    comms("rm", OLD_CARD)
    if old_card_id:
        run(HELPER, "delete", "--id", old_card_id, "--expect-name", "ZZ Old Card")

check("rm deleted the real card", cards() == [], str(cards()))
check("no ZZ row is left in the directory, the iCloud store or Google", leftovers() == [], str(leftovers()))
if google_id:
    gone = json.loads(run("gws", "people", "people", "get", "--params", json.dumps(
        {"resourceName": google_id, "personFields": "names"})).stdout or "{}")
    check("rm deleted the real Google contact", (gone.get("error") or {}).get("code") == 404, str(gone)[:200])
listed = json.loads(comms("list", "zz", "--json").stdout or "[]")
check("the directory holds no test row", listed == [], str(listed))
check("routes.json did not change", routes_hash() == before)
print("RESULT: %s" % ("PASS" if not failures else "%d FAILED" % failures))
sys.exit(1 if failures else 0)
