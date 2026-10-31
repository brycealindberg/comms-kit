#!/usr/bin/env python3
"""Tests for cwd->project scoping — the backbone of `comms here` / `comms unread`.

Stdlib only (unittest), to match commslib.py. Run:

    python3 test_scoping.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

COMMS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),  # kit: code path
                     "bin", "comms")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import commslib as C  # noqa: E402

LV = "/Users/you/Desktop/projects/acme-vortex"


class ResolveProject(unittest.TestCase):
    """resolve_project(cwd, repos) -> the project whose repo is cwd or its
    nearest ancestor; None when nothing owns this folder."""

    def test_exact_match(self):
        self.assertEqual(C.resolve_project(LV, {LV: "acme_vortex"}),
                         "acme_vortex")

    def test_subdir_match(self):
        self.assertEqual(
            C.resolve_project(LV + "/acme_vortex_backend", {LV: "acme_vortex"}),
            "acme_vortex")

    def test_no_match_returns_none(self):
        self.assertIsNone(
            C.resolve_project("/Users/you/Desktop/nowhere", {LV: "acme_vortex"}))

    def test_sibling_prefix_not_matched(self):
        # The string-prefix trap: a sibling folder that merely STARTS with the
        # repo path must not match. Boundary is the path separator, not chars.
        self.assertIsNone(
            C.resolve_project(LV + "-other", {LV: "acme_vortex"}))

    def test_longest_prefix_wins(self):
        repos = {"/Users/you/Desktop/projects": "misc",
                 LV: "acme_vortex"}
        self.assertEqual(C.resolve_project(LV + "/x", repos), "acme_vortex")

    def test_trailing_slash_normalized(self):
        self.assertEqual(C.resolve_project(LV + "/", {LV: "acme_vortex"}),
                         "acme_vortex")

    def test_empty_repos_is_none(self):
        self.assertIsNone(C.resolve_project(LV, {}))


THREADS_FIX = {
    "!room1": {"channel": "whatsapp", "label": "Alex WA",
               "project": "northwind", "target": "!room1", "count": 32,
               "last_ts": 100.0},
    "handle2": {"channel": "imessage", "label": None,
                "project": "northwind", "target": "+15551234", "count": 4,
                "last_ts": 200.0},
    "!other": {"channel": "whatsapp", "label": "Acme",
               "project": "acme_vortex", "target": "!other", "count": 8,
               "last_ts": 150.0},
    "loose": {"channel": "gmail", "label": None, "project": None,
              "target": "x@y.example.com", "count": 1, "last_ts": 50.0},
}


class CatalogForProject(unittest.TestCase):
    """catalog_for_project(project, threads, channel=None) -> the rooms that
    belong to one project, newest activity first."""

    def test_filters_to_one_project(self):
        rooms = C.catalog_for_project("northwind", THREADS_FIX)
        self.assertEqual({r["target"] for r in rooms}, {"!room1", "+15551234"})

    def test_newest_activity_first(self):
        rooms = C.catalog_for_project("northwind", THREADS_FIX)
        self.assertEqual([r["target"] for r in rooms], ["+15551234", "!room1"])

    def test_channel_filter(self):
        rooms = C.catalog_for_project("northwind", THREADS_FIX, channel="whatsapp")
        self.assertEqual([r["target"] for r in rooms], ["!room1"])

    def test_none_project_is_empty(self):
        self.assertEqual(C.catalog_for_project(None, THREADS_FIX), [])

    def test_unknown_project_is_empty(self):
        self.assertEqual(C.catalog_for_project("ghost", THREADS_FIX), [])


def _rows(*specs):
    out = []
    for project, cwd, n in specs:
        out += [{"project": project, "cwd": cwd}] * n
    return out


class SuggestRepos(unittest.TestCase):
    """suggest_repos(rows, exclude_prefixes) -> (links, conflicts).

    A link is proposed only when one project clearly owns a folder. A folder
    shared by two projects (the real `Alpha App` case: proj_beta
    worked from the same folder as proj_alpha) is a conflict for the owner, never an
    auto-link."""

    ROWS = _rows(
        ("acme_co", "/cp/isaac", 5),     # clean: only acme_co here
        ("northwind", "/cp/northwind", 4),        # clean
        ("beta", "/proj/alpha", 9),          # owns the Alpha folder (dominant)
        ("alpha2", "/proj/alpha", 1),        # same folder, 1 msg -> conflict
        ("sam", "/hub", 8),                # from the hub -> excluded
        ("sam", "/cp/isaac", 2),           # isaac is owned by acme_co
    )
    EXCLUDE = ("/hub",)

    def test_clean_links_only(self):
        links, _ = C.suggest_repos(self.ROWS, self.EXCLUDE)
        self.assertEqual(links, {"/cp/isaac": "acme_co",
                                 "/cp/northwind": "northwind",
                                 "/proj/alpha": "beta"})

    def test_contested_projects_are_conflicts(self):
        _, conflicts = C.suggest_repos(self.ROWS, self.EXCLUDE)
        self.assertEqual({c["project"] for c in conflicts}, {"alpha2", "sam"})

    def test_excluded_prefix_never_becomes_a_link(self):
        links, _ = C.suggest_repos(self.ROWS, self.EXCLUDE)
        self.assertNotIn("/hub", links)

    def test_conflict_names_the_owning_project(self):
        _, conflicts = C.suggest_repos(self.ROWS, self.EXCLUDE)
        alpha2 = next(c for c in conflicts if c["project"] == "alpha2")
        self.assertEqual(alpha2["owner"], "beta")


class ActiveRooms(unittest.TestCase):
    """active_rooms(rooms, days, now) -> rooms touched within the window."""

    NOW = 1_000_000.0
    ROOMS = [
        {"target": "a", "last_ts": NOW - 3600},        # 1h ago
        {"target": "b", "last_ts": NOW - 20 * 86400},  # 20d ago
        {"target": "c", "last_ts": NOW - 5 * 86400},   # 5d ago
        {"target": "d", "last_ts": None},              # never
    ]

    def test_keeps_only_within_window(self):
        got = [r["target"] for r in C.active_rooms(self.ROOMS, 14, self.NOW)]
        self.assertEqual(got, ["a", "c"])

    def test_zero_days_keeps_all(self):
        got = [r["target"] for r in C.active_rooms(self.ROOMS, 0, self.NOW)]
        self.assertEqual(got, ["a", "b", "c", "d"])


class FormatImessage(unittest.TestCase):
    """format_imessage(payload, limit) -> compact '[time] who: text' lines."""

    IM = {"messages": [
        {"is_from_me": False, "sender": "+1555", "sender_name": "John",
         "text": "hi there", "created_at": "2026-09-17 10:00",
         "attachments": []},
        {"is_from_me": True, "sender": "+1206", "sender_name": "",
         "text": "", "created_at": "2026-09-17 10:01",
         "attachments": [{"filename": "x"}]},
    ]}

    def test_formats_me_sender_and_attachment(self):
        self.assertEqual(
            C.format_imessage(self.IM, 5),
            ["[2026-09-17 10:00] John: hi there",
             "[2026-09-17 10:01] Me: [attachment]"])

    def test_limit_keeps_most_recent(self):
        self.assertEqual(C.format_imessage(self.IM, 1),
                         ["[2026-09-17 10:01] Me: [attachment]"])


class ConvertEt(unittest.TestCase):
    """_iso_to_et converts a UTC stamp to labeled local ET, DST-correct.

    Forces TZ=America/New_York so the assertions hold on any machine, not just
    the owner's Mac. A no-op (passthrough) implementation cannot satisfy these."""

    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def test_summer_is_edt_utc_minus_4(self):
        self.assertEqual(C._iso_to_et("2026-09-17T14:00:00Z"),
                         "2026-09-17 10:00 EDT")

    def test_winter_is_est_utc_minus_5(self):
        self.assertEqual(C._iso_to_et("2026-01-15T14:00:00Z"),
                         "2026-01-15 09:00 EST")

    def test_millis_offset_form_parses(self):
        self.assertEqual(C._iso_to_et("2026-09-17T20:26:06.353Z"),
                         "2026-09-17 16:26 EDT")

    def test_label_follows_the_mac_zone(self):
        # The owner travels; macOS moves the zone with him. The label must follow, never say ET.
        os.environ["TZ"] = "Asia/Tokyo"
        time.tzset()
        self.assertEqual(C._iso_to_et("2026-09-17T14:00:00Z"), "2026-09-17 23:00 JST")
        self.assertEqual(C._epoch_to_et(1789653600), "2026-09-17 23:00 JST")
        self.assertEqual(C._rfc2822_to_et("Thu, 17 Sep 2026 14:00:00 +0000"), "2026-09-17 23:00 JST")
        self.assertTrue(C._now()[1].endswith(" JST"), C._now()[1])

    def test_naive_or_garbage_passthrough(self):
        self.assertEqual(C._iso_to_et("2026-09-17 10:00"), "2026-09-17 10:00")
        self.assertEqual(C._iso_to_et("not a date"), "not a date")


