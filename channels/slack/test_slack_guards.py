"""Tests for the flag-gated outbound guards wired into slack_bridge.py.

These cover the two guards that apply to Slack (the CLI already has a --yes approval
gate, so Guard 1's hold-for-review is not duplicated here):
  - Guard 2 (recipient): refuse a send to nobody / an excluded channel.
  - Guard 3 (delivery): refuse to treat a non-ok bridge response as "sent".

The guard logic itself lives in and is tested by
<repo_root>/execution/personal_workflows/outbound_guards.py; these tests
prove the WIRING in slack_bridge.py (flag gating, fail-closed, response parsing).

Run: python3 -m unittest test_slack_guards   (from this directory)
"""
import io
import os
import sys
import time
import unittest
import unittest.mock
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slack_bridge  # noqa: E402


def _guards_importable():
    try:
        slack_bridge._load_outbound_guards()
        return True
    except Exception:  # noqa: BLE001 - the shared guard module is private; absent in a bare checkout
        return False


NEEDS_GUARDS = unittest.skipUnless(_guards_importable(), "the shared outbound_guards module is not available here")


class GuardsOff(unittest.TestCase):
    def setUp(self):
        os.environ.pop("OUTBOUND_GUARDS", None)

    def test_recipient_guard_noop_when_off(self):
        # No exception even for an empty channel when the flag is off.
        slack_bridge._apply_recipient_guard("", [])

    def test_delivery_guard_noop_when_off(self):
        # A failed response is still just printed; no refusal when the flag is off.
        with redirect_stdout(io.StringIO()):
            slack_bridge._report_send_result({"ok": False, "error": "channel_not_found"})


class RecipientGuard(unittest.TestCase):
    def setUp(self):
        os.environ["OUTBOUND_GUARDS"] = "1"

    def tearDown(self):
        os.environ.pop("OUTBOUND_GUARDS", None)

    def test_empty_channel_refused(self):
        with self.assertRaises(SystemExit):
            slack_bridge._apply_recipient_guard("   ", [])

    @NEEDS_GUARDS
    def test_real_channel_allowed(self):
        slack_bridge._apply_recipient_guard("C0TESTCH0002", [])

    def test_excluded_channel_refused(self):
        with self.assertRaises(SystemExit):
            slack_bridge._apply_recipient_guard("general", ["general"])


