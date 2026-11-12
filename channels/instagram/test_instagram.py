#!/usr/bin/env python3
"""Tests for instagram.py with a fake Matrix server. No network, no account.

Failure modes covered (written before the code):
  1. a write verb runs without --yes (a dry run must make no write call at all)
  2. --yes without --since still sends a text
  3. a newer message from the other person still lets the send through; our own newer
     message (sent from this Matrix user or from our own Instagram ghost) wrongly blocks it
  4. the bridge login is not CONNECTED (logged out, bad credentials, a challenge, still
     connecting, no login at all, no answer) and the send still goes out
  5. after Instagram refused a send in a way that smells like a restriction, the next send
     still goes out (the send hold), or an ordinary refusal wrongly sets the hold
  6. the daily send cap or the minimum gap between sends is not enforced
  7. the money lock is skipped (text, or the message a reaction points at)
  8. a refusing outbound gate still lets the send through
  9. the bridge status is not read: SUCCESS must be "Sent" with one archive row; FAIL must be
     a failure with no row; no status in time must be UNCERTAIN (exit 4), never retried
 10. a transport error before the send is UNCERTAIN instead of NOT SENT, or the other way round
 11. a chat name that matches two chats picks one; a room that is not an Instagram portal
     (the management room, a WhatsApp room) is accepted as a target
 12. a room id is lowercased on its way to the server (learning: a key is not an address)
 13. room state that changed inside the sync timeline is missed (learning: apply timeline state)
 14. an invite from someone other than the bridge bot is joined
 15. edit or delete of a message that is not ours is sent
 16. a reply, reaction or edit points at a message that is not in that chat
 17. login shows the password, or does not stop on success or failure
 18. reading history sends a read receipt (it must not: a receipt shows "Seen" on Instagram)
"""
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import instagram as IG  # noqa: E402

ME = "@owner:example.org"
BOT = "@instagrambot:example.org"
OWN_GHOST = "@instagram_1111:example.org"
ALEX = "@instagram_2222:example.org"
SAM = "@instagram_3333:example.org"
MGMT = "!MgmtRoom:example.org"
DM = "!AlexDmRoom:example.org"        # mixed case on purpose (failure mode 12)
GROUP = "!GroupRoom:example.org"
WA = "!WhatsAppRoom:example.org"
T_OLD = 1_790_000_000_000             # ms: the message the owner read
T_NEW = T_OLD + 60_000


def bridge_state(protocol="instagram"):
    return {"type": "m.bridge", "state_key": "x", "content": {"protocol": {"id": protocol}}}


def name_state(name):
    return {"type": "m.room.name", "state_key": "", "content": {"name": name}}


def member(uid, name, membership="join"):
    return {"type": "m.room.member", "state_key": uid, "content": {"membership": membership, "displayname": name}}


def text_event(eid, sender, ts, body, **extra):
    content = {"msgtype": "m.text", "body": body}
    content.update(extra)
    return {"type": "m.room.message", "event_id": eid, "sender": sender, "origin_server_ts": ts, "content": content}


