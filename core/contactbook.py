"""`comms contact`: one directory of the people the owner talks to, and their handle on each channel.

`contacts.json` maps an exact name to handles. `add` also writes the real address books: an Apple
card by default, because iCloud carries it to the iPhone and WhatsApp reads the iPhone's address
book, and a Google contact with --google. WhatsApp has no call that saves a contact, so that card
IS how a contact gets added there. Slack, Upwork and LinkedIn have no address book to write.

An edit (`add --replace`) and `rename` reach the linked Apple card and Google contact too. They
change only the value that moved: a second number, note, photo or birthday on the card stays. A
card that existed before this tool is changed only with --update-card. `find` and `list --source`
read the real books and print to the terminal only.

Exit codes: 0 done; 1 refused, nothing saved; 2 (`get`, `wa-check`) saved, but no handle on that
channel; 3 the directory is saved, but an address book or a route asked for did not work (`find` and
`list --source`: one source did not answer, the others printed; `wa-check`: WhatsApp holds no name
or another name yet); 4 (`wa-check`) the names could not be read, so the answer is unknown.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

import commslib as C
import kitconfig as K

CONTACTS = os.path.join(C.ROOT, "contacts.json")
SWIFT_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)),  # kit: code path
                            "apple_contacts.swift")
# the compiled helper is a build product, kept in the data folder with everything else
APPLE_BUILD = os.path.join(C.ROOT, "apple-contacts")
SLACK_BRIDGE = C.channel_cli("slack")
# one folder per project, each holding that project's CLIENT.md ledger
PROJECT_FOLDERS = K.path("ledger_dir") or os.path.join(C.ROOT, "projects")
CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts"
WA_CHECK_TIMEOUT_S = 30


def _gws_as():
    """gws-as in ~/.local/bin (the installed layout), then on PATH, then beside this kit (core/bin)."""
    own = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "gws-as")  # kit: code path
    for path in (os.path.expanduser("~/.local/bin/gws-as"), shutil.which("gws-as"), own):
        if path and os.path.exists(path):
            return path
    return "gws-as"


GWS_AS = _gws_as()
GOOGLE_FIELDS = "names,phoneNumbers,emailAddresses"

# Everything that identifies a person. A slack workspace does not: many people share one.
IDENTITY_FIELDS = ("phone", "email", "whatsapp", "imessage", "slack_user", "slack_dm",
                   "upwork_room", "linkedin")
ROUTE_FIELDS = ("phone", "email", "whatsapp", "imessage", "slack_dm", "upwork_room")


# ---------------------------------------------------------------- pure logic

def clean_name(name):
    return re.sub(r"\s+", " ", name or "").strip()


def name_key(name):
    return clean_name(name).casefold()


def split_name(name):
    parts = clean_name(name).split(" ")
    return (name, "") if len(parts) == 1 else (" ".join(parts[:-1]), parts[-1])


def e164(raw):
    """The number with its country code, or ValueError. Never a guess.

    This is NOT C.norm. norm drops the US `1` so two spellings key the same, which is right for
    finding duplicates and wrong for sending: WhatsApp reads 2065550100 as Egypt (+20).
    """
    text = str(raw).strip()
    if not re.fullmatch(r"[0-9+\-() .]+", text):
        raise ValueError("%r is not a phone number" % raw)
    digits = re.sub(r"[^0-9]", "", text)
    if text.startswith("+"):
        if 8 <= len(digits) <= 15 and digits[0] != "0":
            return "+" + digits
        raise ValueError("%r is not a full international number" % raw)
    # Bare numbers are read as US only when they have the shape of a real US number.
    if len(digits) == 10 and digits[0] in "23456789" and digits[3] in "23456789":
        return "+1" + digits
    if len(digits) == 11 and digits[0] == "1" and digits[1] in "23456789" and digits[4] in "23456789":
        return "+" + digits
    raise ValueError("%r has no country code. Write it like +1 206 555 0100" % raw)


def clean_email(raw):
    text = str(raw).strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
        raise ValueError("%r is not an email address" % raw)
    return text


def channels_of(entry):
    """The handle each channel's own command takes. Worked out here on each read, so a replaced
    phone number cannot leave a stale copy behind on another channel."""
    h = entry.get("handles") or {}
    out = {}
    whatsapp = h.get("whatsapp") or h.get("phone")
    if whatsapp:
        out["whatsapp"] = whatsapp.lstrip("+")
    imessage = h.get("imessage") or h.get("phone") or h.get("email")
    if imessage:
        out["imessage"] = imessage
    for channel, field in (("gmail", "email"), ("slack", "slack_dm"),
                           ("upwork", "upwork_room"), ("linkedin", "linkedin")):
        if h.get(field):
            out[channel] = h[field]
    return out


def owner_of_handle(book, key, handles):
    """(person, handle) when a handle already belongs to someone else, or None."""
    wanted = {C.norm(handles[f]): handles[f] for f in IDENTITY_FIELDS if handles.get(f)}
    for other_key, other in book.items():
        if other_key == key:
            continue
        theirs = other.get("handles") or {}
        for field in IDENTITY_FIELDS:
            hit = wanted.get(C.norm(theirs.get(field)))
            if hit:
                return other.get("name"), hit
    return None


def changed_fields(entry, handles):
    """Fields where this add would overwrite a different value."""
    old = (entry or {}).get("handles") or {}
    return [f for f, value in handles.items()
            if old.get(f) and C.norm(old[f]) != C.norm(value)]


def _is_email(value):
    return "@" in value


def _same(a, b):
    if _is_email(a) and _is_email(b):
        return a.strip().lower() == b.strip().lower()
    return C.norm(a) == C.norm(b)


APPLE_FIELDS = (("phone", "whatsapp", "imessage"), ("email", "imessage"))
GOOGLE_HANDLE_FIELDS = (("phone", "whatsapp"), ("email",))


def plan_ops(old, new, fields):
    """The changes an address book needs when handles go from `old` to `new`, as ops:
    ("rp", old, new) replace a phone, ("ap", new) add one, ("re", ...) and ("ae", ...) for emails.
    A value that stays is not in the list, so everything else on the card is left as it is."""
    ops = []
    for kind, group in (("p", fields[0]), ("e", fields[1])):
        want_email = kind == "e"

        def values(handles):
            return [handles[f] for f in group if handles.get(f) and _is_email(handles[f]) == want_email]

        old_all, new_all = values(old), values(new)
        for field in group:
            before, after = old.get(field), new.get(field)
            if not after or _is_email(after) != want_email:
                continue
            had = before and _is_email(before) == want_email
            if had and not _same(before, after):
                # the old value leaves the card unless another field still uses it
                ops.append(("a" + kind, after) if any(_same(before, v) for v in new_all)
                           else ("r" + kind, before, after))
            elif not had and not any(_same(after, v) for v in old_all):
                ops.append(("a" + kind, after))
    unique = []
    for op in ops:
        if op not in unique:
            unique.append(op)
    return unique


def merge_ops(pending, ops):
    out = [list(op) for op in (pending or [])]
    for op in ops:
        if list(op) not in out:
            out.append(list(op))
    return out


def parse_handles(a):
    """The validated handles typed on the command line. ValueError: save nothing."""
    h = {}
    if a.phone:
        h["phone"] = e164(a.phone)
    if a.email:
        h["email"] = clean_email(a.email)
    if a.whatsapp:
        h["whatsapp"] = e164(a.whatsapp)
    if a.imessage:
        h["imessage"] = clean_email(a.imessage) if "@" in a.imessage else e164(a.imessage)
    if a.upwork_room:
        if not re.fullmatch(r"[A-Za-z0-9_:.\-]+", a.upwork_room):
            raise ValueError("%r is not an Upwork room id" % a.upwork_room)
        h["upwork_room"] = a.upwork_room
    if a.linkedin:
        if "linkedin.com/" not in a.linkedin.lower():
            raise ValueError("%r is not a LinkedIn URL" % a.linkedin)
        h["linkedin"] = a.linkedin.strip()
    if a.slack_dm:
        if not re.fullmatch(r"D[A-Z0-9]+", a.slack_dm):
            raise ValueError("%r is not a Slack DM id (D...)" % a.slack_dm)
        h["slack_dm"] = a.slack_dm
    return h


# ---------------------------------------------------------------- slack

def _slack(*args):
    override = os.environ.get("COMMS_SLACK_BRIDGE")
    cmd = [override] if override else [sys.executable, "-P", SLACK_BRIDGE]
    done = subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise ValueError("the Slack bridge did not answer: %s" % (done.stderr or done.stdout).strip()[:200])
    return [[cell.strip() for cell in line.split(" | ")] for line in done.stdout.splitlines()]


def slack_user(workspace, name):
    rows = [r for r in _slack("contacts", "--workspace", workspace, "--name", name) if len(r) == 5]
    exact = [r for r in rows if name_key(r[1]) == name_key(name)]
    if len(exact) != 1:
        people = "; ".join("%s (%s)" % (r[1], r[3]) for r in (exact or rows)[:5]) or "nobody close"
        raise ValueError("%d Slack people in %s are named exactly %s: %s. Pass --slack %s:USER_ID"
                         % (len(exact), workspace, name, people, workspace))
    return exact[0][3]


def slack_dm(workspace, user):
    try:
        rows = _slack("channels", "--workspace", workspace, "--type", "im", "--limit", "100000")
    except (ValueError, OSError, subprocess.SubprocessError):
        return None
    for row in rows:
        if len(row) >= 5 and row[4] == user:
            return row[3]
    return None


def add_slack(a, name, handles):
    if not a.slack:
        return
    workspace, _, user = a.slack.partition(":")
    if user and not re.fullmatch(r"[UW][A-Z0-9]+", user):
        raise ValueError("%r is not a Slack user id (U...)" % user)
    user = user or slack_user(workspace, name)
    handles["slack_workspace"] = workspace
    handles["slack_user"] = user
    dm = handles.get("slack_dm") or slack_dm(workspace, user)
    if dm:
        handles["slack_dm"] = dm


# ---------------------------------------------------------------- apple

def _apple_bin():
    override = os.environ.get("COMMS_APPLE_CONTACTS_BIN")
    if override:
        return override
    if (not os.path.exists(APPLE_BUILD)
            or os.path.getmtime(APPLE_BUILD) < os.path.getmtime(SWIFT_SOURCE)):
        os.makedirs(os.path.dirname(APPLE_BUILD), exist_ok=True)
        subprocess.run(["swiftc", "-O", SWIFT_SOURCE, "-o", APPLE_BUILD], check=True,
                       capture_output=True, timeout=300)
    return APPLE_BUILD


def _apple(*args):
    done = subprocess.run([_apple_bin()] + list(args), capture_output=True, text=True, timeout=60)
    try:
        data = json.loads(done.stdout)
    except ValueError:
        data = {"error": (done.stderr or done.stdout).strip()[:200]}
    return done.returncode, data


def apple_target():
    """(iCloud container id, None) or (None, why not). Touches no card."""
    _, status = _apple("status")
    if status.get("auth") != "authorized":
        return None, ("this process has no Contacts access (%s). Run it from your terminal app; a "
                      "launchd job has no grant" % status.get("auth", status.get("error")))
    if not status.get("icloud_container"):
        return None, ("no iCloud container on this Mac. A card saved here would never reach the "
                      "iPhone or WhatsApp")
    return status["icloud_container"], None


def apple_handle_flags(handles):
    phones, emails = [], []
    for field in ("phone", "whatsapp", "imessage"):
        value = handles.get(field)
        if value and "@" not in value and value not in phones:
            phones.append(value)
    for field in ("email", "imessage"):
        value = handles.get(field)
        if value and "@" in value and value not in emails:
            emails.append(value)
    flags = []
    for value in phones:
        flags += ["--phone", value]
    for value in emails:
        flags += ["--email", value]
    return flags


def apple_save(entry):
    """Returns (ok, text) and sets the card id on the entry."""
    try:
        container, why = apple_target()
        if why:
            return False, "not saved: " + why
        flags = apple_handle_flags(entry["handles"])
        rc, found = _apple("find", "--name", entry["name"], *flags)
        if rc:
            return False, "not saved: %s" % found.get("error")
        if found.get("by_handle"):
            card = found["by_handle"][0]
            entry["apple_id"], entry["apple_created"] = card["id"], False
            return True, "a card already has this number or email (%s). Card not changed." % card["name"]
        if found.get("by_name"):
            return False, ("not saved: a card with the same name has other details. Card not "
                           "changed. Open Contacts to merge, or add again with a fuller name.")
        given, family = split_name(entry["name"])
        rc, made = _apple("add", "--given", given, "--family", family, "--container", container, *flags)
        if rc:
            return False, "not saved: %s" % made.get("error")
        entry["apple_id"], entry["apple_created"] = made["id"], True
        return True, ("added to iCloud. The iPhone gets it, then WhatsApp shows the name if that "
                      "number is on WhatsApp.")
    except (OSError, subprocess.SubprocessError) as error:
        return False, "not saved: %s" % error


def apple_ops_flags(ops):
    names = {"rp": "--replace-phone", "ap": "--add-phone", "re": "--replace-email", "ae": "--add-email"}
    flags = []
    for op in ops:
        flags += [names[op[0]], "=".join(op[1:]) if op[0][0] == "r" else op[1]]
    return flags


def apple_update(entry, ops, rename_from=None, rename_to=None):
    """Change the linked card in place. Returns (ok, text). The helper checks the card still
    carries the name or a number or email we know, so a stale id cannot edit a stranger."""
    try:
        container, why = apple_target()
        if why:
            return False, "card not updated: " + why
        h = entry["handles"]
        flags = apple_ops_flags(ops)
        for name in dict.fromkeys(n for n in (rename_from, rename_to, entry["name"]) if n):
            flags += ["--expect-name", name]
        known = [h[f] for f in ("phone", "email", "whatsapp", "imessage") if h.get(f)]
        for op in ops:
            known += list(op[1:])
        for value in dict.fromkeys(known):
            flags += ["--expect-handle", value]
        if rename_to:
            given, family = split_name(rename_to)
            flags += ["--given", given, "--family", family]
        rc, out = _apple("update", "--id", entry["apple_id"], *flags)
        if rc:
            return False, "card not updated: %s" % out.get("error")
        return True, "card updated (%s)" % (", ".join(dict.fromkeys(out.get("changes") or [])) or "already right")
    except (OSError, subprocess.SubprocessError) as error:
        return False, "card not updated: %s" % error


def apple_delete(entry):
    if not entry.get("apple_id"):
        return True, "no card is linked to this contact"
    if not entry.get("apple_created"):
        return False, "that card was there before this tool, so it stays. Nothing was removed."
    try:
        rc, out = _apple("delete", "--id", entry["apple_id"], "--expect-name", entry["name"])
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)
    if rc:
        return False, "card not deleted: %s" % out.get("error")
    entry.pop("apple_id", None)
    entry.pop("apple_created", None)
    return True, "card deleted"


# ---------------------------------------------------------------- google

def _gws(*args, account=None):
    """gws for the main login, or gws-as for another account. gws-as refuses an unknown account;
    its words come back to the caller as they are."""
    if account:
        return subprocess.run([GWS_AS, account] + list(args), capture_output=True, text=True, timeout=60)
    binary = shutil.which("gws") or "gws"
    return subprocess.run([binary] + list(args), capture_output=True, text=True, timeout=60)


def _login_scopes(account=None):
    """The scopes the login ASKED for. Google can grant fewer: a consent box can stay unticked."""
    try:
        return json.loads(_gws("auth", "status", account=account).stdout).get("scopes") or []
    except (ValueError, OSError, subprocess.SubprocessError, AttributeError):
        return []


def _scope_refused(done):
    return re.search(r"insufficient authentication scopes|ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                     done.stdout + done.stderr) is not None


def _cache_dir(account):
    if account:
        return os.path.expanduser("~/.config/gws-accounts/%s" % account)
    return os.environ.get("GOOGLE_WORKSPACE_CLI_CONFIG_DIR") or os.path.expanduser("~/.config/gws")


def _gws_contacts(*args, account=None):
    """gws for a contacts call. `gws auth login` leaves the old access token in token_cache.json
    and gws uses it until it expires, so a login that adds the scope still fails for up to an hour.
    When the login asked for the scope, one retry on an empty cache tells that from a refusal.
    Only that account's cache goes: the login itself is in credentials.enc, and gws rebuilds it."""
    done = _gws(*args, account=account)
    if _scope_refused(done) and CONTACTS_SCOPE in _login_scopes(account):
        try:
            os.remove(os.path.join(_cache_dir(account), "token_cache.json"))
        except OSError:
            return done
        done = _gws(*args, account=account)
    return done


