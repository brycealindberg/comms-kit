#!/usr/bin/env python3
"""Tests for slack_feed_messages(): the live Slack read `comms unread` uses.

The bridge buffer holds top-level messages only, and on 2026-10-26 it held none for
a client's DMs or a channel, so `comms unread` showed 3 messages where Slack had
replies and DMs waiting. `slack_bridge.py threads CHANNEL --json` reads Slack live,
replies included; this parses what it prints.

Stdlib only (unittest), to match commslib.py. Run:

    python3 test_slack_live.py
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import commslib as C  # noqa: E402


def feed(messages):
    return json.dumps({"channel": "C0FAKECH001", "workspace": "acmegroup",
                       "messages": messages})


class SlackFeedMessages(unittest.TestCase):

    def test_a_reply_is_kept_marked_and_sorted_by_its_own_time(self):
        # The reply (ts 300) is newer than the next top-level message (ts 200), so in
        # a newest-first feed it must land above it, not hide under its parent.
        out = C.slack_feed_messages(feed([
            {"ts": "100.1", "name": "Pat", "text": "Automations are done"},
            {"ts": "300.1", "name": "Jennifer", "text": "Can you open the calendar?",
             "thread_ts": "100.1"},
            {"ts": "200.1", "name": "Robin", "text": "Calendar is open!"},
        ]))
        self.assertEqual([m["ts"] for m in out], ["100.1", "200.1", "300.1"])
        self.assertEqual(out[2]["sender"], "Jennifer")
        self.assertTrue(out[2]["text"].startswith(C.SLACK_REPLY_MARK), out[2]["text"])
        self.assertFalse(out[0]["text"].startswith(C.SLACK_REPLY_MARK))

    def test_records_built_from_it_carry_the_reply_into_the_merged_feed(self):
        msgs = C.slack_feed_messages(feed([
            {"ts": "100.1", "name": "Pat", "text": "top"},
            {"ts": "101.1", "name": "Jo", "text": "in the thread", "thread_ts": "100.1"},
        ]))
        recs = C.slack_records(msgs, 30)
        self.assertEqual([r["who"] for r in recs], ["Pat", "Jo"])
        self.assertIn("in the thread", recs[1]["text"])
        self.assertAlmostEqual(recs[1]["ts"], 101.1, places=3)

    def test_a_file_with_no_words_reads_as_an_attachment(self):
        out = C.slack_feed_messages(feed([
            {"ts": "100.1", "name": "Jo", "text": "", "files": 2}]))
        self.assertEqual(C.slack_records(out, 5)[0]["text"], "[attachment]")

    def test_a_quiet_channel_is_an_empty_list_not_a_failed_read(self):
        # None would make the caller say "live read failed" about a channel that is
        # simply quiet.
        self.assertEqual(C.slack_feed_messages(feed([])), [])

    def test_the_json_is_read_from_the_last_line_after_any_banner(self):
        out = C.slack_feed_messages(
            "Warning: Permanently added host\n" + feed([{"ts": "1.0", "name": "A", "text": "x"}]) + "\n")
        self.assertEqual(len(out), 1)

    def test_output_that_is_not_a_feed_is_none_so_the_caller_falls_back(self):
        for bad in ("", "   \n", "threads: no answer from vps", "[1, 2]",
                    json.dumps({"channel": "C1"}), json.dumps({"messages": "nope"})):
            self.assertIsNone(C.slack_feed_messages(bad), repr(bad))

    def test_an_entry_that_is_not_an_object_is_skipped_not_fatal(self):
        out = C.slack_feed_messages(feed(["junk", {"ts": "5.0", "name": "B", "text": "ok"}]))
        self.assertEqual([m["sender"] for m in out], ["B"])


if __name__ == "__main__":
    unittest.main()
