#!/usr/bin/env python3
"""Tests for `comms contact` -- the one saved-contact directory.

Each test names a way this can hurt the owner: a text to the wrong country, a guess between two people,
one client's words filed under another, a card that never reaches the phone, a macOS dialog.

Runs the real CLI against a throwaway HOME, with fake Apple, Google and Slack tools first on the
path. The real `gws` is never run here: a fake HOME could harm its login. Stdlib only. Run:

    python3 test_contact.py
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # kit: code path
COMMS = os.path.join(CODE, "bin", "comms")
LIB = os.path.join(CODE, "commslib.py")
BOOK = os.path.join(CODE, "contactbook.py")

FAKE_TOOL = r"""#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_DIR"]
name = os.path.basename(sys.argv[0])
with open(os.path.join(d, "calls.log"), "a") as fh:
    fh.write(json.dumps([name] + sys.argv[1:]) + "\n")
with open(os.path.join(d, name + ".json")) as fh:
    plan = json.load(fh)
for key, reply in plan.items():
    if key in " ".join(sys.argv[1:]):
        if "seq" in reply:              # successive calls get successive replies; the last repeats
            counter = os.path.join(d, name + "." + "".join(c for c in key if c.isalnum()) + ".n")
            n = int(open(counter).read()) if os.path.exists(counter) else 0
            open(counter, "w").write(str(n + 1))
            reply = reply["seq"][min(n, len(reply["seq"]) - 1)]
        if "while_file" in reply:       # the answer changes once this file is gone
            reply = reply["before"] if os.path.exists(reply["while_file"]) else reply["after"]
        sys.stdout.write(reply.get("out", ""))
        sys.stderr.write(reply.get("err", ""))
        sys.exit(reply.get("rc", 0))
