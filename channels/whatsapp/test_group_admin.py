"""group-create / group-add / group-remove / group-rename / group-leave / group-members,
block / unblock. Network mocked: these reach real people, so there is no live run of the writes.

The ways each can go wrong, each pinned below:
  every write - a PUT/POST/invite without --yes; a preview line that starts `About to send to`
                (two parsers read that line for real sends); a name that matches two groups and
                the CLI picks one; a DM treated as a group.
  create      - no members, a junk number, an empty name; a room made but never bridged and the CLI
                still says "created"; a member that cannot be resolved halfway through.
  add/remove  - a number that is not a number; adding yourself; adding someone already in; removing
                someone who is not in; a kick the bridge undid reported as done.
  rename      - the same name again; the bridge reverting the name reported as done.
  leave       - leaving a DM (that is not a group).
  block       - blocking inside a group portal; a bridge error reply reported as done; unblock
                silently pretending to work when the bridge has no such command (exit 5).

Run: python3 -m pytest -q test_group_admin.py   (from this directory)
"""
import argparse
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Hermetic config: fake values only, never the real kit config.
import atexit  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402
_CFG_FD, _CFG_PATH = tempfile.mkstemp(suffix=".json")
with os.fdopen(_CFG_FD, "w") as _cfg_file:
    json.dump({"whatsapp": {"matrix_user": "owner", "owner_lid": "4440100",
                            "self_number": "12065550100", "bot_user": "@whatsappbot:x",
                            "homeserver": "https://matrix.example.com"}}, _cfg_file)
atexit.register(os.unlink, _CFG_PATH)
os.environ["COMMS_KIT_CONFIG"] = _CFG_PATH
import whatsapp_mautrix as W  # noqa: E402

ROOM = "!grp:x"
DM = "!dm:x"
BOT = "@whatsappbot:x"
ME = "@owner:x"
ALICE = "@whatsapp_12065550101:x"
BOB = "@whatsapp_lid-777000111:x"
SELF_NUMBER = "12065550100"
GROUP_JID = "120363" + "@g.us"
GROUP = {"portal_id": ROOM, "is_group": True, "label": "Team"}


class FakeGroup:
    def __init__(self, members=None, name="Team"):
        self.members = dict(members if members is not None else
                            {ALICE: {"display_name": "Alice"}, BOB: {"display_name": "Bob"}})
        self.name = name
        self.calls = []
        self.messages = []             # extra timeline events (bot notices ...)
        self.kick_sticks = True        # False: the bridge puts the member back
        self.rename_sticks = True
        self.bridged_jid = GROUP_JID
        self.bot_joins = True
        self.created_name = None

    def writes(self):
        return [(m, p, b) for m, p, b in self.calls if m in ("PUT", "POST")]

    def request(self, method, path, token=None, body=None, params=None):
        self.calls.append((method, path, body))
        unq = W.urllib.parse.unquote
        if method == "GET" and path.endswith("/joined_members"):
            return {"joined": self.members}
        if method == "GET" and path.endswith("/state"):
            return [{"type": "m.room.name", "state_key": "", "content": {"name": self.name}},
                    {"type": "m.bridge", "state_key": "x", "content": {"channel": {"id": self.bridged_jid}}},
                    {"type": "m.room.power_levels", "state_key": "",
                     "content": {"users": {ALICE: 100, ME: 50}}}]
        if method == "GET" and "/state/m.room.name" in path:
            return {"name": self.name}
        if method == "GET" and path.endswith("/messages"):
            return {"chunk": list(self.messages), "end": "t"}
        if method == "POST" and path.endswith("/kick"):
            if self.kick_sticks:
                self.members.pop(body["user_id"], None)
            return {}
        if method == "POST" and path.endswith("/invite"):
            self.members[body["user_id"]] = {"display_name": ""}
            return {}
        if method == "POST" and path.endswith("/leave"):
            return {}
        if method == "POST" and path.endswith("/createRoom"):
            self.created_name = body.get("name")
            self.members.update({u: {} for u in body.get("invite", []) if not u.startswith("@whatsappbot")})
            if self.bot_joins:
                self.members[BOT] = {}
            return {"room_id": "!new:x"}
        if method == "PUT" and "/state/m.room.name" in path:
            if self.rename_sticks:
                self.name = body["name"]
            return {"event_id": "$state"}
        if method == "PUT" and "/send/m.room.message/" in path:
            return {"event_id": "$cmd"}
        raise AssertionError(f"unexpected {method} {path}")