class FakeMatrix:
    """A tiny homeserver plus bridge. Records every call."""

    def __init__(self):
        self.calls = []
        self.login_reply = "* `1111` (owner.example) - `CONNECTED`"
        self.status = "SUCCESS"          # what the bridge answers for a portal send
        self.status_message = ""
        self.send_exc = None
        self.next_id = 100
        self.state = {
            DM: [bridge_state(), name_state("Alex Doe"), member(ME, "Owner"), member(BOT, "bot"), member(ALEX, "Alex Doe")],
            GROUP: [bridge_state(), name_state("Weekend plans"), member(ME, "Owner"), member(BOT, "bot"),
                    member(ALEX, "Alex Doe"), member(SAM, "Sam Roe")],
            WA: [bridge_state("whatsapp"), name_state("Alex Doe WhatsApp"), member(ME, "Owner")],
            MGMT: [name_state("Instagram bridge bot"), member(ME, "Owner"), member(BOT, "bot")],
        }
        self.timeline = {
            DM: [text_event("$a1", ALEX, T_OLD - 1000, "Hi there"), text_event("$m1", ME, T_OLD - 500, "Hello"),
                 text_event("$a2", ALEX, T_OLD, "Are we still on for Friday?")],
            GROUP: [text_event("$g1", SAM, T_OLD, "Who brings snacks?")],
            WA: [text_event("$w1", ALEX, T_OLD, "wa")],
            MGMT: [],
        }
        self.invites = {}
        self.unread = {DM: 1}

    # -- reads
    def whoami(self):
        self.calls.append(("whoami",))
        return ME

    def sync(self, timeline_limit):
        self.calls.append(("sync", timeline_limit))
        join = {}
        for room, st in self.state.items():
            join[room] = {"state": {"events": list(st)},
                          "timeline": {"events": list(self.timeline.get(room, []))[-timeline_limit:]},
                          "unread_notifications": {"notification_count": self.unread.get(room, 0)}}
        invite = {room: {"invite_state": {"events": evs}} for room, evs in self.invites.items()}
        return {"rooms": {"join": join, "invite": invite}}

    def messages(self, room, limit):
        self.calls.append(("messages", room, limit))
        return list(reversed(self.timeline.get(room, [])))[:limit]

    def get_event(self, room, event_id):
        self.calls.append(("get_event", room, event_id))
        for e in self.timeline.get(room, []):
            if e.get("event_id") == event_id:
                return e
        raise IG.ApiError("event not found in that chat")

    def joined_members(self, room):
        self.calls.append(("joined_members", room))
        return {e["state_key"]: e["content"].get("displayname") for e in self.state.get(room, [])
                if e["type"] == "m.room.member" and e["content"].get("membership") == "join"}

    def download(self, mxc):
        self.calls.append(("download", mxc))
        return b"IMAGEBYTES"

    # -- writes
    def _eid(self):
        self.next_id += 1
        return "$out%d" % self.next_id

    def send_event(self, room, etype, content, txn):
        self.calls.append(("send_event", room, etype, content))
        if self.send_exc and room != MGMT:
            raise self.send_exc
        eid = self._eid()
        now = T_NEW + self.next_id
        self.timeline.setdefault(room, []).append(
            {"type": etype, "event_id": eid, "sender": ME, "origin_server_ts": now, "content": content})
        if room == MGMT:
            body = content.get("body", "")
            if body.strip() == "list-logins" and self.login_reply is not None:
                self.timeline[MGMT].append(text_event(self._eid(), BOT, now + 1, self.login_reply,
                                                      msgtype="m.notice"))
        elif self.status is not None:
            st = {"type": "com.beeper.message_send_status", "event_id": self._eid(), "sender": BOT,
                  "origin_server_ts": now + 2,
                  "content": {"m.relates_to": {"rel_type": "m.reference", "event_id": eid},
                              "status": self.status, "message": self.status_message}}
            self.timeline[room].append(st)
        return eid

    def redact(self, room, event_id, txn):
        self.calls.append(("redact", room, event_id))
        if self.send_exc:
            raise self.send_exc
        eid = self._eid()
        if self.status is not None:
            self.timeline.setdefault(room, []).append(
                {"type": "com.beeper.message_send_status", "event_id": self._eid(), "sender": BOT,
                 "origin_server_ts": T_NEW + self.next_id,
                 "content": {"m.relates_to": {"rel_type": "m.reference", "event_id": eid}, "status": self.status}})
        return eid

    def receipt(self, room, event_id):
        self.calls.append(("receipt", room, event_id))

    def join(self, room):
        self.calls.append(("join", room))

    def upload(self, data, mimetype, filename):
        self.calls.append(("upload", mimetype, filename))
        return "mxc://example.org/abc"

    def close(self):
        pass

    def writes(self, include_mgmt=False):
        out = []
        for c in self.calls:
            if c[0] in ("send_event", "redact", "receipt", "join", "upload"):
                if c[0] == "send_event" and c[1] == MGMT and not include_mgmt:
                    continue
                out.append(c)
        return out