def google_login_line():
    """The one command that adds the contacts scope. It repeats every scope the login holds now,
    read live, because `gws auth login --scopes` replaces the list."""
    scopes = _login_scopes()
    if not scopes:
        return "gws auth status failed, so run it first and keep every scope it lists."
    if CONTACTS_SCOPE not in scopes:
        scopes = scopes + [CONTACTS_SCOPE]
    return "! gws auth login --scopes " + ",".join(scopes)


def _google_failure(done, verb, account=None):
    """The text for a failed Google call. Scope trouble gets the one fix that works."""
    if _scope_refused(done):
        if account:
            return ("%s: Google did not grant Contacts for %s. Log that account in again with the "
                    "Contacts box ticked." % (verb, account))
        if CONTACTS_SCOPE in _login_scopes():
            return ("%s: Google did not grant Contacts. Run this once, tick the "
                    "Contacts box, then press Allow:\n    %s" % (verb, google_login_line()))
        return ("%s: the Google login has no contacts scope. Run this once, "
                "then press Allow:\n    %s" % (verb, google_login_line()))
    return "%s: %s" % (verb, (done.stdout + done.stderr).strip()[:200])


def google_save(entry, account=None):
    if entry.get("google_id"):
        return True, "already linked. Contact not changed."
    h = entry["handles"]
    given, family = split_name(entry["name"])
    body = {"names": [{"givenName": given, "familyName": family}]}
    if h.get("email"):
        body["emailAddresses"] = [{"value": h["email"]}]
    numbers = [h[f] for f in ("phone", "whatsapp") if h.get(f)]
    if numbers:
        body["phoneNumbers"] = [{"value": n, "type": "mobile"} for n in dict.fromkeys(numbers)]
    try:
        done = _gws_contacts("people", "people", "createContact", "--params",
                             json.dumps({"personFields": "names"}), "--json", json.dumps(body),
                             account=account)
        made = json.loads(done.stdout or "{}")
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        return False, "not saved: %s" % error
    if done.returncode or not made.get("resourceName"):
        return False, _google_failure(done, "not saved", account)
    entry["google_id"] = made["resourceName"]
    if account:
        entry["google_account"] = account
    return True, "added"