sys.exit(99)
"""

APPLE_OK = {
    "update": {"out": json.dumps({"updated": "CARD-1", "changes": ["phone"]})},
    "search": {"out": json.dumps({"total": 0, "rows": []})},
    "list": {"out": json.dumps({"total": 0, "rows": []})},
    "status": {"out": json.dumps({"auth": "authorized", "icloud_container": "ICLOUD-1",
                                  "default_container_name": "iCloud"})},
    "find": {"out": json.dumps({"by_handle": [], "by_name": []})},
    "add": {"out": json.dumps({"id": "CARD-1"})},
    "delete": {"out": json.dumps({"deleted": "CARD-1"})},
}

PERSON = {"resourceName": "people/c123", "etag": "E1",
          "names": [{"givenName": "Morgan", "familyName": "Lee", "displayName": "Morgan Lee"}],
          "phoneNumbers": [{"value": "+12065550100", "type": "mobile", "canonicalForm": "+12065550100"},
                           {"value": "+12065550199", "type": "work"}],
          "emailAddresses": [{"value": "m@example.invalid"}],
          "birthdays": [{"date": {"month": 5, "day": 6}}],
          "biographies": [{"value": "met at the fair"}]}

GWS_OK = {
    "people get": {"out": json.dumps(PERSON)},
    "updateContact": {"out": json.dumps(PERSON)},
    "connections list": {"out": json.dumps({"connections": [], "totalItems": 0})},
    "auth status": {"out": json.dumps({"scopes": ["email", "openid"]})},
    "createContact": {"out": json.dumps({"resourceName": "people/c123"})},
    "deleteContact": {"out": "{}"},
}

GWS_NO_SCOPE = {
    "auth status": {"out": json.dumps({"scopes": ["email", "openid",
                                                  "https://www.googleapis.com/auth/drive"]})},
    "createContact": {"rc": 1, "out": json.dumps({"error": {
        "code": 403, "message": "Request had insufficient authentication scopes."}})},
}

SLACK_ROWS = {
    "contacts": {"out": "acme | Morgan Lee | morgan | U111 | m@x.example.com\n"
                        "acme | Morgan Leeson | mk | U222 | k@x.example.com\n"},
    "channels": {"out": "acme | im | D111 | D111 | U111 | 0 | None\n"},
}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.root = os.path.join(self.home, ".claude", "communications")
        os.makedirs(self.root)
        self.config = os.path.join(self.home, "kit-config.json")
        with open(self.config, "w") as fh:
            json.dump({"data_dir": self.root}, fh)
        os.symlink(LIB, os.path.join(self.root, "commslib.py"))
        os.symlink(BOOK, os.path.join(self.root, "contactbook.py"))
        self.fake = os.path.join(self.home, "fake")
        os.makedirs(self.fake)
        self.book = os.path.join(self.root, "contacts.json")
        self.routes_path = os.path.join(self.root, "routes.json")
        for name in ("apple-contacts", "gws", "slack-bridge", "gws-as"):
            path = os.path.join(self.fake, name)
            with open(path, "w") as fh:
                fh.write(FAKE_TOOL)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.plan("apple-contacts", APPLE_OK)
        self.plan("gws", GWS_OK)
        self.plan("slack-bridge", SLACK_ROWS)
        self.plan("gws-as", GWS_OK)
        os.makedirs(os.path.join(self.home, ".local", "bin"))
        os.symlink(os.path.join(self.fake, "gws-as"), os.path.join(self.home, ".local", "bin", "gws-as"))
        with open(self.routes_path, "w") as fh:
            json.dump({}, fh)
        with open(os.path.join(self.root, "threads.json"), "w") as fh:
            json.dump({}, fh)

    def tearDown(self):
        self._tmp.cleanup()

    def plan(self, tool, plan):
        with open(os.path.join(self.fake, tool + ".json"), "w") as fh:
            json.dump(plan, fh)

    def comms(self, *args):
        env = dict(os.environ, HOME=self.home, FAKE_DIR=self.fake, COMMS_KIT_CONFIG=self.config,
                   PATH=self.fake + os.pathsep + os.environ["PATH"],
                   COMMS_APPLE_CONTACTS_BIN=os.path.join(self.fake, "apple-contacts"),
                   COMMS_SLACK_BRIDGE=os.path.join(self.fake, "slack-bridge"))
        return subprocess.run(["python3", COMMS, "contact", *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def add(self, *args):
        return self.comms("add", *args)

    def get(self, name, channel):
        return self.comms("get", name, "--channel", channel)

    def calls(self, needle=""):
        path = os.path.join(self.fake, "calls.log")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            rows = [json.loads(line) for line in fh]
        return [r for r in rows if needle in " ".join(r)]

    def entries(self):
        if not os.path.exists(self.book):
            return {}
        with open(self.book) as fh:
            return json.load(fh)


class Numbers(Base):
    def test_a_us_number_reaches_whatsapp_with_its_country_code(self):
        # C.norm drops the US 1. WhatsApp reads 2065550100 as Egypt (+20) and texts a stranger.
        self.assertEqual(self.add("Ann Lee", "--phone", "(206) 555-0100", "--no-apple").returncode, 0)
        self.assertEqual(self.get("Ann Lee", "whatsapp").stdout.strip(), "12065550100")
        self.assertEqual(self.get("Ann Lee", "imessage").stdout.strip(), "+12065550100")

    def test_ten_digits_and_plus_one_are_the_same_person(self):
        self.add("Ann Lee", "--phone", "2065550100", "--no-apple")
        out = self.add("Ann Twin", "--phone", "+1 206 555 0100", "--no-apple")
        self.assertEqual(out.returncode, 1)
        self.assertIn("Ann Lee", out.stdout)
        self.assertEqual(list(self.entries()), ["ann lee"])

    def test_uk_and_us_lookalikes_stay_two_people(self):
        self.assertEqual(self.add("Uk Person", "--phone", "+44" + " 206 555 0142", "--no-apple").returncode, 0)
        self.assertEqual(self.add("Us Person", "--phone", "(206) 555-0142", "--no-apple").returncode, 0)
        self.assertEqual(self.get("Uk Person", "whatsapp").stdout.strip(), "442065550142")
        self.assertEqual(self.get("Us Person", "whatsapp").stdout.strip(), "12065550142")

    def test_a_number_with_no_country_is_refused_not_guessed(self):
        for raw in ("794609", "07911 123456", "abc", "+123"):
            out = self.add("Who Knows", "--phone", raw, "--no-apple")
            self.assertEqual(out.returncode, 1, raw)
            self.assertEqual(self.entries(), {}, raw)


PN = "@s.whatsapp.net"


class WhatsAppNameCheck(Base):
    """`comms contact wa-check`: did the name reach WhatsApp? Read only, and it never guesses yes."""

    def rows_command(self, rows=None, rc=0, raw=None, hang=False):
        """Point the config at a script that prints these rows, as the bridge's own table would."""
        script = os.path.join(self.fake, "rows.py")
        with open(script, "w") as fh:
            fh.write("import json, sys, time\n")
            if hang:
                fh.write("time.sleep(30)\n")
            fh.write("print(%r)\n" % (raw if raw is not None else json.dumps(rows)))
            fh.write("sys.exit(%d)\n" % rc)
        with open(self.config) as fh:
            conf = json.load(fh)
        conf["whatsapp"] = {"contact_names_command": "%s %s" % (sys.executable, script)}
        with open(self.config, "w") as fh:
            json.dump(conf, fh)

    def check(self, name="Morgan Lee"):
        self.add("Morgan Lee", "--phone", "+12065550100", "--no-apple")
        return self.comms("wa-check", name)

    def test_the_saved_name_from_the_address_book_is_a_yes(self):
        self.rows_command([{"jid": "12065550100" + PN, "name": "Morgan Lee", "source": "synced"}])
        out = self.check()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Morgan Lee", out.stdout)

    def test_case_and_spacing_of_the_name_do_not_matter(self):
        self.rows_command([{"jid": "12065550100" + PN, "name": " morgan  LEE", "source": "synced"}])
        self.assertEqual(self.check().returncode, 0)

    def test_a_name_they_chose_for_themselves_is_never_a_yes(self):
        self.rows_command([{"jid": "12065550100" + PN, "name": "Morgan Lee", "source": "message_buffer"}])
        out = self.check()
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        self.assertIn("not yet", out.stdout)

    def test_a_different_saved_name_is_not_a_yes_and_is_shown(self):
        self.rows_command([{"jid": "12065550100" + PN, "name": "Old Name", "source": "synced"}])
        out = self.check()
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        self.assertIn("Old Name", out.stdout)

    def test_any_matching_row_is_a_yes_when_a_second_login_left_a_stale_one(self):
        self.rows_command([{"jid": "12065550100" + PN, "name": "Old Name", "source": "synced"},
                           {"jid": "12065550100" + PN, "name": "Morgan Lee", "source": "synced"}])
        out = self.check()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Morgan Lee", out.stdout)

    def test_every_different_name_is_shown(self):
        self.rows_command([{"jid": "12065550100" + PN, "name": "Old Name", "source": "synced"},
                           {"jid": "12065550100" + PN, "name": "Older Name", "source": "synced"}])
        out = self.check()
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        self.assertIn("Old Name", out.stdout)
        self.assertIn("Older Name", out.stdout)

    def test_no_row_for_the_number_says_not_yet(self):
        self.rows_command([{"jid": "12065550199" + PN, "name": "Morgan Lee", "source": "synced"}])
        out = self.check()
        self.assertEqual(out.returncode, 3)
        self.assertIn("not yet", out.stdout)

    def test_a_lid_row_with_the_same_digits_is_another_person(self):
        self.rows_command([{"jid": "12065550100@lid", "name": "Morgan Lee", "source": "synced"}])
        self.assertEqual(self.check().returncode, 3)

    def test_a_broken_command_is_unknown_never_a_yes_or_a_no(self):
        for kwargs in (dict(rows=[], rc=1), dict(raw="not json"), dict(raw=json.dumps({"rows": []})),
                       dict(raw=json.dumps(["just text"]))):
            self.rows_command(**kwargs)
            out = self.check()
            self.assertEqual(out.returncode, 4, (kwargs, out.stdout, out.stderr))
            self.assertIn("could not read", out.stdout + out.stderr)

    def test_a_hung_command_stops_at_the_time_limit(self):
        self.rows_command(rows=[], hang=True)
        self.add("Morgan Lee", "--phone", "+12065550100", "--no-apple")
        env_limit = dict(os.environ, HOME=self.home, FAKE_DIR=self.fake, COMMS_KIT_CONFIG=self.config,
                         COMMS_WA_CHECK_TIMEOUT="1", PATH=self.fake + os.pathsep + os.environ["PATH"])
        out = subprocess.run(["python3", COMMS, "contact", "wa-check", "Morgan Lee"], env=env_limit,
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 4, out.stdout + out.stderr)

    def test_no_command_in_the_config_says_which_key_to_set(self):
        out = self.check()
        self.assertEqual(out.returncode, 4)
        self.assertIn("whatsapp.contact_names_command", out.stdout + out.stderr)

    def test_an_unknown_name_never_runs_the_command(self):
        self.rows_command([])
        script = os.path.join(self.fake, "rows.py")
        os.remove(script)                        # a run would fail with exit 4, not the exit 1 asked for
        out = self.check("Morgan")
        self.assertEqual(out.returncode, 1)

    def test_a_contact_with_no_whatsapp_number_is_exit_2(self):
        self.rows_command([])
        self.add("Alex Doe", "--email", "alex@example.com", "--no-apple")
        self.assertEqual(self.comms("wa-check", "Alex Doe").returncode, 2)


