#!/usr/bin/env python3
"""Tests for reader_target(): a Matrix room id must reach the WhatsApp reader
in its ORIGINAL case.

norm() lowercases `!room` ids so they key stably, but Matrix room ids are
case-sensitive. `comms unread` passed the lowercased key to the bridge, which
answered M_NOT_FOUND, so every room-id-keyed WhatsApp chat (29 of 82 on
2026-10-05) silently read as empty. Sam's signed SOW was missed this way.

Stdlib only. Run:

    python3 test_room_case.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import commslib as C  # noqa: E402

REAL = "!fakeiK2ZWeqhFWCEPyYngFb51yBMWXaSCrUZoL8g5ub"
KEY = REAL.lower()


def _store(rows_by_month):
    d = tempfile.mkdtemp(prefix="comms-room-case-")
    for month, rows in rows_by_month.items():
        with open(os.path.join(d, month + ".jsonl"), "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    return d


class ReaderTarget(unittest.TestCase):

    def test_room_id_recovers_original_case_from_the_archive(self):
        d = _store({"2026-10": [
            {"channel": "whatsapp", "target": KEY, "raw_target": REAL}]})
        thread = {"channel": "whatsapp", "target": KEY}
        self.assertEqual(C.reader_target(thread, store=d), REAL)

    def test_a_raw_target_on_the_thread_wins_without_reading_the_archive(self):
        thread = {"channel": "whatsapp", "target": KEY, "raw_target": REAL}
        self.assertEqual(C.reader_target(thread, store="/nonexistent"), REAL)

    def test_a_name_used_as_raw_target_is_not_taken_for_the_room_id(self):
        # The same room is also read by NAME ("Acme Whatsapp Group"); those
        # rows key on the name, so they must never answer for the room id.
        d = _store({"2026-10": [
            {"channel": "whatsapp", "target": "acme whatsapp group",
             "raw_target": "Acme Whatsapp Group"},
            {"channel": "whatsapp", "target": KEY, "raw_target": REAL}]})
        thread = {"channel": "whatsapp", "target": KEY}
        self.assertEqual(C.reader_target(thread, store=d), REAL)

    def test_newest_month_is_searched_first(self):
        # Rows written before raw_target existed carry only the lowercase key.
        d = _store({
            "2026-09": [{"channel": "whatsapp", "target": KEY}],
            "2026-10": [{"channel": "whatsapp", "target": KEY,
                         "raw_target": REAL}]})
        thread = {"channel": "whatsapp", "target": KEY}
        self.assertEqual(C.reader_target(thread, store=d), REAL)

    def test_no_archived_original_falls_back_to_the_key(self):
        d = _store({"2026-10": [{"channel": "whatsapp", "target": KEY}]})
        thread = {"channel": "whatsapp", "target": KEY}
        self.assertEqual(C.reader_target(thread, store=d), KEY)

    def test_non_room_targets_pass_through_unchanged(self):
        for t in ("12065551234", "120363000000000001@g.example.com",
                  "sam roe (wa)"):
            with self.subTest(target=t):
                self.assertEqual(
                    C.reader_target({"channel": "whatsapp", "target": t},
                                    store="/nonexistent"), t)

    def test_a_corrupt_archive_line_does_not_sink_the_lookup(self):
        d = _store({})
        with open(os.path.join(d, "2026-10.jsonl"), "w") as fh:
            fh.write('{"channel": "whatsapp", "target": "%s", "raw_tar\n' % KEY)
            fh.write(json.dumps({"channel": "whatsapp", "target": KEY,
                                 "raw_target": REAL}) + "\n")
        thread = {"channel": "whatsapp", "target": KEY}
        self.assertEqual(C.reader_target(thread, store=d), REAL)


class AliasAndDedupe(unittest.TestCase):
    """A DM can be keyed by its `<lid>@lid` jid, which the bridge cannot open.
    The route that owns that alias names the room, so the reader opens the
    room, and a room reached two ways is read once."""

    ROUTES = {KEY: {"aliases": ["1234567890123@lid"], "project": "proj_tar"}}

    def test_lid_alias_resolves_to_its_routed_room_in_original_case(self):
        d = _store({"2026-10": [
            {"channel": "whatsapp", "target": KEY, "raw_target": REAL}]})
        thread = {"channel": "whatsapp", "target": "1234567890123@lid"}
        self.assertEqual(
            C.reader_target(thread, store=d, routes=self.ROUTES), REAL)

    def test_lid_with_no_route_passes_through(self):
        thread = {"channel": "whatsapp", "target": "123@lid"}
        self.assertEqual(
            C.reader_target(thread, store="/nonexistent", routes=self.ROUTES),
            "123@lid")

    def test_a_room_reached_by_key_and_by_alias_is_read_once(self):
        d = _store({"2026-10": [
            {"channel": "whatsapp", "target": KEY, "raw_target": REAL}]})
        rooms = [{"channel": "whatsapp", "target": "1234567890123@lid"},
                 {"channel": "whatsapp", "target": KEY},
                 {"channel": "imessage", "target": "chat42"}]
        got = C.reader_rooms(rooms, store=d, routes=self.ROUTES)
        self.assertEqual([(r["target"], t) for r, t in got],
                         [("1234567890123@lid", REAL), ("chat42", "chat42")])


class NameKeyedChats(unittest.TestCase):
    """A chat read by NAME keys on the name, but the tool printed the room it
    opened (`[room !…]`) in that row's body. The reader opens that room, so a
    group saved under its room id and under two names is read once."""

    TEAM = "!fake9382dffx1kVZQ2tqMnMcLRkBOzZU3G8xI7CGr5c"

    def _d(self):
        body = 'History for GROUP Deli Team (2 messages) [room %s]:' % self.TEAM
        return _store({"2026-10": [
            {"channel": "whatsapp", "target": "deli team",
             "raw_target": "Deli Team", "body": body},
            {"channel": "whatsapp", "target": self.TEAM.lower(),
             "raw_target": self.TEAM, "body": body}]})

    def test_a_name_resolves_to_the_room_it_last_opened(self):
        thread = {"channel": "whatsapp", "target": "deli team"}
        self.assertEqual(C.reader_target(thread, store=self._d()), self.TEAM)

    def test_room_id_and_name_records_are_read_once(self):
        rooms = [{"channel": "whatsapp", "target": self.TEAM.lower()},
                 {"channel": "whatsapp", "target": "deli team"}]
        got = C.reader_rooms(rooms, store=self._d())
        self.assertEqual([t for _r, t in got], [self.TEAM])

    def test_a_name_never_seen_opening_a_room_passes_through(self):
        thread = {"channel": "whatsapp", "target": "someone new"}
        self.assertEqual(C.reader_target(thread, store=self._d()),
                         "someone new")

    def test_other_channels_never_borrow_a_whatsapp_room(self):
        thread = {"channel": "imessage", "target": "deli team"}
        self.assertEqual(C.reader_target(thread, store=self._d()), "deli team")


if __name__ == "__main__":
    unittest.main()