class FormatSlack(unittest.TestCase):
    """format_slack(messages, limit) -> '[time ET] sender: text' lines."""

    MSGS = [
        {"ts": "1789675569.733789", "sender": "Alex Doe",
         "text": "hi  there", "isVoiceNote": False, "attachments": []},
        {"ts": "1789675570.0", "sender": "Sam", "text": "",
         "isVoiceNote": True, "attachments": []},
    ]

    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def test_sender_text_collapse_and_voice(self):
        lines = C.format_slack(self.MSGS, 5)
        self.assertTrue(lines[0].endswith("] Alex Doe: hi there"), lines[0])
        self.assertTrue(lines[1].endswith("] Sam: [voice note]"), lines[1])

    def test_timestamp_is_et_labeled(self):
        self.assertRegex(C.format_slack(self.MSGS, 1)[0],
                         r"^\[\d{4}-\d\d-\d\d \d\d:\d\d E[DS]T\] ")

    def test_limit_keeps_last(self):
        self.assertEqual(len(C.format_slack(self.MSGS, 1)), 1)


class ProjectEmails(unittest.TestCase):
    """project_emails(project, routes) -> the client's email addresses."""

    ROUTES = {
        "john@betacorp.example.com": {"project": "beta",
                                          "aliases": ["198", "1a08"]},
        "dana@betacorp.example.com": {"project": "beta", "aliases": ["205"]},
        "200": {"project": "beta", "aliases": []},
        "!room": {"project": "beta", "aliases": ["CEO@betacorp.example.com"]},
        "someone@other.example.com": {"project": "other", "aliases": []},
    }

    def test_collects_emails_for_project_only(self):
        self.assertEqual(
            C.project_emails("beta", self.ROUTES),
            ["ceo@betacorp.example.com", "dana@betacorp.example.com",
             "john@betacorp.example.com"])

    def test_none_project_is_empty(self):
        self.assertEqual(C.project_emails(None, self.ROUTES), [])