class Lookup(Base):
    def test_a_partial_name_never_returns_a_handle(self):
        self.add("Morgan Lee", "--phone", "+12065550100", "--no-apple")
        out = self.get("Morgan", "whatsapp")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.stdout.strip(), "")
        self.assertIn("Morgan Lee", out.stderr)      # candidates are shown, never picked

    def test_case_and_spacing_do_not_matter(self):
        self.add("Morgan Lee", "--phone", "+12065550100", "--no-apple")
        self.assertEqual(self.get(" morgan   LEE ", "whatsapp").stdout.strip(), "12065550100")

    def test_a_channel_with_no_handle_fails_and_prints_no_handle(self):
        self.add("Morgan Lee", "--phone", "+12065550100", "--no-apple")
        out = self.get("Morgan Lee", "gmail")
        self.assertEqual(out.returncode, 2)
        self.assertEqual(out.stdout.strip(), "")

    def test_an_email_only_contact_can_use_imessage_but_not_whatsapp(self):
        self.add("Eve Doe", "--email", "EVE@Example.invalid", "--no-apple")
        self.assertEqual(self.get("Eve Doe", "gmail").stdout.strip(), "eve@example.invalid")
        self.assertEqual(self.get("Eve Doe", "imessage").stdout.strip(), "eve@example.invalid")
        self.assertEqual(self.get("Eve Doe", "whatsapp").returncode, 2)

    def test_every_lookup_says_who_it_resolved(self):
        self.add("Morgan Lee", "--phone", "+12065550100", "--no-apple")
        self.assertIn("Morgan Lee -> 12065550100", self.get("Morgan Lee", "whatsapp").stderr)

    def test_list_json_gives_each_channel_handle(self):
        self.add("Morgan Lee", "--phone", "+12065550100", "--no-apple")
        rows = json.loads(self.comms("list", "lee", "--json").stdout)
        self.assertEqual(rows[0]["channels"]["whatsapp"], "12065550100")


class Conflicts(Base):
    def test_a_new_number_for_a_known_name_is_refused_without_replace(self):
        self.add("Morgan Lee", "--phone", "+12065550100", "--no-apple")
        out = self.add("Morgan Lee", "--phone", "+12065550101", "--no-apple")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(self.get("Morgan Lee", "whatsapp").stdout.strip(), "12065550100")
        self.assertEqual(self.add("Morgan Lee", "--phone", "+12065550101", "--no-apple",
                                  "--replace").returncode, 0)
        self.assertEqual(self.get("Morgan Lee", "whatsapp").stdout.strip(), "12065550101")

    def test_adding_a_second_channel_keeps_the_first(self):
        self.add("Morgan Lee", "--phone", "+12065550100", "--no-apple")
        self.assertEqual(self.add("Morgan Lee", "--email", "m@example.invalid", "--no-apple").returncode, 0)
        self.assertEqual(self.get("Morgan Lee", "whatsapp").stdout.strip(), "12065550100")
        self.assertEqual(self.get("Morgan Lee", "gmail").stdout.strip(), "m@example.invalid")

    def test_a_torn_directory_is_never_overwritten(self):
        with open(self.book, "w") as fh:
            fh.write("{torn")
        self.assertEqual(self.add("Morgan Lee", "--phone", "+12065550100", "--no-apple").returncode, 1)
        with open(self.book) as fh:
            self.assertEqual(fh.read(), "{torn")

    def test_dry_run_writes_nothing_anywhere(self):
        out = self.add("Morgan Lee", "--phone", "+12065550100", "--email", "m@example.invalid",
                       "--google", "--project", "acme", "--path", self.home, "--dry-run")
        self.assertEqual(out.returncode, 0)
        self.assertEqual(self.entries(), {})
        self.assertEqual(self.calls("add --"), [])
        self.assertEqual(self.calls("createContact"), [])
        with open(self.routes_path) as fh:
            self.assertEqual(json.load(fh), {})