class FakeGate:
    class GateDenied(Exception):
        pass

    class Approvals:
        @classmethod
        def from_env(cls, **kw):
            return cls()

    class OutboundPayload:
        def __init__(self, channel, **kw):
            self.channel, self.kw = channel, kw

    def __init__(self, allow=True):
        self.allow, self.finished, self.begun, self.payloads = allow, [], 0, []

    def evaluate(self, payload, approvals, dry_run=False, source=""):
        self.payloads.append(payload)
        gate = self

        class V:
            allow = gate.allow

            def deny_line(self):
                return "NOT SENT: outbound gate refused."
        return V()

    def begin(self, payload, approvals):
        self.begun += 1
        return "op-1"

    def finish(self, op, status, receipts=()):
        self.finished.append((op, status, list(receipts)))


class FakeComms:
    def __init__(self):
        self.rows = []

    def log_event(self, *a, **kw):
        self.rows.append((a, kw))
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mx = FakeMatrix()
        self.gate = FakeGate()
        self.comms = FakeComms()
        self.money = []
        self.conf = {"instagram.bot_user": BOT, "instagram.homeserver": "https://matrix.example.org",
                     "instagram.min_send_gap_seconds": 0}   # Pacing sets its own
        session = os.path.join(self.tmp, "session.json")
        with open(session, "w") as fh:
            json.dump({"token": "tok-FAKE", "mgmt_room": MGMT}, fh)
        mods = {"outbound_gate": self.gate, "commslib": self.comms}
        patches = [
            mock.patch.object(IG, "make_client", lambda token=None: self.mx),
            mock.patch.object(IG, "_comms_module", lambda name: mods.get(name)),
            mock.patch.object(IG, "_money_hits", lambda text: list(self.money)),
            mock.patch.object(IG, "_conf", lambda key, default=None: self.conf.get(key, default)),
            mock.patch.object(IG, "POLL_S", 0.0),
            mock.patch.object(IG, "STATUS_WAIT_S", 0.05),
            mock.patch.object(IG, "BOT_REPLY_WAIT_S", 0.05),
            mock.patch.object(IG, "LOGIN_WAIT_S", 0.05),
            mock.patch.dict(os.environ, {"INSTAGRAM_MAUTRIX_SESSION": session,
                                         "INSTAGRAM_MAUTRIX_STATE": os.path.join(self.tmp, "state")}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                IG.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    def send(self, *extra, text="Yes, Friday works.", since=str(T_OLD // 1000), chat=DM):
        args = ["send", chat, text]
        if since:
            args += ["--since", since]
        return self.run_cli(*args, *extra)


class SendGuards(Base):
    def test_dry_run_makes_no_write_and_archives_nothing(self):
        code, out, _ = self.send()
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn("Yes, Friday works.", out)
        self.assertEqual(self.mx.writes(), [])
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.begun, 0)

    def test_yes_without_since_exits_2(self):
        code, _, err = self.send("--yes", since=None)
        self.assertEqual(code, 2)
        self.assertIn("--since", err)
        self.assertEqual(self.mx.writes(), [])

    def test_newer_message_from_the_other_person_exits_5(self):
        self.mx.timeline[DM].append(text_event("$a3", ALEX, T_NEW, "Actually wait"))
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 5)
        self.assertEqual(self.mx.writes(), [])

    def test_our_own_newer_messages_do_not_block(self):
        self.mx.timeline[DM].append(text_event("$m2", ME, T_NEW, "from this tool"))
        self.mx.timeline[DM].append(text_event("$m3", OWN_GHOST, T_NEW + 1, "from my phone"))
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("Sent.", out)

    def test_since_accepts_iso_and_ms_and_refuses_junk(self):
        self.assertIsNotNone(IG.parse_ts("2026-09-21T10:00:00Z"))
        self.assertEqual(IG.parse_ts("2026-09-21T10:00:00Z"), IG.parse_ts("2026-09-21T10:00:00+00:00"))
        self.assertEqual(IG.parse_ts(str(T_OLD)), T_OLD / 1000)
        self.assertEqual(IG.parse_ts(str(T_OLD // 1000)), float(T_OLD // 1000))
        self.assertIsNone(IG.parse_ts("friday"))
        code, _, _ = self.send("--yes", since="friday")
        self.assertEqual(code, 2)

    def test_empty_text_is_refused(self):
        code, _, _ = self.send("--yes", text="   ")
        self.assertEqual(code, 1)
        self.assertEqual(self.mx.writes(), [])

    def test_money_lock_exits_6_without_money_ok(self):
        self.money = ["$40"]
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 6)
        self.assertEqual(self.mx.writes(), [])
        code, out, _ = self.send("--yes", "--money-ok")
        self.assertEqual(code, 0, out)

    def test_money_check_that_cannot_run_locks(self):
        with mock.patch.object(IG, "_money_hits", lambda text: None):
            code, _, _ = self.send("--yes")
        self.assertEqual(code, 6)

    def test_gate_refusal_sends_nothing(self):
        self.gate.allow = False
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 7)
        self.assertEqual(self.mx.writes(), [])

    def test_gate_sees_the_chat_name_and_the_text(self):
        self.send()
        p = self.gate.payloads[-1]
        self.assertEqual(p.channel, "instagram")
        self.assertEqual(p.kw["texts"], ["Yes, Friday works."])
        self.assertIn("Alex Doe", " ".join(p.kw.get("labels") or []) + p.kw["target"])


class BridgeHealth(Base):
    def assert_refused(self, reply, word):
        self.mx.login_reply = reply
        code, out, err = self.send("--yes")
        self.assertEqual(code, 3, out + err)
        self.assertIn(word, out + err)
        self.assertEqual(self.mx.writes(), [])

    def test_bad_credentials_refuses(self):
        self.assert_refused("* `1111` (owner.example) - `BAD_CREDENTIALS`", "BAD_CREDENTIALS")

    def test_transient_disconnect_refuses(self):
        self.assert_refused("* `1111` (owner.example) - `TRANSIENT_DISCONNECT`", "TRANSIENT_DISCONNECT")

    def test_no_login_refuses(self):
        self.assert_refused("You're not logged in", "login")

    def test_no_bot_answer_refuses(self):
        self.mx.login_reply = None
        code, out, err = self.send("--yes")
        self.assertEqual(code, 3)
        self.assertEqual(self.mx.writes(), [])

    def test_dry_run_reports_the_state_but_does_not_need_it(self):
        self.mx.login_reply = "* `1111` (owner.example) - `BAD_CREDENTIALS`"
        code, out, _ = self.send()
        self.assertEqual(code, 0)
        self.assertIn("BAD_CREDENTIALS", out)


class SendResults(Base):
    def test_success_is_sent_with_one_archive_row(self):
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("Sent.", out)
        self.assertEqual(len(self.comms.rows), 1)
        args, kw = self.comms.rows[0]
        self.assertEqual(args[0], "instagram")
        self.assertIs(kw["hook_covers"], False)
        self.assertEqual(self.gate.finished[-1][1], "sent")
        sends = [c for c in self.mx.writes() if c[0] == "send_event"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][1], DM)          # failure mode 12: case kept

    def test_ordinary_failure_is_not_sent_and_sets_no_hold(self):
        self.mx.status, self.mx.status_message = "FAIL_PERMANENT", "File too large"
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 1)
        self.assertIn("File too large", out)
        self.assertEqual(self.comms.rows, [])
        self.mx.status = "SUCCESS"
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 0, out)

    def test_restriction_failure_sets_a_hold_that_blocks_the_next_send(self):
        self.mx.status, self.mx.status_message = "FAIL_PERMANENT", "feedback_required: try again later"
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 1)
        self.assertIn("HOLD", out)
        self.mx.status = "SUCCESS"
        before = len(self.mx.writes())
        code, out, err = self.send("--yes")
        self.assertEqual(code, 3)
        self.assertEqual(len(self.mx.writes()), before)
        code, out, _ = self.run_cli("clear-hold")
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        code, out, _ = self.run_cli("clear-hold", "--yes")
        self.assertEqual(code, 0)
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 0, out)

    def test_no_status_in_time_is_uncertain_and_not_retried(self):
        self.mx.status = None
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 4)
        self.assertIn("UNCERTAIN", out)
        self.assertEqual(len([c for c in self.mx.writes() if c[0] == "send_event"]), 1)
        self.assertEqual(self.gate.finished[-1][1], "uncertain")
        self.assertEqual(self.comms.rows, [])

    def test_transport_timeout_is_uncertain(self):
        self.mx.send_exc = IG.UncertainError("no answer")
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 4)

    def test_clean_http_refusal_is_not_sent(self):
        self.mx.send_exc = IG.ApiError("HTTP 403 forbidden")
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 1)
        self.assertIn("NOT SENT", out)
        self.assertEqual(self.gate.finished[-1][1], "failed_before_send")