def google_delete(entry):
    if not entry.get("google_id"):
        return True, "no Google contact is linked to this contact"
    try:
        done = _gws_contacts("people", "people", "deleteContact", "--params",
                             json.dumps({"resourceName": entry["google_id"]}),
                             account=entry.get("google_account"))
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)
    if done.returncode:
        return False, "not deleted: %s" % (done.stdout + done.stderr).strip()[:200]
    entry.pop("google_id", None)
    entry.pop("google_account", None)
    return True, "Google contact deleted"


def _is_stale(done):
    return re.search(r"etag|FAILED_PRECONDITION", done.stdout + done.stderr, re.I) is not None


def _apply_google_ops(person, ops):
    """(body, fields): the lists that changed, with every untouched entry as Google sent it."""
    body, fields = {}, []
    for kind, key, listname in (("p", "value", "phoneNumbers"), ("e", "value", "emailAddresses")):
        mine = [op for op in ops if op[0][1] == kind]
        if not mine:
            continue
        items = [dict(i) for i in person.get(listname) or []]
        before = json.dumps(items, sort_keys=True)

        def find(value):
            return next((n for n, i in enumerate(items) if _same(i.get("value"), value)), None)

        for op in mine:
            if op[0][0] == "r":
                old_at, new_at = find(op[1]), find(op[2])
                if old_at is not None and new_at is None:
                    items[old_at]["value"] = op[2]
                    items[old_at].pop("canonicalForm", None)
                    items[old_at].pop("formattedType", None)
                elif old_at is not None:
                    del items[old_at]
                elif new_at is None:
                    items.append({"value": op[2], "type": "mobile"} if kind == "p" else {"value": op[2]})
            elif find(op[1]) is None:
                items.append({"value": op[1], "type": "mobile"} if kind == "p" else {"value": op[1]})
        if json.dumps(items, sort_keys=True) != before:
            body[listname] = items
            fields.append(listname)
    return body, fields