class Filing(Base):
    def test_no_project_means_routes_stay_byte_identical(self):
        with open(self.routes_path, "rb") as fh:
            before = fh.read()
        self.add("Morgan Lee", "--phone", "+12065550100", "--no-apple")
        with open(self.routes_path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_project_routes_every_handle_as_one_person(self):
        out = self.add("Morgan Lee", "--phone", "+12065550100", "--email", "M@example.invalid",
                       "--no-apple", "--project", "acme", "--path", self.home)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        with open(self.routes_path) as fh:
            routes = json.load(fh)
        self.assertEqual(routes["2065550100"]["project"], "acme")
        self.assertEqual(routes["2065550100"]["label"], "Morgan Lee")
        self.assertIn("m@example.invalid", routes["2065550100"]["aliases"])

    def test_a_handle_filed_under_another_client_is_not_moved(self):
        with open(self.routes_path, "w") as fh:
            json.dump({"2065550100": {"project": "other_client", "path": None,
                                      "label": "x", "aliases": []}}, fh)
        out = self.add("Morgan Lee", "--phone", "+12065550100", "--no-apple",
                       "--project", "acme", "--path", self.home)
        self.assertEqual(out.returncode, 3)
        self.assertIn("other_client", out.stdout)
        with open(self.routes_path) as fh:
            self.assertEqual(json.load(fh)["2065550100"]["project"], "other_client")
        self.assertEqual(self.get("Morgan Lee", "whatsapp").returncode, 0)


class Apple(Base):
    def test_a_phone_saves_a_card_to_icloud(self):
        out = self.add("Morgan Van Lee", "--phone", "+12065550100")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        add = self.calls("add --")[0]
        self.assertEqual(add[add.index("--container") + 1], "ICLOUD-1")
        self.assertEqual(add[add.index("--given") + 1], "Morgan Van")
        self.assertEqual(add[add.index("--family") + 1], "Lee")
        self.assertEqual(self.entries()["morgan van lee"]["apple_id"], "CARD-1")
        self.assertTrue(self.entries()["morgan van lee"]["apple_created"])

    def test_no_contacts_permission_saves_nothing_to_apple_and_opens_no_dialog(self):
        plan = dict(APPLE_OK, status={"out": json.dumps({"auth": "notDetermined"})})
        self.plan("apple-contacts", plan)
        out = self.add("Morgan Lee", "--phone", "+12065550100")
        self.assertEqual(out.returncode, 3)
        self.assertEqual(self.calls("find"), [])
        self.assertEqual(self.calls("add --"), [])
        self.assertEqual(self.get("Morgan Lee", "whatsapp").returncode, 0)

    def test_a_mac_only_container_is_refused_because_the_phone_never_sees_it(self):
        plan = dict(APPLE_OK, status={"out": json.dumps({"auth": "authorized", "icloud_container": None})})
        self.plan("apple-contacts", plan)
        out = self.add("Morgan Lee", "--phone", "+12065550100")
        self.assertEqual(out.returncode, 3)
        self.assertIn("iCloud", out.stdout)
        self.assertEqual(self.calls("add --"), [])

    def test_a_card_with_that_number_is_not_duplicated(self):
        plan = dict(APPLE_OK, find={"out": json.dumps(
            {"by_handle": [{"id": "OLD-1", "name": "M Lee"}], "by_name": []})})
        self.plan("apple-contacts", plan)
        self.assertEqual(self.add("Morgan Lee", "--phone", "+12065550100").returncode, 0)
        self.assertEqual(self.calls("add --"), [])
        self.assertEqual(self.entries()["morgan lee"]["apple_id"], "OLD-1")
        self.assertFalse(self.entries()["morgan lee"]["apple_created"])

    def test_a_same_name_card_with_other_details_is_left_alone(self):
        plan = dict(APPLE_OK, find={"out": json.dumps(
            {"by_handle": [], "by_name": [{"id": "OTHER-1", "name": "Morgan Lee"}]})})
        self.plan("apple-contacts", plan)
        out = self.add("Morgan Lee", "--phone", "+12065550100")
        self.assertEqual(out.returncode, 3)
        self.assertEqual(self.calls("add --"), [])
        self.assertIn("same name", out.stdout)

    def test_slack_only_contact_makes_no_card(self):
        self.add("Morgan Lee", "--slack", "acme:U111", "--slack-dm", "D111")
        self.assertEqual(self.calls("apple-contacts"), [])

    def test_rm_leaves_the_card_unless_asked(self):
        self.add("Morgan Lee", "--phone", "+12065550100")
        self.assertEqual(self.comms("rm", "Morgan Lee").returncode, 0)
        self.assertEqual(self.calls("delete"), [])
        self.assertEqual(self.entries(), {})

    def test_rm_apple_deletes_only_the_card_we_made(self):
        self.add("Morgan Lee", "--phone", "+12065550100")
        self.assertEqual(self.comms("rm", "Morgan Lee", "--apple").returncode, 0)
        delete = self.calls("delete")[0]
        self.assertEqual(delete[delete.index("--id") + 1], "CARD-1")
        self.assertEqual(delete[delete.index("--expect-name") + 1], "Morgan Lee")

    def test_rm_apple_never_deletes_a_card_that_was_already_there(self):
        plan = dict(APPLE_OK, find={"out": json.dumps(
            {"by_handle": [{"id": "OLD-1", "name": "M Lee"}], "by_name": []})})
        self.plan("apple-contacts", plan)
        self.add("Morgan Lee", "--phone", "+12065550100")
        out = self.comms("rm", "Morgan Lee", "--apple")
        self.assertEqual(out.returncode, 3)
        self.assertEqual(self.calls("delete"), [])
        self.assertIn("morgan lee", self.entries())


class RunAgain(Base):
    def test_the_same_command_finishes_only_what_failed(self):
        self.plan("gws", GWS_NO_SCOPE)
        self.plan("apple-contacts", dict(APPLE_OK, status={"out": json.dumps({"auth": "denied"})}))
        args = ("Morgan Lee", "--phone", "+12065550100", "--email", "m@example.invalid", "--google")
        self.assertEqual(self.add(*args).returncode, 3)
        self.assertEqual(self.get("Morgan Lee", "whatsapp").stdout.strip(), "12065550100")

        self.plan("apple-contacts", APPLE_OK)
        self.plan("gws", GWS_OK)
        self.assertEqual(self.add(*args).returncode, 0)
        self.assertEqual(self.entries()["morgan lee"]["apple_id"], "CARD-1")
        self.assertEqual(self.entries()["morgan lee"]["google_id"], "people/c123")

        self.assertEqual(self.add(*args).returncode, 0)
        self.assertEqual(len(self.calls("add --")), 1)
        self.assertEqual(len(self.calls("createContact")), 2)   # the first try had no scope


class RemoveAgain(Base):
    def test_a_partial_rm_can_be_run_again(self):
        self.add("Morgan Lee", "--phone", "+12065550100", "--google")
        self.plan("gws", dict(GWS_OK, deleteContact={"rc": 1, "out": "boom"}))
        self.assertEqual(self.comms("rm", "Morgan Lee", "--apple", "--google").returncode, 3)
        self.assertEqual(len(self.calls("delete --id")), 1)
        self.assertNotIn("apple_id", self.entries()["morgan lee"])
        self.plan("gws", GWS_OK)
        self.assertEqual(self.comms("rm", "Morgan Lee", "--apple", "--google").returncode, 0)
        self.assertEqual(len(self.calls("delete --id")), 1)     # the card is not deleted twice
        self.assertEqual(self.entries(), {})

    def test_a_replaced_number_says_the_card_kept_the_old_one(self):
        # A card from before this tool is not ours to edit (see EditReachesBooks for ours).
        self.plan("apple-contacts", dict(APPLE_OK, find={"out": json.dumps(
            {"by_handle": [{"id": "OLD-1", "name": "M Lee"}], "by_name": []})}))
        self.add("Morgan Lee", "--phone", "+12065550100")
        out = self.add("Morgan Lee", "--phone", "+12065550101", "--replace")
        self.assertEqual(out.returncode, 0)
        self.assertIn("Card not changed", out.stdout)
        self.assertEqual(self.calls("add --"), [])


class Google(Base):
    def test_google_is_opt_in_so_the_iphone_gets_no_second_card(self):
        self.add("Morgan Lee", "--phone", "+12065550100")
        self.assertEqual(self.calls("gws"), [])

    def test_google_saves_when_asked(self):
        out = self.add("Morgan Lee", "--email", "m@example.invalid", "--no-apple", "--google")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        call = self.calls("createContact")[0]
        body = json.loads(call[call.index("--json") + 1])
        self.assertEqual(body["names"][0]["givenName"], "Morgan")
        self.assertEqual(body["emailAddresses"][0]["value"], "m@example.invalid")
        self.assertEqual(self.entries()["morgan lee"]["google_id"], "people/c123")

    def test_a_missing_scope_prints_a_login_line_that_keeps_every_scope(self):
        self.plan("gws", GWS_NO_SCOPE)
        out = self.add("Morgan Lee", "--email", "m@example.invalid", "--no-apple", "--google")
        self.assertEqual(out.returncode, 3)
        self.assertIn("gws auth login --scopes", out.stdout)
        for scope in ("email", "openid", "auth/drive", "auth/contacts"):
            self.assertIn(scope, out.stdout)
        self.assertEqual(self.get("Morgan Lee", "gmail").returncode, 0)


SCOPE_ERROR = {"rc": 1, "out": json.dumps({"error": {
    "code": 403, "message": "Request had insufficient authentication scopes."}})}
STATUS_HAS_CONTACTS = {"out": json.dumps({"scopes": ["email", "openid",
                                                     "https://www.googleapis.com/auth/contacts"]})}


class GoogleStaleToken(Base):
    """`gws auth login` leaves the old access token in token_cache.json, and gws keeps using it
    until it expires. Found live on 2026-10-31: the login worked and gws still said 403."""

    def setUp(self):
        super().setUp()
        self.cache = os.path.join(self.home, ".config", "gws", "token_cache.json")
        os.makedirs(os.path.dirname(self.cache))
        with open(self.cache, "w") as fh:
            fh.write("old token")

    def add_google(self):
        return self.add("Morgan Lee", "--email", "m@example.invalid", "--no-apple", "--google")

    def test_the_first_add_after_a_login_works_with_no_wait(self):
        self.plan("gws", {"auth status": STATUS_HAS_CONTACTS, "createContact": {
            "while_file": self.cache, "before": SCOPE_ERROR,
            "after": {"out": json.dumps({"resourceName": "people/c123"})}}})
        out = self.add_google()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.entries()["morgan lee"]["google_id"], "people/c123")
        self.assertFalse(os.path.exists(self.cache))

    def test_a_box_left_unticked_says_so_after_one_retry(self):
        # auth status lists what the login asked for, not what Google granted.
        self.plan("gws", {"auth status": STATUS_HAS_CONTACTS, "createContact": SCOPE_ERROR})
        out = self.add_google()
        self.assertEqual(out.returncode, 3)
        self.assertEqual(len(self.calls("createContact")), 2)
        self.assertIn("tick the Contacts box", out.stdout)
        self.assertIn("gws auth login --scopes", out.stdout)

    def test_no_cache_file_means_no_retry(self):
        os.remove(self.cache)
        self.plan("gws", {"auth status": STATUS_HAS_CONTACTS, "createContact": SCOPE_ERROR})
        self.assertEqual(self.add_google().returncode, 3)
        self.assertEqual(len(self.calls("createContact")), 1)

    def test_a_login_without_the_scope_keeps_its_cache(self):
        # Every other gws call still uses that cache. Emptying it would fix nothing here.
        self.plan("gws", GWS_NO_SCOPE)
        out = self.add_google()
        self.assertEqual(out.returncode, 3)
        self.assertTrue(os.path.exists(self.cache))
        self.assertEqual(len(self.calls("createContact")), 1)

    def test_another_403_is_not_called_a_scope_problem(self):
        self.plan("gws", {"auth status": STATUS_HAS_CONTACTS, "createContact": {"rc": 1, "out": json.dumps(
            {"error": {"code": 403, "message": "People API has not been used in project 1"}})}})
        out = self.add_google()
        self.assertEqual(out.returncode, 3)
        self.assertIn("has not been used", out.stdout)
        self.assertNotIn("gws auth login", out.stdout)
        self.assertTrue(os.path.exists(self.cache))

    def test_a_contact_named_after_the_error_is_not_an_error(self):
        self.plan("gws", {"auth status": STATUS_HAS_CONTACTS, "createContact": {"out": json.dumps(
            {"resourceName": "people/c9", "names": [{"displayName": "403 insufficient"}]})}})
        self.assertEqual(self.add("403 insufficient", "--email", "i@example.invalid", "--no-apple",
                                  "--google").returncode, 0)
        self.assertTrue(os.path.exists(self.cache))

    def test_delete_recovers_the_same_way(self):
        self.plan("gws", {"auth status": STATUS_HAS_CONTACTS,
                          "createContact": {"out": json.dumps({"resourceName": "people/c123"})},
                          "deleteContact": {"while_file": self.cache, "before": SCOPE_ERROR,
                                            "after": {"out": "{}"}}})
        self.add_google()
        out = self.comms("rm", "Morgan Lee", "--google")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.entries(), {})