class Pacing(Base):
    def test_min_gap_between_writes_to_different_chats(self):
        self.conf["instagram.min_send_gap_seconds"] = 3600
        code, out, _ = self.send("--yes")
        self.assertEqual(code, 0, out)
        code, out, err = self.send("--yes", chat=GROUP)
        self.assertEqual(code, 3)
        self.assertIn("gap", (out + err).lower())

    def test_a_few_quick_bubbles_to_the_same_chat_pass_but_a_fourth_is_held(self):
        # People send 2 or 3 bubbles in a row. A bot sends many, or to many chats.
        self.conf["instagram.min_send_gap_seconds"] = 3600
        for n in range(3):
            code, out, err = self.send("--yes", text="part %d" % n)
            self.assertEqual(code, 0, out + err)
        code, out, err = self.send("--yes", text="part 4")
        self.assertEqual(code, 3)
        self.assertIn("gap", (out + err).lower())

    def test_daily_cap(self):
        self.conf["instagram.min_send_gap_seconds"] = 0
        self.conf["instagram.daily_send_cap"] = 2
        self.assertEqual(self.send("--yes")[0], 0)
        self.assertEqual(self.send("--yes")[0], 0)
        code, out, err = self.send("--yes")
        self.assertEqual(code, 3)
        self.assertIn("cap", (out + err).lower())