def run(fn, fake, args, chat=None, extra=()):
    out, code = io.StringIO(), 0
    chat = chat or GROUP
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(W, "_require_session", return_value=("tok", "!mgmt:x")))
        stack.enter_context(mock.patch.object(W, "_resolve_chat", return_value=chat))
        stack.enter_context(mock.patch.object(W, "_request", side_effect=fake.request))
        stack.enter_context(mock.patch.object(W, "_self_number", return_value=SELF_NUMBER))
        stack.enter_context(mock.patch.object(W, "_ghost_for_digits",
                                              side_effect=lambda t, r, d: {"12065550101": ALICE,
                                                                           "12065550199": "@whatsapp_12065550199:x"}[d]))
        stack.enter_context(mock.patch.object(W, "_await_portal_bot_reply", return_value=None))
        stack.enter_context(mock.patch.object(W, "_read_self_number_cache", return_value=SELF_NUMBER))
        stack.enter_context(mock.patch.object(W.time, "sleep"))
        for p in extra:
            stack.enter_context(p)
        stack.enter_context(contextlib.redirect_stdout(out))
        try:
            fn(args)
        except SystemExit as e:
            code = e.code
    return out.getvalue(), code or 0


def ns(**kw):
    base = dict(group="Team", digits="12065550199", name="New name", member=["12065550101"],
                yes=False, report_spam=False)
    base.update(kw)
    return argparse.Namespace(**base)


def no_send_preview(test, out):
    for line in out.splitlines():
        test.assertFalse(line.startswith("About to send to"), line)


class DryRunsAndPreviews(unittest.TestCase):
    """The same rule for every write verb: dry run unless --yes, and never the send preview."""

    CASES = [
        (W.cmd_group_add, dict(digits="12065550199"), "About to add"),
        (W.cmd_group_remove, dict(digits="12065550101"), "About to remove"),
        (W.cmd_group_rename, dict(name="Fresh"), "About to rename"),
        (W.cmd_group_leave, dict(), "About to leave"),
        (W.cmd_group_create, dict(group=None, name="Crew", member=["12065550101"]), "About to create"),
        (W.cmd_block, dict(digits="12065550101"), "About to block"),
    ]

    def test_no_write_without_yes_and_no_send_preview(self):
        for fn, kw, words in self.CASES:
            with self.subTest(fn.__name__):
                fake = FakeGroup()
                chat = {"portal_id": DM, "is_group": False, "label": "12065550101"} \
                    if fn is W.cmd_block else GROUP
                out, code = run(fn, fake, ns(**kw), chat=chat)
                self.assertEqual(code, 0, out)
                self.assertEqual(fake.writes(), [], out)
                self.assertIn(words, out)
                self.assertIn("Dry run", out)
                no_send_preview(self, out)

    def test_members_read_prints_no_send_preview_and_writes_nothing(self):
        fake = FakeGroup()
        out, code = run(W.cmd_group_members, fake, ns())
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])
        no_send_preview(self, out)