class Slack(Base):
    def test_a_workspace_finds_the_exact_person_not_the_near_match(self):
        out = self.add("Morgan Lee", "--slack", "acme")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.get("Morgan Lee", "slack").stdout.strip(), "D111")
        self.assertIn("acme", self.get("Morgan Lee", "slack").stderr)

    def test_two_people_with_that_name_save_nothing(self):
        self.plan("slack-bridge", {"contacts": {"out": "acme | Morgan Lee | a | U1 | x\nacme | Morgan Lee | b | U2 | y\n"}})
        self.assertEqual(self.add("Morgan Lee", "--slack", "acme").returncode, 1)
        self.assertEqual(self.entries(), {})

    def test_no_dm_yet_is_a_clear_failure_at_lookup(self):
        self.plan("slack-bridge", {"contacts": SLACK_ROWS["contacts"], "channels": {"out": ""}})
        self.assertEqual(self.add("Morgan Lee", "--slack", "acme").returncode, 0)
        out = self.get("Morgan Lee", "slack")
        self.assertEqual(out.returncode, 2)
        self.assertEqual(out.stdout.strip(), "")
        self.assertIn("no DM", out.stderr)


class Records(Base):
    def test_upwork_room_is_stored_and_found(self):
        self.add("Morgan Lee", "--upwork-room", "room_abc123")
        self.assertEqual(self.get("Morgan Lee", "upwork").stdout.strip(), "room_abc123")

    def test_linkedin_is_a_record_and_says_no_request_went_out(self):
        out = self.add("Morgan Lee", "--linkedin", "https://www.linkedin.com/in/morgan-lee/")
        self.assertEqual(out.returncode, 0)
        self.assertIn("No connection request", out.stdout)
        self.assertEqual(self.get("Morgan Lee", "linkedin").stdout.strip(),
                         "https://www.linkedin.com/in/morgan-lee/")

    def test_nothing_to_save_is_refused(self):
        self.assertEqual(self.add("Morgan Lee").returncode, 1)
        self.assertEqual(self.entries(), {})


def body_of(call):
    return json.loads(call[call.index("--json") + 1])


def params_of(call):
    return json.loads(call[call.index("--params") + 1])