class DeliveryGuard(unittest.TestCase):
    def setUp(self):
        os.environ["OUTBOUND_GUARDS"] = "1"

    def tearDown(self):
        os.environ.pop("OUTBOUND_GUARDS", None)

    @NEEDS_GUARDS
    def test_ok_response_confirmed(self):
        with redirect_stdout(io.StringIO()):
            slack_bridge._report_send_result({"ok": True, "ts": "1700000000.000100", "channel": "C1"})

    def test_not_ok_response_refused(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                slack_bridge._report_send_result({"ok": False, "error": "channel_not_found"})

    def test_missing_receipt_refused(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                slack_bridge._report_send_result({"weird": "shape"})


class SinceGuard(unittest.TestCase):
    """`send --since`: the live pre-send re-read. No network: _live_messages is stubbed."""
    READ = 1790189636.637169            # the message the caller read

    def setUp(self):
        self._live = slack_bridge._live_messages

    def tearDown(self):
        slack_bridge._live_messages = self._live

    def guard(self, msgs=None, error=None, since=READ):
        def fake(config, channel, workspace):
            if error:
                raise error
            return msgs
        slack_bridge._live_messages = fake
        with redirect_stdout(io.StringIO()):
            slack_bridge.chat_unchanged_guard(None, "D0TESTDM0001", None, since)

    def test_the_message_you_read_passes(self):
        self.guard([{"ts": "1790189636.637169", "name": "Morgan"}])

    def test_a_newer_message_from_anyone_refuses(self):
        with self.assertRaises(SystemExit) as cm:
            self.guard([{"ts": "1790189636.637169"}, {"ts": "1790189700.1", "name": "Alex Doe"}])
        self.assertEqual(cm.exception.code, slack_bridge.STALE_EXIT)

    def test_a_newer_thread_reply_refuses(self):
        with self.assertRaises(SystemExit):
            self.guard([{"ts": "1790189636.637169", "latest_reply": "1790189900.5"}])

    def test_a_failed_read_refuses_but_only_with_since(self):
        with self.assertRaises(SystemExit) as cm:
            self.guard(error=RuntimeError("ssh down"))
        self.assertEqual(cm.exception.code, slack_bridge.STALE_EXIT)
        self.guard(error=RuntimeError("ssh down"), since=None)   # dry run without --since: just a note

    def test_an_empty_read_refuses(self):
        with self.assertRaises(SystemExit):
            self.guard([])

    def test_empty_passes_only_while_the_channel_is_empty(self):
        self.guard([], since=0.0)
        with self.assertRaises(SystemExit):
            self.guard([{"ts": "1790189636.637169"}], since=0.0)

    def test_next_since_is_the_sent_ts(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            slack_bridge.print_next_since({"ok": True, "ts": "1790189700.1", "channel": "D1"})
            slack_bridge.print_next_since({"ok": True})
        self.assertEqual(buf.getvalue().splitlines(),
                         ["next --since 1790189700.100000",
                          "next --since unknown (the bridge returned no message ts)"])

    def _handle_send(self, argv):
        """Run the real `send` path with the bridge faked; -> list of POSTs made."""
        posts = []

        def fake_request(config, path, params=None, method="GET", body=None, timeout=30):
            if method == "POST":
                posts.append((path, body))
                return {"ok": True, "ts": "1790189999.000100", "channel": "D0TESTDM0001"}
            return []
        saved = (slack_bridge.request_json, slack_bridge.load_config, slack_bridge._live_messages)
        slack_bridge.request_json = fake_request
        slack_bridge.load_config = lambda: None
        slack_bridge._live_messages = lambda c, ch, ws: [{"ts": str(self.READ), "name": "Morgan"}]
        try:
            with redirect_stdout(io.StringIO()):
                slack_bridge.handle(slack_bridge.build_parser().parse_args(argv))
        finally:
            slack_bridge.request_json, slack_bridge.load_config, slack_bridge._live_messages = saved
        return posts

    def test_yes_without_since_never_posts(self):
        with self.assertRaises(SystemExit) as cm:
            self._handle_send(["send", "D0TESTDM0001", "hi", "--yes"])
        self.assertEqual(cm.exception.code, 2)
        # a second call with the anchor proves the fake would have recorded a POST
        self.assertEqual(len(self._handle_send(["send", "D0TESTDM0001", "hi", "--yes",
                                                "--since", str(self.READ)])), 1)

    def test_stale_since_never_posts(self):
        with self.assertRaises(SystemExit) as cm:
            self._handle_send(["send", "D0TESTDM0001", "hi", "--yes", "--since", "1790189000"])
        self.assertEqual(cm.exception.code, slack_bridge.STALE_EXIT)

    def test_parse_since(self):
        self.assertEqual(slack_bridge.parse_since("empty"), 0.0)
        self.assertEqual(slack_bridge.parse_since("1790189636000"), 1790189636.0)
        for bad in ("2:53pm", "0", ""):
            with self.assertRaises(slack_bridge.argparse.ArgumentTypeError):
                slack_bridge.parse_since(bad)


class TimesFollowTheMacZone(unittest.TestCase):
    """The owner travels; macOS sets the zone by location. Times must follow it, not ET."""

    def setUp(self):
        self._tz = os.environ.get("TZ")

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def test_local_times_use_the_system_zone(self):
        os.environ["TZ"] = "Asia/Tokyo"
        time.tzset()
        self.assertEqual(slack_bridge._et(0), "1970-01-01 09:00 JST")

    def test_zone_name_read_from_localtime_link(self):
        link = "/var/db/timezone/zoneinfo/America/Los_Angeles"
        with unittest.mock.patch.object(slack_bridge.os, "readlink", return_value=link):
            self.assertEqual(slack_bridge._local_zone(), "America/Los_Angeles")

    def test_unreadable_or_odd_zone_falls_back_to_utc(self):
        with unittest.mock.patch.object(slack_bridge.os, "readlink", side_effect=OSError):
            self.assertEqual(slack_bridge._local_zone(), "UTC")
        for odd in ("/etc/weird", "/usr/share/zoneinfo/America/X; rm -rf ~", "/zoneinfo/$(id)"):
            with unittest.mock.patch.object(slack_bridge.os, "readlink", return_value=odd):
                self.assertEqual(slack_bridge._local_zone(), "UTC")

    def test_vps_thread_reader_gets_the_mac_zone(self):
        with unittest.mock.patch.object(slack_bridge, "_local_zone", return_value="Europe/Lisbon"), \
             unittest.mock.patch.object(slack_bridge.subprocess, "run") as run:
            slack_bridge._thread_script("C0123456789", "acme", 5)
        remote = run.call_args[0][0][-1]
        self.assertIn("TZ=Europe/Lisbon node - ", remote)


if __name__ == "__main__":
    unittest.main()