class GroupTargeting(unittest.TestCase):
    def test_a_dm_is_not_a_group_for_every_group_verb(self):
        dm = {"portal_id": DM, "is_group": False, "label": "Alex Doe"}
        for fn in (W.cmd_group_add, W.cmd_group_remove, W.cmd_group_rename, W.cmd_group_leave,
                   W.cmd_group_members):
            with self.subTest(fn.__name__):
                fake = FakeGroup()
                out, code = run(fn, fake, ns(yes=True, digits="12065550101"), chat=dm)
                self.assertEqual(code, 1)
                self.assertEqual(fake.writes(), [])
                self.assertIn("not a group", out)

    def test_two_groups_with_the_same_name_fail_instead_of_guessing(self):
        # the real resolver, with two portals that match "team"
        fake = FakeGroup()
        out, code = io.StringIO(), 0
        with mock.patch.object(W, "_require_session", return_value=("tok", "!mgmt:x")), \
             mock.patch.object(W, "_request", side_effect=fake.request), \
             mock.patch.object(W, "_self_number", return_value=SELF_NUMBER), \
             mock.patch.object(W, "_saved_contact_number", return_value=None), \
             mock.patch.object(W, "_joined_portals",
                               return_value=[("!a:x", "Team Alpha"), ("!b:x", "Team Beta")]), \
             contextlib.redirect_stdout(out):
            try:
                W.cmd_group_add(ns(group="team", digits="12065550199", yes=True))
            except SystemExit as e:
                code = e.code
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", out.getvalue())
        self.assertEqual(fake.writes(), [])


class Add(unittest.TestCase):
    def test_yes_invites_the_resolved_ghost_and_confirms_it_is_in(self):
        fake = FakeGroup()
        out, code = run(W.cmd_group_add, fake, ns(digits="12065550199", yes=True))
        self.assertEqual(code, 0, out)
        [(m, path, body)] = fake.writes()
        self.assertTrue(path.endswith("/invite"))
        self.assertEqual(body, {"user_id": "@whatsapp_12065550199:x"})
        self.assertIn("Added", out)

    def test_junk_numbers_refused_before_any_call(self):
        for bad in ("abc", "12", "+", "1206555010x"):
            fake = FakeGroup()
            out, code = run(W.cmd_group_add, fake, ns(digits=bad, yes=True))
            self.assertEqual(code, 1, bad)
            self.assertEqual(fake.calls, [], bad)

    def test_plus_and_dashes_are_cleaned(self):
        fake = FakeGroup()
        out, code = run(W.cmd_group_add, fake, ns(digits="+1 (206) 555-0199", yes=True))
        self.assertEqual(code, 0, out)

    def test_adding_yourself_refused(self):
        fake = FakeGroup()
        out, code = run(W.cmd_group_add, fake, ns(digits=SELF_NUMBER, yes=True))
        self.assertEqual(code, 1)
        self.assertEqual(fake.writes(), [])

    def test_already_a_member_sends_nothing(self):
        fake = FakeGroup()
        out, code = run(W.cmd_group_add, fake, ns(digits="12065550101", yes=True))
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])
        self.assertIn("already in", out)

    def test_bridge_notice_after_invite_is_a_failure(self):
        fake = FakeGroup()
        fake.messages = [{"type": "m.room.message", "sender": BOT, "origin_server_ts": 4_000_000_000_000,
                          "content": {"msgtype": "m.notice", "body": "Failed to add user: not an admin"}}]
        out, code = run(W.cmd_group_add, fake, ns(digits="12065550199", yes=True))
        self.assertEqual(code, 4)
        self.assertIn("NOT ADDED", out)
        self.assertNotIn("Added", out)


class Remove(unittest.TestCase):
    def test_yes_kicks_and_confirms_the_member_is_gone(self):
        fake = FakeGroup()
        out, code = run(W.cmd_group_remove, fake, ns(digits="12065550101", yes=True))
        self.assertEqual(code, 0, out)
        [(m, path, body)] = fake.writes()
        self.assertTrue(path.endswith("/kick"))
        self.assertEqual(body["user_id"], ALICE)
        self.assertIn("Removed", out)

    def test_not_a_member_sends_nothing(self):
        fake = FakeGroup()
        out, code = run(W.cmd_group_remove, fake, ns(digits="12065550199", yes=True))
        self.assertEqual(code, 1)
        self.assertEqual(fake.writes(), [])
        self.assertIn("not in", out)

    def test_a_kick_the_bridge_undid_is_a_failure(self):
        fake = FakeGroup()
        fake.kick_sticks = False
        out, code = run(W.cmd_group_remove, fake, ns(digits="12065550101", yes=True))
        self.assertEqual(code, 4)
        self.assertNotIn("Removed", out)
        self.assertIn("NOT REMOVED", out)