class Targets(Base):
    def test_name_resolves_to_the_one_portal(self):
        code, out, _ = self.send(chat="alex doe")
        self.assertEqual(code, 0, out)
        self.assertIn(DM, out)

    def test_ambiguous_name_exits_2_and_lists_both(self):
        self.mx.state[GROUP][1] = name_state("Alex Doe and friends")
        code, out, err = self.send(chat="alex")
        self.assertEqual(code, 2)
        self.assertIn(DM, out + err)
        self.assertIn(GROUP, out + err)

    def test_non_portal_rooms_are_refused(self):
        for room in (MGMT, WA):
            code, out, err = self.send("--yes", chat=room)
            self.assertEqual(code, 1, room)
            self.assertIn("not an Instagram chat", out + err)
        self.assertEqual(self.mx.writes(), [])

    def test_state_changed_inside_the_timeline_wins(self):
        self.mx.timeline[DM].append(name_state("Alex Renamed") | {"event_id": "$n1", "origin_server_ts": T_OLD - 1})
        code, out, _ = self.run_cli("chats")
        self.assertIn("Alex Renamed", out)

    def test_invites_from_the_bot_only_are_joined(self):
        self.mx.invites = {"!FromBot:example.org": [member(ME, "Owner", "invite") | {"sender": BOT}],
                           "!FromSomeone:example.org": [member(ME, "Owner", "invite") | {"sender": "@x:example.org"}]}
        self.run_cli("chats")
        joins = [c[1] for c in self.mx.calls if c[0] == "join"]
        self.assertEqual(joins, ["!FromBot:example.org"])