def google_update(entry, ops, rename_to=None):
    """Change the linked Google contact. Reads it first for its etag and edits only the lists that
    changed. A stale etag (someone saved in between) is read again and retried once."""
    account = entry.get("google_account")
    resource = entry["google_id"]
    done = None
    for attempt in (0, 1):
        try:
            got = _gws_contacts("people", "people", "get", "--params",
                                json.dumps({"resourceName": resource, "personFields": GOOGLE_FIELDS}),
                                account=account)
            person = json.loads(got.stdout or "{}")
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            return False, "Google contact not updated: %s" % error
        if got.returncode or not person.get("etag"):
            return False, _google_failure(got, "Google contact not updated", account)
        body, fields = _apply_google_ops(person, ops)
        if rename_to:
            given, family = split_name(rename_to)
            names = [dict(n) for n in person.get("names") or [{}]]
            for key in ("displayName", "displayNameLastFirst", "unstructuredName", "middleName"):
                names[0].pop(key, None)
            names[0]["givenName"], names[0]["familyName"] = given, family
            body["names"] = names
            fields.append("names")
        if not fields:
            return True, "already right"
        body["etag"] = person["etag"]
        try:
            done = _gws_contacts("people", "people", "updateContact", "--params",
                                 json.dumps({"resourceName": resource,
                                             "updatePersonFields": ",".join(fields)}),
                                 "--json", json.dumps(body), account=account)
        except (OSError, subprocess.SubprocessError) as error:
            return False, "Google contact not updated: %s" % error
        if not done.returncode:
            return True, "updated (%s)" % ", ".join(fields)
        if attempt == 0 and _is_stale(done):
            continue
        break
    return False, _google_failure(done, "Google contact not updated", account)


def google_people(account=None, limit=None, query=None):
    """(rows, total, None) or (None, None, why). Reads the Google contacts; writes nothing.
    With a query it reads every page and filters here; without one it stops at `limit`."""
    rows, total, token = [], None, None
    for _ in range(60):
        size = 1000 if (query or not limit) else min(limit - len(rows), 1000)
        params = {"resourceName": "people/me", "personFields": GOOGLE_FIELDS, "pageSize": size,
                  "sortOrder": "FIRST_NAME_ASCENDING"}
        if token:
            params["pageToken"] = token
        try:
            done = _gws_contacts("people", "people", "connections", "list", "--params",
                                 json.dumps(params), account=account)
            page = json.loads(done.stdout or "{}")
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            return None, None, str(error)
        if done.returncode:
            return None, None, _google_failure(done, "not read", account).replace("\n", " ")
        total = page.get("totalItems", page.get("totalPeople", total))
        for person in page.get("connections") or []:
            rows.append({"id": person.get("resourceName"),
                         "name": ((person.get("names") or [{}])[0].get("displayName") or ""),
                         "phones": [p.get("value") for p in person.get("phoneNumbers") or []],
                         "emails": [e.get("value") for e in person.get("emailAddresses") or []]})
        token = page.get("nextPageToken")
        if not token or (not query and limit and len(rows) >= limit):
            break
    if query:
        rows = [r for r in rows if row_matches(r, query)]
        total = len(rows)
    elif total is None:
        total = len(rows)
    return rows, total, None