class Rename(unittest.TestCase):
    def test_yes_sets_the_room_name_and_confirms(self):
        fake = FakeGroup()
        out, code = run(W.cmd_group_rename, fake, ns(name="Fresh", yes=True))
        self.assertEqual(code, 0, out)
        [(m, path, body)] = fake.writes()
        self.assertEqual(m, "PUT")
        self.assertIn("/state/m.room.name", path)
        self.assertEqual(body, {"name": "Fresh"})
        self.assertIn("Renamed", out)

    def test_same_name_is_a_no_op(self):
        fake = FakeGroup()
        out, code = run(W.cmd_group_rename, fake, ns(name="Team", yes=True))
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])

    def test_empty_name_refused(self):
        fake = FakeGroup()
        out, code = run(W.cmd_group_rename, fake, ns(name="  ", yes=True))
        self.assertEqual(code, 1)
        self.assertEqual(fake.calls, [])

    def test_bridge_reverting_the_name_is_a_failure(self):
        fake = FakeGroup()
        fake.rename_sticks = False
        out, code = run(W.cmd_group_rename, fake, ns(name="Fresh", yes=True))
        self.assertEqual(code, 4)
        self.assertNotIn("Renamed", out)


class Leave(unittest.TestCase):
    def test_yes_leaves_the_room_once(self):
        fake = FakeGroup()
        out, code = run(W.cmd_group_leave, fake, ns(yes=True))
        self.assertEqual(code, 0, out)
        [(m, path, _b)] = fake.writes()
        self.assertTrue(path.endswith("/leave"))
        self.assertIn("Left", out)
        self.assertIn("check", out.lower())    # leaving cannot be read back; it says so


class Members(unittest.TestCase):
    def test_lists_names_numbers_and_admins_read_only(self):
        fake = FakeGroup()
        out, code = run(W.cmd_group_members, fake, ns())
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])
        self.assertIn('GROUP "Team"', out)
        self.assertIn("2 members", out)
        self.assertIn("Alice", out)
        self.assertIn("12065550101", out)
        self.assertIn("admin", out)
        self.assertIn("Bob", out)


