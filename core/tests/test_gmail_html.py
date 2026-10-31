#!/usr/bin/env python3
"""Tests for gmail_html -- plain reply text to the HTML Gmail's own composer sends.

Gmail folds the quoted thread behind "..." only in an HTML reply (the
`gmail_quote` block `gws --html` builds). A plain-text reply shows the whole
old email as "> " lines, so every gws send goes out as HTML made here.

Stdlib only (unittest), to match commslib.py. Run:

    python3 test_gmail_html.py
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import gmail_html as G  # noqa: E402
import commslib as C  # noqa: E402

CLI = os.path.join(HERE, "gmail_html.py")


class ToHtml(unittest.TestCase):

    def test_text_that_looks_like_markup_is_escaped_not_rendered(self):
        h = G.to_html("Q&A on <b>Friday</b>, 5 < 6")
        self.assertIn("Q&amp;A on &lt;b&gt;Friday&lt;/b&gt;, 5 &lt; 6", h)
        self.assertNotIn("<b>", h)

    def test_signature_lines_stay_on_their_own_lines(self):
        h = G.to_html("Sounds good.\n\nThanks,\nAlex")
        self.assertIn("<div>Thanks,</div><div>Alex</div>", h)

    def test_blank_line_becomes_one_gmail_spacer(self):
        h = G.to_html("One.\n\nTwo.")
        self.assertEqual(h, '<div dir="ltr"><div>One.</div><div><br></div><div>Two.</div></div>')

    def test_crlf_and_trailing_blank_lines_add_no_empty_rows(self):
        self.assertEqual(G.to_html("One.\r\nTwo.\r\n\r\n\n"),
                         '<div dir="ltr"><div>One.</div><div>Two.</div></div>')

    def test_url_is_linked_and_sentence_period_stays_outside(self):
        h = G.to_html("Book here: https://cal.example.com/alex/30min.")
        self.assertIn('<a href="https://cal.example.com/alex/30min">https://cal.example.com/alex/30min</a>.', h)

    def test_url_query_ampersand_is_escaped_in_href_and_text(self):
        h = G.to_html("https://x.com/a?b=1&c=2")
        self.assertIn('<a href="https://x.com/a?b=1&amp;c=2">https://x.com/a?b=1&amp;c=2</a>', h)

    def test_closing_paren_of_the_sentence_is_not_part_of_the_link(self):
        h = G.to_html("(see https://x.com/doc)")
        self.assertIn('(see <a href="https://x.com/doc">https://x.com/doc</a>)', h)

    def test_balanced_parens_inside_a_url_are_kept(self):
        u = "https://en.wikipedia.org/wiki/Mercury_(planet)"
        self.assertIn('<a href="%s">%s</a>' % (u, u), G.to_html("Read %s please" % u))

    def test_email_address_becomes_a_mailto_link(self):
        h = G.to_html("Write to pat@acme.example.com.")
        self.assertIn('<a href="mailto:pat@acme.example.com">pat@acme.example.com</a>.', h)

    def test_email_inside_a_url_is_not_linked_twice(self):
        h = G.to_html("https://x.com/u?e=a@b.example.com")
        self.assertEqual(h.count("<a "), 1)
        self.assertNotIn("mailto:", h)

    def test_indent_and_double_spaces_survive_html_whitespace_collapse(self):
        h = G.to_html("  1. Open it")
        self.assertIn("<div>&nbsp; 1. Open it</div>", h)

    def test_non_ascii_text_is_kept(self):
        self.assertIn("Café 👍", G.to_html("Café 👍"))

    def test_empty_or_blank_body_is_refused(self):
        for bad in ("", "   ", "\n\n \n"):
            with self.assertRaises(ValueError):
                G.to_html(bad)


class Cli(unittest.TestCase):

    def run_cli(self, *args):
        return subprocess.run([sys.executable, CLI] + list(args),
                              capture_output=True, text=True)

    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_prints_html_for_a_good_file(self):
        r = self.run_cli(self.write("Hi Sam,\n\nThanks,\nAlex\n"))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith('<div dir="ltr"><div>Hi Sam,</div>'))

    def test_check_passes_silently_for_a_good_file(self):
        r = self.run_cli("--check", self.write("Hi.\n"))
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_check_fails_for_a_missing_or_empty_file(self):
        # `--check FILE && gws ... --body "$(gmail-html-body FILE)"`: gws sends
        # an empty --body without complaint, so the check must stop the chain
        self.assertNotEqual(self.run_cli("--check", "/nonexistent/reply.txt").returncode, 0)
        self.assertNotEqual(self.run_cli("--check", self.write("\n \n")).returncode, 0)

    def test_failure_prints_nothing_on_stdout(self):
        r = self.run_cli(self.write(""))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")


class CommsArchiveReadsThePlainText(unittest.TestCase):
    """comms-log archives what was sent. With `--body "$(gmail-html-body F)"`
    it must read F (the plain words the owner approved), not lose the text."""

    def test_converter_substitution_names_its_file(self):
        v = "$(gmail-html-body /tmp/client-draft/reply.txt)"
        self.assertTrue(C.unexpanded_text(v))
        self.assertEqual(C.substitution_file(v), "/tmp/client-draft/reply.txt")

    def test_cat_substitution_still_works(self):
        self.assertEqual(C.substitution_file('$(cat "/tmp/x/body.txt")'), "/tmp/x/body.txt")


if __name__ == "__main__":
    unittest.main()