class RfcToEt(unittest.TestCase):
    """_rfc2822_to_et converts an email Date header to labeled local ET."""

    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def test_offset_to_et(self):
        # 13:57 -0300 = 16:57 UTC = 12:57 EDT
        self.assertEqual(C._rfc2822_to_et("Tue, 15 Sep 2026 13:57:32 -0300"),
                         "2026-09-15 12:57 EDT")

    def test_garbage_passthrough(self):
        self.assertEqual(C._rfc2822_to_et("not a date"), "not a date")


class _ForceET:
    """Mixin: pin the process to America/New_York so ET assertions and
    naive-local->epoch conversions hold on any machine, not just the owner's Mac."""

    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()


class EpochConverters(_ForceET, unittest.TestCase):
    """The *_to_epoch converters mirror the *_to_et ones but return a sortable
    epoch, or None for input a merged feed must NOT pretend to have timed."""

    def test_iso_to_epoch_roundtrips_through_et(self):
        ep = C._iso_to_epoch("2026-09-17T20:26:06.353Z")
        self.assertIsInstance(ep, float)
        self.assertEqual(C._epoch_to_et(ep), "2026-09-17 16:26 EDT")

    def test_iso_naive_or_garbage_is_none(self):
        # A naive stamp has no zone, so its epoch is ambiguous -> None, not a guess
        self.assertIsNone(C._iso_to_epoch("2026-09-17 10:00"))
        self.assertIsNone(C._iso_to_epoch("not a date"))

    def test_rfc2822_to_epoch_roundtrips_through_et(self):
        ep = C._rfc2822_to_epoch("Tue, 15 Sep 2026 13:57:32 -0300")
        self.assertIsInstance(ep, float)
        self.assertEqual(C._epoch_to_et(ep), "2026-09-15 12:57 EDT")

    def test_rfc2822_garbage_is_none(self):
        self.assertIsNone(C._rfc2822_to_epoch("not a date"))

    def test_wall_et_epoch_roundtrips(self):
        # WhatsApp's printed time is a naive LOCAL wall clock; interpret it as ET
        ep = C._wall_et_to_epoch("2026-09-15 14:03")
        self.assertIsInstance(ep, float)
        self.assertEqual(C._epoch_to_et(ep), "2026-09-15 14:03 EDT")

    def test_wall_et_garbage_is_none(self):
        self.assertIsNone(C._wall_et_to_epoch("nope"))
        self.assertIsNone(C._wall_et_to_epoch(""))