class OtherWrites(Base):
    def test_reply_pins_the_message_and_needs_it_in_that_chat(self):
        code, out, _ = self.run_cli("reply", DM, "$a2", "Yes!", "--since", str(T_OLD // 1000), "--yes")
        self.assertEqual(code, 0, out)
        sent = [c for c in self.mx.calls if c[0] == "send_event" and c[1] == DM][-1][3]
        self.assertEqual(sent["m.relates_to"]["m.in_reply_to"]["event_id"], "$a2")
        code, out, _ = self.run_cli("reply", DM, "$g1", "Yes!", "--since", str(T_OLD // 1000), "--yes")
        self.assertEqual(code, 1)

    def test_react_locks_on_a_money_message(self):
        self.mx.timeline[DM].append(text_event("$a4", ALEX, T_OLD, "Can you send the $40?"))
        with mock.patch.object(IG, "_money_hits", lambda text: ["$40"] if "$" in (text or "") else []):
            code, out, _ = self.run_cli("react", DM, "$a4", "👍", "--yes")
        self.assertEqual(code, 6)
        code, out, _ = self.run_cli("react", DM, "$a2", "👍", "--yes")
        self.assertEqual(code, 0, out)
        sent = [c for c in self.mx.calls if c[0] == "send_event" and c[1] == DM][-1]
        self.assertEqual(sent[2], "m.reaction")
        self.assertEqual(sent[3]["m.relates_to"], {"rel_type": "m.annotation", "event_id": "$a2", "key": "👍"})

    def test_edit_and_delete_only_our_own_messages(self):
        code, out, err = self.run_cli("edit", DM, "$a2", "changed", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("not yours", out + err)
        code, out, _ = self.run_cli("delete", DM, "$a2", "--yes")
        self.assertEqual(code, 1)
        code, out, _ = self.run_cli("edit", DM, "$m1", "Hello again", "--yes")
        self.assertEqual(code, 0, out)
        sent = [c for c in self.mx.calls if c[0] == "send_event" and c[1] == DM][-1][3]
        self.assertEqual(sent["m.relates_to"], {"rel_type": "m.replace", "event_id": "$m1"})
        self.assertEqual(sent["m.new_content"]["body"], "Hello again")
        code, out, _ = self.run_cli("delete", DM, "$m1", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn(("redact", DM, "$m1"), self.mx.calls)

    def test_mark_read_dry_run_then_yes(self):
        code, out, _ = self.run_cli("mark-read", DM)
        self.assertEqual(code, 0)
        self.assertEqual(self.mx.writes(), [])
        code, out, _ = self.run_cli("mark-read", DM, "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn(("receipt", DM, "$a2"), self.mx.calls)

    def test_send_media_uploads_then_sends_an_image(self):
        path = os.path.join(self.tmp, "photo.jpg")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xd8\xff fake jpeg")
        code, out, _ = self.run_cli("send-media", DM, path, "--since", str(T_OLD // 1000))
        self.assertEqual(code, 0)
        self.assertEqual(self.mx.writes(), [])
        code, out, _ = self.run_cli("send-media", DM, path, "--since", str(T_OLD // 1000), "--yes")
        self.assertEqual(code, 0, out)
        sent = [c for c in self.mx.calls if c[0] == "send_event" and c[1] == DM][-1][3]
        self.assertEqual(sent["msgtype"], "m.image")
        self.assertEqual(sent["url"], "mxc://example.org/abc")


class Reads(Base):
    def test_chats_lists_only_instagram_portals(self):
        code, out, _ = self.run_cli("chats")
        self.assertEqual(code, 0)
        self.assertIn(DM, out)
        self.assertIn(GROUP, out)
        self.assertNotIn(WA, out)
        self.assertNotIn(MGMT, out)
        self.assertIn("group", out)

    def test_history_is_oldest_first_and_sends_no_receipt(self):
        code, out, _ = self.run_cli("history", DM)
        self.assertEqual(code, 0)
        self.assertLess(out.index("Hi there"), out.index("Are we still on"))
        self.assertIn("--since", out)
        self.assertEqual(self.mx.writes(include_mgmt=True), [])

    def test_the_since_hint_from_history_lets_the_next_send_through(self):
        # Real Matrix timestamps carry milliseconds. A hint rounded down to whole seconds would be older
        # than the newest message, so the send it suggests would always stop with exit 5.
        self.mx.timeline[DM].append(text_event("$a9", ALEX, T_OLD + 60_123, "One more thing"))
        code, out, _ = self.run_cli("history", DM)
        self.assertEqual(code, 0)
        hint = out.rsplit("--since ", 1)[1].split()[0]
        code, out, err = self.send("--yes", since=hint)
        self.assertEqual(code, 0, out + err)

    def test_chats_json_lists_portals_with_unread_and_kind(self):
        code, out, _ = self.run_cli("chats", "--json")
        self.assertEqual(code, 0)
        rows = {r["chat"]: r for r in json.loads(out)}
        self.assertEqual(set(rows), {DM, GROUP})
        self.assertEqual(rows[DM]["unread"], 1)
        self.assertFalse(rows[DM]["group"])
        self.assertTrue(rows[GROUP]["group"])
        self.assertEqual(rows[DM]["name"], "Alex Doe")
        self.assertEqual(rows[GROUP]["last_ts"], T_OLD)

    def test_history_json_is_oldest_first_marks_mine_and_writes_nothing(self):
        code, out, _ = self.run_cli("history", DM, "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual([r["id"] for r in rows], ["$a1", "$m1", "$a2"])
        self.assertEqual([r["me"] for r in rows], [False, True, False])
        self.assertEqual(rows[-1]["ts"], T_OLD)
        self.assertEqual(rows[-1]["text"], "Are we still on for Friday?")
        self.assertEqual(self.mx.writes(include_mgmt=True), [])

    def test_unread_shows_the_unread_chat(self):
        code, out, _ = self.run_cli("unread")
        self.assertEqual(code, 0)
        self.assertIn("Alex Doe", out)
        self.assertNotIn("Weekend plans", out)
        self.assertEqual([c for c in self.mx.calls if c[0] == "receipt"], [])

    def test_find_searches_text(self):
        code, out, _ = self.run_cli("find", "friday")
        self.assertEqual(code, 0)
        self.assertIn("Are we still on for Friday?", out)
        self.assertNotIn("snacks", out)

    def test_download_writes_the_file(self):
        self.mx.timeline[DM].append({"type": "m.room.message", "event_id": "$img", "sender": ALEX,
                                     "origin_server_ts": T_OLD, "content": {"msgtype": "m.image", "body": "pic.jpg",
                                                                            "url": "mxc://example.org/pic"}})
        code, out, _ = self.run_cli("download", DM, "$img", "--out", self.tmp)
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.tmp, "pic.jpg"), "rb") as fh:
            self.assertEqual(fh.read(), b"IMAGEBYTES")


class Login(Base):
    """Bot phrases come from mautrix-go v0.32.0 bridgev2/commands/login.go and the Instagram connector
    (pkg/igconnector/login_native.go): "Please enter your <field name>", "Logged in as <name> (<id>)"."""

    def script(self, replies):
        """The bot answers each message we send with the next reply (None = no answer)."""
        replies = iter(replies)
        self.sent_bodies = []

        def fake_send(room, etype, content, txn):
            self.sent_bodies.append(content["body"])
            n = len(self.sent_bodies)
            eid = "$l%d" % n
            self.mx.timeline[MGMT].append({"type": etype, "event_id": eid, "sender": ME,
                                           "origin_server_ts": T_NEW + n, "content": content})
            reply = next(replies, None)
            if reply:
                self.mx.timeline[MGMT].append(text_event(eid + "r", BOT, T_NEW + n + 0.5, reply, msgtype="m.notice"))
            return eid
        self.mx.send_event = fake_send

    def test_password_is_hidden_the_code_is_asked_and_login_stops_on_success(self):
        self.script(["Please enter your Email or username", "Please enter your Password",
                     "Please enter your Verification code", "Logged in as Owner (1111)"])
        hidden, asked = [], []
        with mock.patch.object(IG, "_ask", lambda prompt: asked.append(prompt) or
                               ("owner.example" if "username" in prompt else "123456")), \
                mock.patch.object(IG, "_ask_hidden", lambda prompt: hidden.append(prompt) or "s3cret-pass"):
            code, out, err = self.run_cli("login")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.sent_bodies, ["login android", "owner.example", "s3cret-pass", "123456"])
        self.assertEqual(len(hidden), 1)
        self.assertIn("Password", hidden[0])
        self.assertNotIn("s3cret-pass", out + err)
        self.assertIn("Logged in", out)

    def test_a_failure_line_stops_and_cancels(self):
        for line in ("Login failed: bad password", "Invalid value for Password: too short",
                     "Failed to submit input: checkpoint_required"):
            with self.subTest(line=line):
                self.script(["Please enter your Email or username", line])
                with mock.patch.object(IG, "_ask", lambda prompt: "owner.example"):
                    code, out, _ = self.run_cli("login")
                self.assertEqual(code, 1)
                self.assertIn(line.split(":")[0], out)
                self.assertEqual(self.sent_bodies[-1], "cancel")

    def test_a_browser_check_cancels_and_says_to_approve_in_the_app(self):
        self.script(["Instagram needs you to finish a verification step. Complete it in the browser (you may "
                     "need to sign in again), then your login will continue here.\nLogin URL: <https://www.instagram.com/challenge/x>"])
        code, out, _ = self.run_cli("login")
        self.assertEqual(code, 1)
        self.assertIn("Instagram app", out)
        self.assertEqual(self.sent_bodies[-1], "cancel")

    def test_no_answer_stops_without_hanging(self):
        self.script([None])
        code, out, _ = self.run_cli("login")
        self.assertEqual(code, 1)
        self.assertIn("No answer from the bridge", out)


class TunnelRoute(unittest.TestCase):
    """instagram.tunnel_homeserver names a local tunnel to the bridge box, for a network that drops or intercepts
    the direct route. Failures: the tunnel is open and the tool still takes the slow direct route; the tunnel is
    closed and the tool does not go back; no key set and the tool takes a detour anyway."""

    def homeserver(self, listen, key=True):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        url = "http://127.0.0.1:%d" % sock.getsockname()[1]
        conf = {"instagram.homeserver": "https://matrix.example.com"}
        if key:
            conf["instagram.tunnel_homeserver"] = url
        try:
            if listen:
                sock.listen()
            with mock.patch.object(IG, "_conf", lambda k, default=None: conf.get(k, default)):
                return IG._homeserver(), url
        finally:
            sock.close()

    def test_an_open_tunnel_carries_the_homeserver(self):
        got, url = self.homeserver(True)
        self.assertEqual(got, url)

    def test_a_closed_tunnel_is_the_configured_route(self):
        self.assertEqual(self.homeserver(False)[0], "https://matrix.example.com")

    def test_no_tunnel_key_is_the_configured_route(self):
        self.assertEqual(self.homeserver(True, key=False)[0], "https://matrix.example.com")


if __name__ == "__main__":
    unittest.main()