# ---------------------------------------------------------------- find / list

def _digits(text):
    digits = re.sub(r"\D", "", text or "")
    return digits[1:] if len(digits) == 11 and digits[0] == "1" else digits


def row_matches(row, text):
    """A name or an email contains the text, or a number contains its digits (3 or more)."""
    query = clean_name(text).casefold()
    if query in (row.get("name") or "").casefold():
        return True
    if any(query in (e or "").casefold() for e in row.get("emails") or []):
        return True
    if re.fullmatch(r"[0-9+\-() .]+", text.strip()):
        wanted = _digits(text)
        return len(wanted) >= 3 and any(wanted in _digits(p) for p in row.get("phones") or [])
    return False


def directory_rows(book, text):
    rows = []
    for _, entry in sorted(book.items()):
        h = entry.get("handles") or {}
        row = {"source": "directory", "id": None, "name": entry.get("name"),
               "phones": [v for f in ("phone", "whatsapp", "imessage") for v in [h.get(f)] if v and "@" not in v],
               "emails": [v for f in ("email", "imessage") for v in [h.get(f)] if v and "@" in v]}
        others = [str(h[f]) for f in ("slack_user", "slack_dm", "upwork_room", "linkedin") if h.get(f)]
        if text is None or row_matches(row, text) or any(text.casefold() in o.casefold() for o in others):
            rows.append(row)
    return rows


def apple_rows(text, limit):
    """(rows, total, why). text None lists every card."""
    try:
        if text is None:
            rc, out = _apple("list", "--limit", str(limit))
        else:
            rc, out = _apple("search", "--text", text, "--limit", str(limit))
    except (OSError, subprocess.SubprocessError) as error:
        return None, None, str(error)
    if rc:
        if out.get("error") == "no_access":
            return None, None, ("no Contacts access (%s). Run it from your terminal app; a launchd "
                                "job has no grant" % out.get("auth"))
        return None, None, str(out.get("error"))
    rows = [dict(r, source="apple") for r in out.get("rows") or []]
    return rows, out.get("total", len(rows)), None


def google_rows(text, limit, account=None):
    rows, total, why = google_people(account, limit, text)
    if why:
        return None, None, why
    rows = [dict(r, source="google") for r in rows]
    return (rows[:limit] if limit else rows), total, None


def _print_rows(rows):
    for row in rows:
        details = "  ".join(list(row.get("phones") or []) + list(row.get("emails") or []))
        print("%-10s %-24s %s" % (row["source"], row.get("name") or "", details))


def cmd_find(a):
    text = clean_name(a.text)
    if not text:
        print("give some text to look for: a name, a number or an email")
        return 1
    wanted = ["directory", "apple", "google"] if a.source == "all" else [a.source]
    rows, errors = [], {}
    for source in wanted:
        try:
            if source == "directory":
                book = _load()
                if book is None:
                    errors[source] = "%s is unreadable" % CONTACTS
                    continue
                found = directory_rows(book, text)
            elif source == "apple":
                found, _, why = apple_rows(text, 0)
            else:
                found, _, why = google_rows(text, 0)
            if source != "directory" and why:
                errors[source] = why
                continue
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            errors[source] = str(error)
            continue
        rows += found
    if a.json:
        print(json.dumps({"rows": rows, "errors": errors}))
    else:
        _print_rows(rows)
        for source, why in errors.items():
            print("%s: not searched: %s" % (source, why.replace("\n", " ")))
        if not rows and not errors:
            print("no match in %s" % ", ".join(wanted))
    return 3 if errors else 0


def cmd_list_source(a):
    limit = 50 if a.limit is None else a.limit
    if limit < 0:
        print("--limit cannot be negative")
        return 1
    text = clean_name(a.query or "") or None
    if a.source == "apple":
        rows, total, why = apple_rows(text, limit)
    else:
        rows, total, why = google_rows(text, limit)
    if why:
        if a.json:
            print(json.dumps({"source": a.source, "total": None, "rows": [], "error": why}))
        else:
            print("%s: not read: %s" % (a.source, why.replace("\n", " ")))
        return 3
    if a.json:
        print(json.dumps({"source": a.source, "total": total, "rows": rows}))
    else:
        _print_rows(rows)
        print("showing %d of %d in %s" % (len(rows), total, a.source))
    return 0


# ---------------------------------------------------------------- routing

def file_under_project(entry, a, route_fn):
    """Route every handle as one person. Returns (ok, text).

    A handle already filed under another client stays there: a wrong route files one client's
    words in another client's folder, and that move needs the owner to say so.
    """
    routes = C.load_strict(C.ROUTES, {})
    if routes is None:
        return False, "routes.json is unreadable; nothing filed"
    h = entry["handles"]
    targets = list(dict.fromkeys(C.norm(h[f]) for f in ROUTE_FIELDS if h.get(f)))
    if not targets:
        return False, "no handle that comms files by (phone, email, Slack DM, Upwork room)"
    for target in targets:
        current = C.route(target, routes)
        if current and current.get("project") != a.project:
            return False, ("%s is already filed under %s. Left there. To move it, run comms route."
                           % (target, current.get("project")))
    path = a.path
    if not path and not any(isinstance(r, dict) and r.get("project") == a.project and r.get("path")
                            for r in routes.values()):
        default = os.path.join(PROJECT_FOLDERS, a.project)
        if not os.path.isdir(default):
            return False, "no folder for project %s yet. Pass --path." % a.project
        path = default
    args = argparse.Namespace(target=targets[0], project=a.project, path=path,
                              label=entry["name"], alias=targets[1:])
    return (not route_fn(args)), "filed under %s" % a.project