class MergeFeed(_ForceET, unittest.TestCase):
    """merge_feed(records, limit) -> one newest-first list, every row tagged with
    its channel, rendered through the SAME ET path as every other view."""

    def _recs(self):
        return [
            {"ts": C._wall_et_to_epoch("2026-09-17 16:26"), "channel": "imessage",
             "who": "John", "text": "hi", "me": False},
            {"ts": C._wall_et_to_epoch("2026-09-17 15:57"), "channel": "slack",
             "who": "Me", "text": "later", "me": True},
            {"ts": C._wall_et_to_epoch("2026-09-18 09:00"), "channel": "gmail",
             "who": "a@b.example.com", "text": "subject", "me": False},
        ]

    def test_sorted_newest_first_and_tagged(self):
        self.assertEqual(C.merge_feed(self._recs(), 10), [
            "[2026-09-18 09:00 EDT] gmail/a@b.example.com: subject",
            "[2026-09-17 16:26 EDT] imessage/John: hi",
            "[2026-09-17 15:57 EDT] slack/Me: later",
        ])

    def test_limit_keeps_newest(self):
        self.assertEqual(C.merge_feed(self._recs(), 2), [
            "[2026-09-18 09:00 EDT] gmail/a@b.example.com: subject",
            "[2026-09-17 16:26 EDT] imessage/John: hi",
        ])

    def test_multiline_text_collapses_to_one_row(self):
        r = [{"ts": C._wall_et_to_epoch("2026-09-17 16:26"), "channel": "whatsapp",
              "who": "N", "text": "line one\nline  two", "me": False}]
        self.assertEqual(C.merge_feed(r, 5),
                         ["[2026-09-17 16:26 EDT] whatsapp/N: line one line two"])

    def test_bad_ts_sorts_last_with_empty_time_not_a_fake_one(self):
        # "Make nothing loud": a record with no real epoch shows an EMPTY time
        # bracket and sinks to the bottom -- it never borrows a plausible time.
        recs = [{"ts": None, "channel": "whatsapp", "who": "X", "text": "no time"},
                {"ts": C._wall_et_to_epoch("2026-09-17 16:26"), "channel": "slack",
                 "who": "Y", "text": "has time", "me": False}]
        out = C.merge_feed(recs, 5)
        self.assertEqual(out[0], "[2026-09-17 16:26 EDT] slack/Y: has time")
        self.assertEqual(out[1], "[] whatsapp/X: no time")

    def test_empty_is_empty(self):
        self.assertEqual(C.merge_feed([], 5), [])


