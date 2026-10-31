"""`send --since` output vs the comms capture hook (commslib).

A refused send must never be archived as delivered, and the new preview lines
(`newest message here`, `next --since`) must never leak into the message text.
Run: python3 -m pytest -q  (from this directory)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import commslib as C  # noqa: E402

ROOM = "!fakepLIix6MEOLeMa61EqJomTEI1JEzO3joOj37HyVa"
ECHO = 'About to send to Sam Rivers (WA) [room %s]: "Not directly, but I know them well"\n' % ROOM
# a quote at the end of a line inside the preview must not be read as the close
NEWEST = '  newest message here: [2026-09-23 15:10] +lid-231339907412126: Do you "work"  (--since 1790190627.000)\n'
TEXT = "Not directly, but I know them well"


class WhatsappSince(unittest.TestCase):
    def test_a_stale_refusal_is_not_delivered(self):
        body = (ECHO + NEWEST +
                "NOT SENT: 1 newer message(s) in this chat since the one you read:\n"
                '  [2026-09-23 15:12] +lid-5914824327362: ok "sick"\n'
                "Re-read the chat, redraft, and get a fresh yes before sending.\n")
        self.assertTrue(C.failed(body, empty_counts=False, channel="whatsapp", target=ROOM))
        self.assertEqual(C._wa_delivered(body, "outbound", ROOM), [])

    def test_yes_without_since_is_not_delivered(self):
        body = (ECHO + NEWEST + "NOT SENT: --yes needs --since. Run without --yes first; "
                "the preview prints the value for the chat as you read it.\n")
        self.assertTrue(C.failed(body, empty_counts=False, channel="whatsapp", target=ROOM))

    def test_a_real_send_keeps_exactly_its_text(self):
        body = ECHO + NEWEST + "Sent. event_id=$abc123\nnext --since 1790190700.123\n"
        self.assertFalse(C.failed(body, empty_counts=False, channel="whatsapp", target=ROOM))
        self.assertEqual(C._wa_delivered(body, "outbound", ROOM), [TEXT])

    def test_a_dry_run_is_a_draft_with_exactly_its_text(self):
        body = ECHO + NEWEST + "Dry run — pass --yes to actually send.\n"
        self.assertEqual(C._wa_delivered(body, "draft", ROOM), [TEXT])


class SlackSince(unittest.TestCase):
    CH = "D0FAKEDM001"
    NEWEST = ("  newest message here: [2026-09-23 14:53 ET] acme: Thanks Alex!  "
              "(--since 1790189636.637169)\n")

    def test_a_stale_refusal_is_a_failure(self):
        body = (self.NEWEST + "NOT SENT: 1 newer message(s) in this channel since the one you read:\n"
                "  [2026-09-23 14:55 ET] Alex Doe: all good\n")
        self.assertTrue(C.failed(body, empty_counts=False, channel="slack", target=self.CH))

    def test_a_real_send_is_not_a_failure(self):
        body = (self.NEWEST + '{\n  "ok": true,\n  "ts": "1790189700.100000",\n  "channel": "%s"\n}\n'
                "next --since 1790189700.100000\n" % self.CH)
        self.assertIsNot(C.failed(body, empty_counts=False, channel="slack", target=self.CH), True)


if __name__ == "__main__":
    unittest.main()