# ---------------------------------------------------------------- commands

def _load():
    return C.load_strict(CONTACTS, {})


def _store(key, entry):
    with C.filelock(CONTACTS):
        book = _load()
        if book is None:
            return False
        book[key] = entry
        C._save(CONTACTS, book)
    return True


def entry_account_conflict(entry, account):
    """True when the Google contact is held by another account. No saved account means the main login."""
    return bool(entry and entry.get("google_id") and (entry.get("google_account") or None) != account)


def _account_name(entry):
    return entry.get("google_account") or "the main Google login"


def apple_sync(entry, update_card):
    """Send the held changes to the linked card. Returns (ok, text)."""
    ops = entry.get("apple_pending") or []
    if not ops:
        return True, "a card is already linked. Nothing to change on it."
    if not entry.get("apple_created") and not update_card:
        return True, ("that card was there before this tool, so Card not changed. To change it, "
                      "run the same command with --update-card.")
    ok, text = apple_update(entry, ops)
    if ok:
        entry.pop("apple_pending", None)
    return ok, text


def google_sync(entry):
    ops = entry.get("google_pending") or []
    if not ops:
        return True, "already linked. Nothing to change on it."
    ok, text = google_update(entry, ops)
    if ok:
        entry.pop("google_pending", None)
    return ok, text


def cmd_add(a, route_fn):
    name = clean_name(a.name)
    key = name_key(name)
    if not key:
        print("a contact needs a name; nothing saved")
        return 1
    try:
        handles = parse_handles(a)
        add_slack(a, name, handles)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("nothing saved: %s" % error)
        return 1
    if not handles:
        print("nothing saved: give at least one of --phone --email --whatsapp --imessage --slack "
              "--upwork-room --linkedin")
        return 1

    C.secure_dir(C.ROOT)
    with C.filelock(CONTACTS):
        book = _load()
        if book is None:
            print("%s is unreadable; nothing saved" % CONTACTS)
            return 1
        if a.google_account and entry_account_conflict(book.get(key), a.google_account):
            print("nothing saved: %s is linked to %s, not %s"
                  % (book[key]["name"], _account_name(book[key]), a.google_account))
            return 1
        owner = owner_of_handle(book, key, handles)
        if owner:
            print("nothing saved: %s already belongs to %s" % (owner[1], owner[0]))
            return 1
        entry = book.get(key)
        prior = dict((entry or {}).get("handles") or {})
        changed = changed_fields(entry, handles)
        if changed and not a.replace:
            print("nothing saved: %s already has a different %s. Pass --replace to change it."
                  % (entry["name"], ", ".join(changed)))
            return 1
        entry = dict(entry or {"name": name, "handles": {},
                               "added": time.strftime("%Y-%m-%d")})
        entry["handles"] = {**entry["handles"], **handles}
        # What the linked books must change, kept on the row until it is done: the same command
        # run again after a failure finishes it, though the directory already holds the new value.
        if entry.get("apple_id"):
            ops = merge_ops(entry.get("apple_pending"), plan_ops(prior, entry["handles"], APPLE_FIELDS))
            if ops:
                entry["apple_pending"] = ops
        if entry.get("google_id"):
            ops = merge_ops(entry.get("google_pending"),
                            plan_ops(prior, entry["handles"], GOOGLE_HANDLE_FIELDS))
            if ops:
                entry["google_pending"] = ops
        if a.project:
            entry["project"] = a.project
        if a.dry_run:
            print("DRY RUN, nothing saved: %s" % name)
            for channel, value in sorted(channels_of(entry).items()):
                print("  %-10s %s" % (channel, value))
            if not a.no_apple and apple_handle_flags(entry["handles"]):
                try:
                    container, why = apple_target()
                except (OSError, subprocess.SubprocessError) as error:
                    container, why = None, str(error)
                print("  %-10s %s" % ("apple", "would save to iCloud container %s" % container
                                      if container else "would fail: " + why))
            return 0
        book[key] = entry
        C._save(CONTACTS, book)

    failed = False
    print("saved %s" % name)
    print("  %-10s %s" % ("directory", CONTACTS))
    if not a.no_apple and apple_handle_flags(entry["handles"]):
        if entry.get("apple_id"):
            ok, text = apple_sync(entry, a.update_card)
        else:
            ok, text = apple_save(entry)
        failed = failed or not ok
        print("  %-10s %s" % ("apple", text))
    if (a.google or a.google_account) and not entry.get("google_id"):
        ok, text = google_save(entry, a.google_account)
        failed = failed or not ok
        print("  %-10s %s" % ("google", text))
    elif entry.get("google_id"):
        ok, text = google_sync(entry)
        failed = failed or not ok
        print("  %-10s %s" % ("google", text))
    if a.project:
        ok, text = file_under_project(entry, a, route_fn)
        failed = failed or not ok
        print("  %-10s %s" % ("project", text))
    if not _store(key, entry):
        print("%s went unreadable; the card ids were not stored" % CONTACTS)
        failed = True

    resolved = channels_of(entry)
    for channel, value in sorted(resolved.items()):
        print("  %-10s %s" % (channel, value))
    if a.linkedin:
        print("  %-10s saved as a record. No connection request went out." % "note")
    if entry["handles"].get("slack_user") and not resolved.get("slack"):
        print("  %-10s no DM with this person yet, so Slack sends cannot reach them." % "note")
    return 3 if failed else 0