class ImessageRecords(_ForceET, unittest.TestCase):
    """imessage_records(payload, limit) -> records carrying a real epoch, and the
    grouped format_imessage keeps producing the SAME strings it always did."""

    # Real iMessage created_at is UTC ISO (Z). A naive stamp has no zone and
    # correctly yields ts=None (see test_naive_created_at_has_no_epoch).
    IM_UTC = {"messages": [
        {"is_from_me": False, "sender": "+1555", "sender_name": "John",
         "text": "hi there", "created_at": "2026-09-17T14:00:00Z",
         "attachments": []},
        {"is_from_me": True, "sender_name": "", "text": "",
         "created_at": "2026-09-17T14:01:00Z",
         "attachments": [{"filename": "x"}]},
    ]}

    def test_records_have_epoch_who_text_channel(self):
        recs = C.imessage_records(self.IM_UTC, 5)
        self.assertEqual([r["channel"] for r in recs], ["imessage", "imessage"])
        self.assertEqual(recs[0]["who"], "John")
        self.assertEqual(recs[0]["text"], "hi there")
        self.assertEqual(C._epoch_to_et(recs[0]["ts"]), "2026-09-17 10:00 EDT")
        self.assertTrue(recs[1]["me"])
        self.assertEqual(recs[1]["text"], "[attachment]")

    def test_naive_created_at_has_no_epoch(self):
        # 'Make nothing loud': a zoneless stamp yields ts=None, never a guess.
        recs = C.imessage_records(FormatImessage.IM, 5)
        self.assertIsNone(recs[0]["ts"])

    def test_grouped_formatter_unchanged(self):
        self.assertEqual(
            C.format_imessage(FormatImessage.IM, 5),
            ["[2026-09-17 10:00] John: hi there",
             "[2026-09-17 10:01] Me: [attachment]"])


class SlackRecords(_ForceET, unittest.TestCase):
    """slack_records(messages, limit) -> records with the Slack epoch preserved."""

    MSGS = FormatSlack.MSGS

    def test_records_have_float_ts_and_voice_placeholder(self):
        recs = C.slack_records(self.MSGS, 5)
        self.assertEqual(recs[0]["channel"], "slack")
        self.assertEqual(recs[0]["who"], "Alex Doe")
        self.assertEqual(recs[0]["text"], "hi there")
        self.assertAlmostEqual(recs[0]["ts"], 1789675569.733789, places=3)
        self.assertEqual(recs[1]["text"], "[voice note]")

    def test_grouped_formatter_unchanged(self):
        lines = C.format_slack(self.MSGS, 5)
        self.assertTrue(lines[0].endswith("] Alex Doe: hi there"), lines[0])
        self.assertTrue(lines[1].endswith("] Sam: [voice note]"), lines[1])


class GmailRecord(_ForceET, unittest.TestCase):
    """gmail_record(from, subject, date) -> one record with the Date parsed to
    an epoch; subject is the text so it reads like every other channel."""

    def test_builds_a_record(self):
        r = C.gmail_record("john@x.example.com", "Re: Milestone 1",
                           "Tue, 15 Sep 2026 13:57:32 -0300")
        self.assertEqual(r["channel"], "gmail")
        self.assertEqual(r["who"], "john@x.example.com")
        self.assertEqual(r["text"], "Re: Milestone 1")
        self.assertEqual(C._epoch_to_et(r["ts"]), "2026-09-15 12:57 EDT")