class EditReachesBooks(Base):
    """An edit must reach the real card and the real Google contact, and touch nothing else."""

    def made(self, *extra):
        out = self.add("Morgan Lee", "--phone", "+12065550100", "--email", "m@example.invalid",
                       "--google", *extra)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_a_replaced_number_updates_only_that_number_on_our_card(self):
        self.made()
        out = self.add("Morgan Lee", "--phone", "+12065550101", "--replace")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        update = self.calls("update --id")
        self.assertEqual(len(update), 1)
        call = update[0]
        self.assertEqual(call[call.index("--id") + 1], "CARD-1")
        self.assertEqual(call[call.index("--replace-phone") + 1], "+12065550100=+12065550101")
        # one pair, no add, no wipe: a second number, note, photo or birthday has no way to go
        self.assertNotIn("--add-phone", call)
        self.assertNotIn("--replace-email", call)
        self.assertEqual(len(self.calls("add --")), 1)      # still the one card from the first add
        self.assertIn("updated", out.stdout)
        self.assertEqual(self.entries()["morgan lee"]["handles"]["phone"], "+12065550101")

    def test_a_new_channel_number_is_added_to_the_card_not_swapped(self):
        self.made()
        self.assertEqual(self.add("Morgan Lee", "--whatsapp", "+12065550177").returncode, 0)
        call = self.calls("update --id")[0]
        self.assertEqual(call[call.index("--add-phone") + 1], "+12065550177")
        self.assertNotIn("--replace-phone", call)

    def test_the_card_check_accepts_the_name_or_a_number_it_holds(self):
        self.made()
        self.add("Morgan Lee", "--phone", "+12065550101", "--replace")
        call = self.calls("update --id")[0]
        self.assertEqual(call[call.index("--expect-name") + 1], "Morgan Lee")
        self.assertIn("+12065550100", [call[i + 1] for i, x in enumerate(call) if x == "--expect-handle"])

    def test_a_card_from_before_the_tool_stays_unchanged_and_says_so(self):
        self.plan("apple-contacts", dict(APPLE_OK, find={"out": json.dumps(
            {"by_handle": [{"id": "OLD-1", "name": "M Lee"}], "by_name": []})}))
        self.add("Morgan Lee", "--phone", "+12065550100")
        out = self.add("Morgan Lee", "--phone", "+12065550101", "--replace")
        self.assertEqual(out.returncode, 0)
        self.assertEqual(self.calls("update --id"), [])
        self.assertIn("--update-card", out.stdout)
        self.assertIn("Card not changed", out.stdout)

    def test_update_card_changes_a_card_from_before_the_tool(self):
        self.plan("apple-contacts", dict(APPLE_OK, find={"out": json.dumps(
            {"by_handle": [{"id": "OLD-1", "name": "M Lee"}], "by_name": []})}))
        self.add("Morgan Lee", "--phone", "+12065550100")
        out = self.add("Morgan Lee", "--phone", "+12065550101", "--replace", "--update-card")
        self.assertEqual(out.returncode, 0, out.stdout)
        call = self.calls("update --id")[0]
        self.assertEqual(call[call.index("--id") + 1], "OLD-1")
        self.assertNotIn("Card not changed", out.stdout)

    def test_a_held_change_goes_out_when_update_card_comes_later(self):
        self.plan("apple-contacts", dict(APPLE_OK, find={"out": json.dumps(
            {"by_handle": [{"id": "OLD-1", "name": "M Lee"}], "by_name": []})}))
        self.add("Morgan Lee", "--phone", "+12065550100")
        self.add("Morgan Lee", "--phone", "+12065550101", "--replace")
        self.assertEqual(self.add("Morgan Lee", "--phone", "+12065550101", "--update-card").returncode, 0)
        self.assertEqual(len(self.calls("update --id")), 1)
        self.assertNotIn("apple_pending", self.entries()["morgan lee"])

    def test_a_failed_card_update_finishes_on_the_same_command(self):
        self.made()
        self.plan("apple-contacts", dict(APPLE_OK, update={"rc": 6, "out": json.dumps({"error": "store busy"})}))
        args = ("Morgan Lee", "--phone", "+12065550101", "--replace")
        out = self.add(*args)
        self.assertEqual(out.returncode, 3)
        self.assertIn("store busy", out.stdout)
        self.plan("apple-contacts", APPLE_OK)
        self.assertEqual(self.add(*args).returncode, 0)
        last = self.calls("update --id")[-1]
        self.assertEqual(last[last.index("--replace-phone") + 1], "+12065550100=+12065550101")
        self.assertNotIn("apple_pending", self.entries()["morgan lee"])

    def test_google_edit_keeps_everything_but_the_changed_number(self):
        self.made()
        out = self.add("Morgan Lee", "--phone", "+12065550101", "--replace", "--no-apple")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        call = self.calls("updateContact")[0]
        self.assertEqual(params_of(call), {"resourceName": "people/c123", "updatePersonFields": "phoneNumbers"})
        body = body_of(call)
        self.assertEqual(body["etag"], "E1")
        self.assertEqual([p["value"] for p in body["phoneNumbers"]], ["+12065550101", "+12065550199"])
        self.assertEqual(body["phoneNumbers"][1], PERSON["phoneNumbers"][1])     # the second number, whole
        for kept in ("birthdays", "biographies", "names", "emailAddresses"):
            self.assertNotIn(kept, body)        # not sent, so Google leaves them alone
        self.assertIn("google     updated", out.stdout)

    def test_google_email_edit_names_only_the_email_field(self):
        self.made()
        self.add("Morgan Lee", "--email", "new@example.invalid", "--replace", "--no-apple")
        call = self.calls("updateContact")[0]
        self.assertEqual(params_of(call)["updatePersonFields"], "emailAddresses")
        self.assertEqual(body_of(call)["emailAddresses"][0]["value"], "new@example.invalid")

    def test_a_stale_etag_is_read_again_and_retried_once(self):
        self.made()
        stale = {"rc": 1, "out": json.dumps({"error": {"code": 400, "status": "FAILED_PRECONDITION",
                 "message": "Request person.etag is different than the current person.etag."}})}
        fresh = dict(PERSON, etag="E2")
        self.plan("gws", dict(GWS_OK, **{
            "people get": {"seq": [{"out": json.dumps(PERSON)}, {"out": json.dumps(fresh)}]},
            "updateContact": {"seq": [stale, {"out": json.dumps(fresh)}]}}))
        out = self.add("Morgan Lee", "--phone", "+12065550101", "--replace", "--no-apple")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        sent = self.calls("updateContact")
        self.assertEqual([body_of(c)["etag"] for c in sent], ["E1", "E2"])
        self.assertEqual(len(self.calls("people get")), 2)

    def test_a_second_stale_etag_is_not_retried_again(self):
        self.made()
        stale = {"rc": 1, "out": json.dumps({"error": {"code": 400, "status": "FAILED_PRECONDITION",
                 "message": "Request person.etag is different than the current person.etag."}})}
        self.plan("gws", dict(GWS_OK, updateContact=stale))
        out = self.add("Morgan Lee", "--phone", "+12065550101", "--replace", "--no-apple")
        self.assertEqual(out.returncode, 3)
        self.assertEqual(len(self.calls("updateContact")), 2)
        self.assertIn("etag", out.stdout)

    def test_an_unlinked_google_contact_is_never_created_by_an_edit(self):
        self.add("Morgan Lee", "--phone", "+12065550100", "--no-apple")
        self.add("Morgan Lee", "--phone", "+12065550101", "--replace", "--no-apple")
        self.assertEqual(self.calls("gws"), [])