def cmd_rename(a):
    old_name, new_name = clean_name(a.old), clean_name(a.new)
    old_key, new_key = name_key(old_name), name_key(new_name)
    if not new_key:
        print("a contact needs a name; nothing changed")
        return 1
    with C.filelock(CONTACTS):
        book = _load()
        if book is None:
            print("%s is unreadable; nothing changed" % CONTACTS)
            return 1
        entry = book.get(old_key)
        if entry is None:
            # A rename that moved the row but not every book is finished by running it again.
            moved = book.get(new_key)
            if not (moved and (moved.get("name_sync") or {}).get("from", "").casefold() == old_key):
                print("no saved contact is named exactly %s" % old_name)
                return 1
            entry = moved
        elif new_key != old_key and new_key in book:
            print("nothing changed: %s already exists. Pick another name, or remove that one first."
                  % book[new_key]["name"])
            return 1
        else:
            book.pop(old_key)
            entry = dict(entry, name=new_name, name_sync={"from": old_key})
            book[new_key] = entry
            C._save(CONTACTS, book)
    print("renamed %s to %s" % (old_name, new_name))
    print("  %-10s %s" % ("directory", CONTACTS))

    failed = False
    sources = []
    if entry.get("apple_id"):
        if entry.get("apple_created") or a.update_card:
            ok, text = apple_update(entry, [], rename_from=old_name, rename_to=new_name)
        else:
            ok, text = True, ("that card was there before this tool, so Card not changed. To "
                              "rename it, run the same command with --update-card.")
            sources.append("apple")
        failed = failed or not ok
        print("  %-10s %s" % ("apple", text))
    if entry.get("google_id"):
        ok, text = google_update(entry, [], rename_to=new_name)
        failed = failed or not ok
        print("  %-10s %s" % ("google", text))
    if not failed:
        entry.pop("name_sync", None)
    if not _store(new_key, entry):
        print("%s went unreadable; the rename was not finished" % CONTACTS)
        return 3
    return 3 if failed else 0


def cmd_get(a):
    book = _load()
    if book is None:
        print("%s is unreadable" % CONTACTS, file=sys.stderr)
        return 1
    key = name_key(a.name)
    entry = book.get(key)
    if not entry:
        close = [e.get("name") for k, e in book.items() if key and (key in k or k in key)]
        print("no saved contact is named exactly %s." % clean_name(a.name), file=sys.stderr)
        if close:
            print("close: %s" % ", ".join(close[:5]), file=sys.stderr)
        return 1
    h = entry.get("handles") or {}
    value = channels_of(entry).get(a.channel)
    if not value:
        if a.channel == "slack" and h.get("slack_user"):
            print("%s has no DM with the owner yet. Open one: slack_bridge.py open-dm %s --workspace %s "
                  "--wait 1200. Then run: comms contact add \"%s\" --slack %s:%s"
                  % (entry["name"], h["slack_user"], h.get("slack_workspace"), entry["name"],
                     h.get("slack_workspace"), h["slack_user"]),
                  file=sys.stderr)
        else:
            print("%s has no %s handle" % (entry["name"], a.channel), file=sys.stderr)
        return 2
    workspace = h.get("slack_workspace") if a.channel == "slack" else None
    print("%s -> %s%s" % (entry["name"], value, " (workspace %s)" % workspace if workspace else ""),
          file=sys.stderr)
    if a.json:
        print(json.dumps({"name": entry["name"], "channel": a.channel, "handle": value,
                          "workspace": workspace}))
    else:
        print(value)
    return 0


def cmd_list(a):
    if a.source:
        return cmd_list_source(a)
    book = _load()
    if book is None:
        print("%s is unreadable" % CONTACTS, file=sys.stderr)
        return 1
    query = name_key(a.query or "")
    rows = [{"name": e.get("name"), "project": e.get("project"), "channels": channels_of(e),
             "apple_id": e.get("apple_id"), "google_id": e.get("google_id")}
            for k, e in sorted(book.items()) if query in k]
    if a.json:
        print(json.dumps(rows))
        return 0
    for row in rows:
        handles = "  ".join("%s=%s" % pair for pair in sorted(row["channels"].items()))
        print("%-24s %s" % (row["name"], handles))
    return 0


def cmd_rm(a):
    book = _load()
    if book is None:
        print("%s is unreadable; nothing removed" % CONTACTS)
        return 1
    key = name_key(a.name)
    entry = book.get(key)
    if not entry:
        print("no saved contact is named exactly %s" % clean_name(a.name))
        return 1
    if a.google and a.google_account and entry.get("google_id") and entry_account_conflict(entry, a.google_account):
        print("not deleted: this Google contact belongs to %s, not %s"
              % (_account_name(entry), a.google_account))
        return 3
    # The directory entry goes last: it holds the card ids, the only way back to the cards. Each
    # id leaves as its card goes, so a re-run after a partial failure skips what is already gone.
    for wanted, remove in ((a.apple, apple_delete), (a.google, google_delete)):
        if wanted:
            ok, text = remove(entry)
            print(text)
            if not ok:
                _store(key, entry)
                print("%s stays in the directory" % entry["name"])
                return 3
    with C.filelock(CONTACTS):
        book = _load()
        if book is None:
            print("%s is unreadable; nothing removed" % CONTACTS)
            return 1
        book.pop(key, None)
        C._save(CONTACTS, book)
    print("removed %s from the directory. Routes and message history stay." % entry["name"])
    return 0


def _whatsapp_name_rows():
    """The names WhatsApp holds, from the read-only command in the config. Returns (rows, why):
    rows is None when they cannot be read, so a failure can never read as a yes or a no."""
    command = K.get("whatsapp.contact_names_command")
    if not command:
        return None, "set whatsapp.contact_names_command to a read-only command that prints them"
    try:
        limit = int(os.environ.get("COMMS_WA_CHECK_TIMEOUT") or WA_CHECK_TIMEOUT_S)
        done = subprocess.run(shlex.split(str(command)), capture_output=True, text=True, timeout=limit)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return None, "the command did not finish (%s)" % e.__class__.__name__
    if done.returncode != 0:
        return None, "the command exited %d" % done.returncode
    try:
        rows = json.loads(done.stdout or "[]")
    except ValueError:
        return None, "the command did not print JSON"
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return None, "the command did not print a list of rows"
    return rows, ""