class ParseWhatsappRecords(_ForceET, unittest.TestCase):
    """parse_whatsapp_records(lines) -> (records, notes). WhatsApp has no JSON
    mode, so its printed '[time] who: body' lines are parsed back to records; a
    body's extra lines fold into the message above; an error/sentinel line with
    no message above it becomes a NOTE, never a fake-timed record."""

    def test_parses_messages_with_epoch(self):
        recs, notes = C.parse_whatsapp_records([
            "[2026-09-15 14:03] Alex: first",
            "[2026-09-15 14:05] Me: reply",
        ])
        self.assertEqual(notes, [])
        self.assertEqual([r["who"] for r in recs], ["Alex", "Me"])
        self.assertEqual([r["channel"] for r in recs], ["whatsapp", "whatsapp"])
        self.assertEqual(C._epoch_to_et(recs[0]["ts"]), "2026-09-15 14:03 EDT")

    def test_continuation_line_folds_into_message_above(self):
        recs, notes = C.parse_whatsapp_records([
            "[2026-09-15 14:03] Alex: first line",
            "second line of the same message",
            "[2026-09-15 14:05] Me: reply",
        ])
        self.assertEqual(len(recs), 2)
        self.assertEqual(notes, [])
        self.assertEqual(recs[0]["text"], "first line\nsecond line of the same message")
        self.assertEqual(recs[1]["text"], "reply")

    def test_sentinel_is_a_note_not_a_record(self):
        recs, notes = C.parse_whatsapp_records(["(whatsapp unavailable: bridge down)"])
        self.assertEqual(recs, [])
        self.assertEqual(notes, ["(whatsapp unavailable: bridge down)"])

    def test_nothing_recent_is_a_note(self):
        recs, notes = C.parse_whatsapp_records(["(nothing recent)"])
        self.assertEqual(recs, [])
        self.assertEqual(notes, ["(nothing recent)"])

    def test_empty_body_media_line_is_a_message_not_a_note(self):
        # A media-only / empty WhatsApp message prints as '[ts] who:' with no body
        # (the reader strips the trailing space). It is a real, timestamped
        # message -- it must NOT fall into notes, nor glue onto the one above it.
        recs, notes = C.parse_whatsapp_records(["[2026-08-25 21:34] +lid-299:"])
        self.assertEqual(notes, [])
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["who"], "+lid-299")
        self.assertEqual(recs[0]["text"], "[no text]")
        self.assertEqual(C._epoch_to_et(recs[0]["ts"]), "2026-08-25 21:34 EDT")

    def test_colon_in_body_is_preserved(self):
        recs, _ = C.parse_whatsapp_records(
            ["[2026-09-15 14:03] Alex: link: http://x.com/a:b"])
        self.assertEqual(recs[0]["who"], "Alex")
        self.assertEqual(recs[0]["text"], "link: http://x.com/a:b")


class CliBase(unittest.TestCase):
    """Runs the real `comms` against a throwaway data folder, never the owner's archive."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="comms-scope-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(self.data, "store"))
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w") as fh:
            json.dump({"data_dir": self.data}, fh)

    def comms(self, *args):
        env = {"HOME": self.tmp, "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "COMMS_KIT_CONFIG": self.config}
        return subprocess.run([sys.executable, COMMS, *args], capture_output=True, text=True,
                              env=env, cwd=self.tmp, timeout=60)


class CommsHereCLI(CliBase):
    """`comms here` end-to-end: parses args, loads the maps, prints."""

    def test_unowned_folder_reports_no_project(self):
        out = self.comms("here", "--path", "/tmp/not-a-client-repo-xyz", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIsNone(json.loads(out.stdout)["project"])

    def test_a_linked_folder_reports_its_project(self):
        with open(os.path.join(self.data, "project_repos.json"), "w") as fh:
            json.dump({"/work/acme-app": "acme"}, fh)
        out = self.comms("here", "--path", "/work/acme-app/src", "--json")
        self.assertEqual(json.loads(out.stdout)["project"], "acme")


class CommsLinkSuggestCLI(CliBase):
    """`comms link --suggest` reads the store and proposes clean links."""

    def write_rows(self, rows):
        with open(os.path.join(self.data, "store", "2026-09.jsonl"), "w") as fh:
            for i, (project, cwd) in enumerate(rows):
                fh.write(json.dumps({"ts": 1790000000 + i, "channel": "gmail", "dir": "send",
                                     "target": "a@example.com", "project": project,
                                     "cwd": cwd}) + "\n")

    def test_suggest_lists_a_known_clean_client(self):
        self.write_rows([("acme", "/work/acme-app")] * 4 + [("acme", "/work/other")])
        out = self.comms("link", "--suggest")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("acme", out.stdout.split("needs your call")[0])
        self.assertIn("/work/acme-app", out.stdout)

    def test_a_folder_two_clients_share_is_a_conflict_not_a_link(self):
        self.write_rows([("acme", "/work/shared")] * 5 + [("beta", "/work/shared")] * 2)
        out = self.comms("link", "--suggest")
        clean, _, conflicts = out.stdout.partition("needs your call")
        self.assertNotIn("beta", clean)
        self.assertIn("beta", conflicts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
