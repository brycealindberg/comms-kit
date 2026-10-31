"""Long WhatsApp messages: the bridge puts the whole text in `formatted_body` (HTML)
and a short copy ending in `[...]` in `body`. `_message_text` must return the full text.

The ways it can go wrong, each pinned below:
  - a long message stays cut at ~500 chars;
  - a normal message with a mention pill gets its HTML instead of its body;
  - a real message ending in `[...]` is replaced by nothing or by junk;
  - same-length formatted_body replaces body for no gain;
  - HTML (br, p, lists, entities) comes out unreadable;
  - formatted_body is None or not a string and the reader crashes;
  - an edit (m.new_content) keeps the short copy.

Run: python3 -m pytest -q test_long_body.py   (from this directory)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Hermetic config: fake values only, never the real kit config. Without this, running this
# file first loads the module with no owner and breaks the message-admin tests after it.
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
import whatsapp_mautrix as wm  # noqa: E402

LONG = "Section %d: " + "word " * 40


def _long_parts(n=30):
    paras = [LONG % i for i in range(n)]
    return paras


class MessageText(unittest.TestCase):
    def test_long_case_returns_full_text(self):
        paras = _long_parts()
        c = {"body": ("\n".join(paras))[:480] + "[...]",
             "formatted_body": "".join(f"<p>{p}</p>" for p in paras)}
        out = wm._message_text(c)
        self.assertGreater(len(out), 5000)
        self.assertTrue(out.startswith("Section 0:"))
        self.assertTrue(out.rstrip().endswith("word"))
        self.assertNotIn("<p>", out)

    def test_mention_keeps_body(self):
        c = {"body": "hi @Sam see this",
             "formatted_body": 'hi <a href="https://matrix.to/#/@wa_1:x">@Sam</a> see this, and a lot more html ' * 3}
        self.assertEqual(wm._message_text(c), "hi @Sam see this")

    def test_real_ellipsis_without_formatted_body(self):
        self.assertEqual(wm._message_text({"body": "wait for it [...]"}), "wait for it [...]")

    def test_same_length_keeps_body(self):
        c = {"body": "abc [...]", "formatted_body": "abc <b>[...]</b>"}
        self.assertEqual(wm._message_text(c), "abc [...]")

    def test_html_is_readable(self):
        fb = ("<p>Title &amp; more</p><ul><li>one<ul><li>nested</li></ul></li><li>two</li></ul>"
              "line a<br>line b<br/>line c <b>bold</b> &lt;x&gt; " + "pad " * 30)
        out = wm._message_text({"body": "Title[...]", "formatted_body": fb})
        self.assertIn("Title & more", out)
        self.assertIn("<x>", out)
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        for want in ("- one", "- nested", "- two", "line a", "line b"):
            self.assertIn(want, lines)
        self.assertIn("line c bold <x>", out)
        self.assertNotIn("<li>", out)
        self.assertNotIn("&amp;", out)

    def test_numbered_lists_keep_their_numbers(self):
        fb = ('<ol start="7"><li>seven</li><li>eight</li></ol><p>gap</p><ol><li>first</li></ol>'
              '<ol start="x"><li>bad start</li></ol>'
              '<ol><li>outer<ul><li>inner</li></ul></li><li>next</li></ol>' + "<p>pad</p>" * 20)
        out = wm._message_text({"body": "seven[...]", "formatted_body": fb})
        lines = [l.rstrip() for l in out.splitlines() if l.strip()]
        for want in ("7. seven", "8. eight", "1. first", "1. bad start", "1. outer", "  - inner", "2. next"):
            self.assertIn(want, lines)

    def test_hostile_list_markup_does_not_crash(self):
        huge = "9" * 5000
        for fb in (f'<ol start="{huge}"><li>x</li></ol>' + "pad " * 500,
                   "</ol></ul><li>no list open</li>" + "pad " * 500,
                   "<ol/><li>self closed</li>" + "pad " * 500):
            out = wm._message_text({"body": "x[...]", "formatted_body": fb})
            self.assertIn("pad", out)

    def test_bad_formatted_body_keeps_body(self):
        for fb in (None, 5, ["x"], {"a": 1}):
            c = {"body": "short[...]", "formatted_body": fb}
            self.assertEqual(wm._message_text(c), "short[...]")
        self.assertEqual(wm._message_text({}), "")
        self.assertEqual(wm._message_text({"body": None}), "")
        self.assertEqual(wm._message_text(None), "")

    def test_edit_event_new_content(self):
        paras = _long_parts(10)
        new = {"body": "\n".join(paras)[:300] + "[...]",
               "formatted_body": "<br>".join(paras)}
        ev = {"content": {"body": "* " + new["body"], "m.new_content": new}}
        out = wm._message_text(ev["content"]["m.new_content"])
        self.assertGreater(len(out), 1000)
        self.assertNotIn("<br>", out)


if __name__ == "__main__":
    unittest.main()