class Create(unittest.TestCase):
    def go(self, fake=None, **kw):
        fake = fake or FakeGroup(members={})
        out, code = run(W.cmd_group_create, fake, ns(group=None, **kw), extra=[
            mock.patch.object(W, "_portal_channel_jid", side_effect=lambda t, r: fake.bridged_jid)])
        return fake, out, code

    def test_no_members_refused_before_any_call(self):
        fake, out, code = self.go(name="Crew", member=[], yes=True)
        self.assertEqual(code, 1)
        self.assertEqual(fake.calls, [])

    def test_junk_member_refused_before_any_call(self):
        fake, out, code = self.go(name="Crew", member=["12065550101", "nope"], yes=True)
        self.assertEqual(code, 1)
        self.assertEqual(fake.calls, [])

    def test_empty_name_refused(self):
        fake, out, code = self.go(name=" ", member=["12065550101"], yes=True)
        self.assertEqual(code, 1)
        self.assertEqual(fake.calls, [])

    def test_yes_makes_a_room_then_runs_the_bridge_create_group_command(self):
        fake, out, code = self.go(name="Crew", member=["12065550101"], yes=True)
        self.assertEqual(code, 0, out)
        kinds = [(m, p.rsplit("/", 1)[-1] if m == "POST" else "put") for m, p, _b in fake.writes()]
        self.assertEqual(kinds[0], ("POST", "createRoom"))
        create = fake.writes()[0][2]
        self.assertEqual(create["name"], "Crew")
        self.assertIn(ALICE, create["invite"])
        self.assertIn(W.BOT_USER, create["invite"])
        cmd = [b for m, p, b in fake.writes() if m == "PUT" and "/send/m.room.message/" in p]
        self.assertEqual(cmd, [{"msgtype": "m.text", "body": "!wa create-group"}])
        self.assertIn(GROUP_JID, out)
        self.assertIn("Created group", out)

    def test_room_that_never_became_a_group_is_a_failure(self):
        fake = FakeGroup(members={})
        fake.bridged_jid = ""
        fake, out, code = self.go(fake=fake, name="Crew", member=["12065550101"], yes=True)
        self.assertEqual(code, 4)
        self.assertNotIn("Created group", out)
        self.assertIn("NOT CREATED", out)

    def test_bot_that_never_joins_stops_before_the_command(self):
        fake = FakeGroup(members={})
        fake.bot_joins = False
        fake, out, code = self.go(fake=fake, name="Crew", member=["12065550101"], yes=True)
        self.assertEqual(code, 4)
        self.assertEqual([b for m, p, b in fake.writes() if m == "PUT"], [])

    def test_member_that_cannot_be_resolved_creates_nothing(self):
        fake = FakeGroup(members={})
        with mock.patch.object(W, "_ghost_for_digits", side_effect=SystemExit(1)):
            out, code = io.StringIO(), 0
            with mock.patch.object(W, "_require_session", return_value=("tok", "!mgmt:x")), \
                 mock.patch.object(W, "_request", side_effect=fake.request), \
                 mock.patch.object(W, "_self_number", return_value=SELF_NUMBER), \
                 contextlib.redirect_stdout(out):
                try:
                    W.cmd_group_create(ns(group=None, name="Crew", member=["12065550101"], yes=True))
                except SystemExit as e:
                    code = e.code
        self.assertNotEqual(code, 0)
        self.assertEqual(fake.writes(), [])


class Block(unittest.TestCase):
    DM_CHAT = {"portal_id": DM, "is_group": False, "label": "12065550101"}

    def go(self, fake, chat=None, **kw):
        return run(W.cmd_block, fake, ns(digits="12065550101", **kw), chat=chat or self.DM_CHAT)

    def test_yes_posts_only_the_fixed_bridge_command_into_the_dm_portal(self):
        fake = FakeGroup()
        out, code = self.go(fake, yes=True)
        self.assertEqual(code, 0, out)
        [(m, path, body)] = fake.writes()
        self.assertIn(W._encode_room(DM), path)
        self.assertEqual(body, {"msgtype": "m.text", "body": "!wa block-user"})

    def test_a_group_portal_is_refused(self):
        fake = FakeGroup()
        out, code = self.go(fake, chat=GROUP, yes=True)
        self.assertEqual(code, 1)
        self.assertEqual(fake.writes(), [])

    def test_bridge_error_reply_is_a_failure(self):
        fake = FakeGroup()
        out, code = run(W.cmd_block, fake, ns(digits="12065550101", yes=True), chat=self.DM_CHAT,
                        extra=[mock.patch.object(W, "_await_portal_bot_reply",
                                                 return_value="Failed to block user: not supported")])
        self.assertEqual(code, 4)
        self.assertIn("NOT BLOCKED", out)

    def test_report_spam_flag_is_passed_to_the_command(self):
        fake = FakeGroup()
        self.go(fake, yes=True, report_spam=True)
        self.assertEqual(fake.writes()[0][2]["body"], "!wa block-user --report-spam")


class Unblock(unittest.TestCase):
    def test_exits_5_with_the_reason_and_touches_nothing(self):
        fake = FakeGroup()
        out, code = run(W.cmd_unblock, fake, ns(digits="12065550101", yes=True))
        self.assertEqual(code, 5)
        self.assertEqual(fake.calls, [])
        self.assertIn("block-user", out)
        self.assertEqual(len(out.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