class Rename(Base):
    def made(self):
        self.add("Morgan Lee", "--phone", "+12065550100", "--google")

    def test_rename_moves_the_row_the_card_and_the_google_contact(self):
        self.made()
        out = self.comms("rename", "Morgan Lee", "Michael Lee")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        book = self.entries()
        self.assertEqual(list(book), ["michael lee"])
        self.assertEqual(book["michael lee"]["name"], "Michael Lee")
        self.assertEqual(book["michael lee"]["apple_id"], "CARD-1")
        self.assertEqual(book["michael lee"]["google_id"], "people/c123")
        card = self.calls("update --id")[0]
        self.assertEqual(card[card.index("--given") + 1], "Michael")
        self.assertEqual(card[card.index("--family") + 1], "Lee")
        names = [card[i + 1] for i, x in enumerate(card) if x == "--expect-name"]
        self.assertEqual(sorted(names), ["Michael Lee", "Morgan Lee"])
        google = self.calls("updateContact")[0]
        self.assertEqual(params_of(google)["updatePersonFields"], "names")
        self.assertEqual(body_of(google)["names"][0]["givenName"], "Michael")
        self.assertNotIn("displayName", body_of(google)["names"][0])
        self.assertEqual(self.get("Michael Lee", "whatsapp").stdout.strip(), "12065550100")
        self.assertEqual(self.get("Morgan Lee", "whatsapp").returncode, 1)

    def test_rename_onto_an_existing_name_is_refused_and_changes_nothing(self):
        self.made()
        self.add("Ann Lee", "--phone", "+12065550111", "--no-apple")
        before = self.entries()
        out = self.comms("rename", "Morgan Lee", "ann  LEE")
        self.assertEqual(out.returncode, 1)
        self.assertIn("already", out.stdout)
        self.assertEqual(self.entries(), before)
        self.assertEqual(self.calls("update --id"), [])
        self.assertEqual(self.calls("updateContact"), [])

    def test_rename_of_a_name_nobody_has_is_refused(self):
        self.assertEqual(self.comms("rename", "Nobody", "Someone").returncode, 1)

    def test_a_card_from_before_the_tool_is_not_renamed_without_update_card(self):
        self.plan("apple-contacts", dict(APPLE_OK, find={"out": json.dumps(
            {"by_handle": [{"id": "OLD-1", "name": "M Lee"}], "by_name": []})}))
        self.add("Morgan Lee", "--phone", "+12065550100")
        out = self.comms("rename", "Morgan Lee", "Michael Lee")
        self.assertEqual(out.returncode, 0)
        self.assertEqual(self.calls("update --id"), [])
        self.assertIn("--update-card", out.stdout)
        self.assertIn("michael lee", self.entries())
        self.assertEqual(self.comms("rename", "Michael Lee", "Morgan Lee", "--update-card").returncode, 0)
        self.assertEqual(len(self.calls("update --id")), 1)

    def test_a_failed_google_rename_can_be_run_again(self):
        self.made()
        self.plan("gws", dict(GWS_OK, updateContact={"rc": 1, "out": "boom"}))
        out = self.comms("rename", "Morgan Lee", "Michael Lee")
        self.assertEqual(out.returncode, 3)
        self.assertIn("michael lee", self.entries())           # the directory moved first
        self.plan("gws", GWS_OK)
        again = self.comms("rename", "Morgan Lee", "Michael Lee")
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertNotIn("name_sync", self.entries()["michael lee"])

    def test_a_case_only_rename_is_allowed(self):
        self.add("morgan lee", "--phone", "+12065550100", "--no-apple")
        self.assertEqual(self.comms("rename", "morgan lee", "Morgan Lee").returncode, 0)
        self.assertEqual(self.entries()["morgan lee"]["name"], "Morgan Lee")


class Find(Base):
    def setUp(self):
        super().setUp()
        self.add("Morgan Lee", "--phone", "+12065550100", "--email", "m@example.invalid", "--no-apple")
        self.apple_rows = {"total": 1, "rows": [{"id": "A1", "name": "Morgany Apple",
                                                 "phones": ["(206) 555-0100"], "emails": []}]}
        self.google_rows = {"connections": [{"resourceName": "people/c9",
                            "names": [{"displayName": "Morgan G"}],
                            "phoneNumbers": [{"value": "+1 206-555-0100"}]}], "totalItems": 1}
        self.plan("apple-contacts", dict(APPLE_OK, search={"out": json.dumps(self.apple_rows)}))
        self.plan("gws", dict(GWS_OK, **{"connections list": {"out": json.dumps(self.google_rows)}}))

    def test_find_by_number_asks_every_source_and_names_each_row(self):
        out = self.comms("find", "206 555 0100")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        lines = out.stdout.splitlines()
        self.assertTrue(lines[0].startswith("directory"), lines)      # directory first
        self.assertTrue(any(l.startswith("apple") and "Morgany Apple" in l for l in lines))
        self.assertTrue(any(l.startswith("google") and "Morgan G" in l for l in lines))

    def test_find_json_gives_rows_with_a_source(self):
        data = json.loads(self.comms("find", "lee", "--json").stdout)
        self.assertEqual([r["source"] for r in data["rows"]][0], "directory")
        self.assertEqual(data["errors"], {})

    def test_one_source_failing_still_returns_the_others(self):
        self.plan("apple-contacts", {"search": {"rc": 5, "out": json.dumps({"error": "no_access", "auth": "denied"})}})
        out = self.comms("find", "0100")
        self.assertEqual(out.returncode, 3)
        self.assertEqual(len([l for l in out.stdout.splitlines() if l.startswith("apple")]), 1)
        self.assertIn("denied", out.stdout)
        self.assertTrue(any(l.startswith("directory") for l in out.stdout.splitlines()))
        self.assertTrue(any(l.startswith("google") and "Morgan G" in l for l in out.stdout.splitlines()))

    def test_a_missing_google_scope_is_one_line_and_the_rest_continue(self):
        self.plan("gws", GWS_NO_SCOPE)
        self.plan("gws", dict(GWS_NO_SCOPE, **{"connections list": SCOPE_ERROR}))
        out = self.comms("find", "0100", "--json")
        data = json.loads(out.stdout)
        self.assertIn("google", data["errors"])
        self.assertEqual({r["source"] for r in data["rows"]}, {"directory", "apple"})
        self.assertEqual(out.returncode, 3)

    def test_source_limits_the_search_to_one_book(self):
        out = self.comms("find", "0100", "--source", "apple")
        self.assertEqual(self.calls("connections list"), [])
        self.assertTrue(all(l.startswith("apple") for l in out.stdout.splitlines()))
        self.assertEqual(self.comms("find", "0100", "--source", "directory").returncode, 0)
        self.assertEqual(len(self.calls("search --text")), 1)

    def test_find_matches_a_name_and_an_email_in_the_directory(self):
        for text in ("morgan", "M@EXAMPLE"):
            out = self.comms("find", text, "--source", "directory")
            self.assertIn("Morgan Lee", out.stdout, text)
        self.assertEqual(self.comms("find", "zzznobody", "--source", "directory").stdout.strip(),
                         "no match in directory")

    def test_find_writes_no_file_anywhere(self):
        def tree():
            seen = {}
            for base, _, files in os.walk(self.home):
                if base.startswith(self.fake):
                    continue
                for f in files:
                    path = os.path.join(base, f)
                    if os.path.islink(path):
                        continue
                    with open(path, "rb") as fh:
                        seen[path] = fh.read()
            return seen
        before = tree()
        self.comms("find", "0100")
        self.comms("list", "--source", "apple")
        self.assertEqual(tree(), before)

    def test_an_empty_search_is_refused(self):
        self.assertEqual(self.comms("find", "  ").returncode, 1)