def cmd_wa_check(a):
    """Did this contact's name reach WhatsApp? Read only. Only a name from the phone's address book
    counts: a name people chose for themselves says nothing about the card."""
    book = _load()
    if book is None:
        print("%s is unreadable" % CONTACTS, file=sys.stderr)
        return 1
    key = name_key(a.name)
    entry = book.get(key)
    if not entry:
        print("no saved contact is named exactly %s." % clean_name(a.name), file=sys.stderr)
        return 1
    number = channels_of(entry).get("whatsapp")
    if not number:
        print("%s has no whatsapp handle" % entry["name"], file=sys.stderr)
        return 2
    rows, why = _whatsapp_name_rows()
    if rows is None:
        print("could not read the names WhatsApp holds: %s" % why)
        return 4
    jid = number + "@s.whatsapp.net"
    names = [str(r.get("name") or "").strip() for r in rows
             if r.get("jid") == jid and r.get("source") == "synced"]
    names = [n for n in names if n]
    if not names:
        print("not yet: WhatsApp holds no address book name for +%s. The phone can be slow, so try "
              "again later, or look in the WhatsApp app." % number)
        return 3
    # A second login can leave a stale row, so one matching row is enough.
    matching = [n for n in names if name_key(n) == key]
    if not matching:
        print('different: WhatsApp holds "%s" for +%s, and the directory holds "%s".'
              % ('", "'.join(sorted(set(names))), number, entry["name"]))
        return 3
    print('yes: WhatsApp holds "%s" for +%s.' % (matching[0], number))
    return 0


def run(a, route_fn):
    account = getattr(a, "google_account", None)
    if account is not None:
        a.google_account = account.strip()
        if not _account_ok(a.google_account):
            print("nothing done: %r is not a Google account email" % account)
            return 1
    if a.contact_cmd == "add":
        return cmd_add(a, route_fn)
    return {"get": cmd_get, "list": cmd_list, "rm": cmd_rm, "rename": cmd_rename,
            "find": cmd_find, "wa-check": cmd_wa_check}[a.contact_cmd](a)


def _account_ok(text):
    """The Google account for gws-as. It names a folder, so a path in it is refused."""
    return bool(re.fullmatch(r"[^@\s/\\]+@[^@\s/\\]+\.[^@\s/\\]+", text)) and ".." not in text


def add_parser(sub, route_fn):
    """Register `comms contact` on the comms CLI."""
    ct = sub.add_parser("contact", help="save a person's handles; look them up by exact name")
    cts = ct.add_subparsers(dest="contact_cmd", required=True)
    cta = cts.add_parser("add", help="save to the directory and to Apple Contacts (iCloud, then the "
                                     "iPhone, then WhatsApp)")
    cta.add_argument("name")
    cta.add_argument("--phone", help="with country code, or a 10-digit US number")
    cta.add_argument("--email")
    cta.add_argument("--whatsapp", help="only if it differs from --phone")
    cta.add_argument("--imessage", help="only if it differs from --phone and --email")
    cta.add_argument("--slack", metavar="WORKSPACE[:USER_ID]",
                     help="a workspace alone finds the exact name on the bridge")
    cta.add_argument("--slack-dm", metavar="DM_ID")
    cta.add_argument("--upwork-room", metavar="ROOM_ID")
    cta.add_argument("--linkedin", metavar="URL", help="a record only; it sends no connection request")
    cta.add_argument("--project", help="also file every handle under this project")
    cta.add_argument("--path", help="absolute folder for --project, if it has none yet")
    cta.add_argument("--google", action="store_true", help="also save a Google contact")
    cta.add_argument("--google-account", metavar="EMAIL",
                     help="use this Google account (through gws-as); implies --google")
    cta.add_argument("--update-card", action="store_true",
                     help="also change an Apple card that was there before this tool")
    cta.add_argument("--no-apple", action="store_true", help="skip the Apple card")
    cta.add_argument("--replace", action="store_true", help="change a handle already on record")
    cta.add_argument("--dry-run", action="store_true", help="show the result; write nothing")
    ctg = cts.add_parser("get", help="one channel's handle for an exact name; never guesses")
    ctg.add_argument("name")
    ctg.add_argument("--channel", required=True,
                     choices=["whatsapp", "imessage", "gmail", "slack", "upwork", "linkedin"])
    ctg.add_argument("--json", action="store_true")
    ctw = cts.add_parser("wa-check", help="did this contact's address book name reach WhatsApp? read only")
    ctw.add_argument("name")
    ctl = cts.add_parser("list", help="saved contacts, optionally by part of a name")
    ctl.add_argument("query", nargs="?")
    ctl.add_argument("--json", action="store_true")
    ctl.add_argument("--source", choices=["apple", "google"],
                     help="read the real address book instead of the directory")
    ctl.add_argument("--limit", type=int, help="rows to show with --source (default 50)")
    ctr = cts.add_parser("rm", help="remove from the directory; routes and history stay")
    ctr.add_argument("name")
    ctr.add_argument("--apple", action="store_true", help="also delete the card, only if we made it")
    ctr.add_argument("--google", action="store_true", help="also delete the Google contact")
    ctr.add_argument("--google-account", metavar="EMAIL",
                     help="the Google account that holds it (the saved one is used by default)")
    ctn = cts.add_parser("rename", help="rename the directory row and the linked card and Google contact")
    ctn.add_argument("old")
    ctn.add_argument("new")
    ctn.add_argument("--update-card", action="store_true",
                     help="also rename an Apple card that was there before this tool")
    ctf = cts.add_parser("find", help="search the directory, Apple Contacts and Google by name, "
                                      "number or email; prints to the terminal only")
    ctf.add_argument("text")
    ctf.add_argument("--source", choices=["directory", "apple", "google", "all"], default="all")
    ctf.add_argument("--json", action="store_true")
    ct.set_defaults(fn=lambda a: run(a, route_fn))