class ListSources(Base):
    def test_apple_list_shows_the_true_total_not_just_the_page(self):
        rows = [{"id": "A%d" % i, "name": "Person %d" % i, "phones": ["+1206555%04d" % i], "emails": []}
                for i in range(2)]
        self.plan("apple-contacts", dict(APPLE_OK, list={"out": json.dumps({"total": 588, "rows": rows})}))
        out = self.comms("list", "--source", "apple", "--limit", "2")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("showing 2 of 588", out.stdout)
        call = self.calls("list --limit")[0]
        self.assertEqual(call[call.index("--limit") + 1], "2")

    def test_a_negative_limit_is_refused_before_any_book_is_read(self):
        self.assertEqual(self.comms("list", "--source", "apple", "--limit", "-3").returncode, 1)
        self.assertEqual(self.calls("list --limit"), [])

    def test_the_default_limit_is_fifty(self):
        self.comms("list", "--source", "apple")
        call = self.calls("list --limit")[0]
        self.assertEqual(call[call.index("--limit") + 1], "50")

    def test_google_list_reads_the_total_from_google(self):
        people = [{"resourceName": "people/c%d" % i, "names": [{"displayName": "G %d" % i}]} for i in range(3)]
        self.plan("gws", dict(GWS_OK, **{"connections list": {"out": json.dumps(
            {"connections": people, "totalItems": 900})}}))
        out = self.comms("list", "--source", "google", "--limit", "3", "--json")
        data = json.loads(out.stdout)
        self.assertEqual((data["source"], data["total"], len(data["rows"])), ("google", 900, 3))
        self.assertEqual(params_of(self.calls("connections list")[0])["pageSize"], 3)

    def test_a_failing_source_says_so_in_one_line(self):
        self.plan("apple-contacts", {"list": {"rc": 5, "out": json.dumps({"error": "no_access", "auth": "denied"})}})
        out = self.comms("list", "--source", "apple")
        self.assertEqual(out.returncode, 3)
        self.assertEqual(len(out.stdout.strip().splitlines()), 1)
        self.assertIn("denied", out.stdout)

    def test_the_directory_list_is_unchanged(self):
        self.add("Morgan Lee", "--phone", "+12065550100", "--no-apple")
        self.assertIn("Morgan Lee", self.comms("list").stdout)
        self.assertEqual(self.calls("list --limit"), [])


class GoogleAccount(Base):
    """--google-account runs through gws-as, and never touches the main login's cache."""
    ACCOUNT = "a@example.invalid"

    def setUp(self):
        super().setUp()
        self.main_cache = os.path.join(self.home, ".config", "gws", "token_cache.json")
        self.acct_cache = os.path.join(self.home, ".config", "gws-accounts", self.ACCOUNT, "token_cache.json")
        for path in (self.main_cache, self.acct_cache):
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as fh:
                fh.write("old token")

    def add_google(self, *extra):
        return self.add("Morgan Lee", "--email", "m@example.invalid", "--no-apple", "--google",
                        "--google-account", self.ACCOUNT, *extra)

    def test_the_account_runs_through_gws_as_and_is_stored_with_the_id(self):
        out = self.add_google()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.calls("gws"), [c for c in self.calls("gws") if c[0] == "gws-as"])
        self.assertEqual(self.calls("createContact")[0][:2], ["gws-as", self.ACCOUNT])
        entry = self.entries()["morgan lee"]
        self.assertEqual((entry["google_id"], entry["google_account"]), ("people/c123", self.ACCOUNT))

    def test_rm_and_edit_use_the_stored_account(self):
        self.add_google()
        self.add("Morgan Lee", "--email", "new@example.invalid", "--replace", "--no-apple")
        self.assertEqual(self.calls("updateContact")[0][:2], ["gws-as", self.ACCOUNT])
        self.assertEqual(self.calls("people get")[0][:2], ["gws-as", self.ACCOUNT])
        self.assertEqual(self.comms("rm", "Morgan Lee", "--google").returncode, 0)
        self.assertEqual(self.calls("deleteContact")[0][:2], ["gws-as", self.ACCOUNT])
        self.assertEqual([c for c in self.calls("gws") if c[0] == "gws"], [])

    def test_a_different_account_on_rm_is_refused(self):
        self.add_google()
        out = self.comms("rm", "Morgan Lee", "--google", "--google-account", "b@example.invalid")
        self.assertEqual(out.returncode, 3)
        self.assertEqual(self.calls("deleteContact"), [])
        self.assertIn("morgan lee", self.entries())

    def test_a_stale_token_clears_that_accounts_cache_not_the_main_one(self):
        status = {"out": json.dumps({"scopes": ["https://www.googleapis.com/auth/contacts"]})}
        self.plan("gws-as", {"auth status": status, "createContact": {
            "while_file": self.acct_cache, "before": SCOPE_ERROR,
            "after": {"out": json.dumps({"resourceName": "people/c123"})}}})
        out = self.add_google()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertFalse(os.path.exists(self.acct_cache))
        self.assertTrue(os.path.exists(self.main_cache))
        self.assertEqual([c for c in self.calls("gws") if c[0] == "gws"], [])

    def test_a_refused_account_passes_the_refusal_through(self):
        self.plan("gws-as", {"createContact": {"rc": 2, "err": "gws-as: no saved login for a@example.invalid\n"}})
        out = self.add_google()
        self.assertEqual(out.returncode, 3)
        self.assertIn("no saved login for a@example.invalid", out.stdout)
        self.assertTrue(os.path.exists(self.main_cache))
        self.assertNotIn("google_id", self.entries()["morgan lee"])

    def test_a_path_in_the_account_is_refused_before_anything_runs(self):
        out = self.add("Morgan Lee", "--email", "m@example.invalid", "--no-apple", "--google",
                       "--google-account", "../../x@y.z")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(self.calls("gws"), [])


if __name__ == "__main__":
    unittest.main()
