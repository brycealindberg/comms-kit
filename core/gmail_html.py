#!/usr/bin/env python3
"""gmail-html-body -- plain email text to the HTML Gmail's own composer sends.

  gmail-html-body FILE          print FILE as Gmail-style HTML
  gmail-html-body --check FILE  print nothing; exit 1 if FILE is missing or blank

Why: `gws gmail +reply` without `--html` sends text/plain and quotes the old
email as "> " lines, which Gmail shows in full under the reply. With `--html`
gws wraps the quote in Gmail's `gmail_quote` block, which Gmail folds behind
"..." exactly like a reply typed in Gmail. A plain-text draft also opens in
Gmail's plain-text mode, which hard-wraps lines at ~76 characters on send.

gws sends an empty `--body` without complaint, so a failed conversion inside
`$(...)` would send a blank reply. Always run `--check` first:

  gmail-html-body --check F && gws gmail +reply --message-id ID --html \\
      --body "$(gmail-html-body F)"

comms-log reads F itself from that `$(gmail-html-body F)`, so the archive keeps
the plain words, not the HTML. Stdlib only, to match commslib.py.
"""
import html
import re
import sys

# A URL runs to the next space or quote; an address needs a dotted domain.
# One alternation, URL first, so an address inside a URL is not linked twice.
LINK_RE = re.compile(
    r"(?P<url>(?:https?://|www\.)[^\s<>\"]+)"
    r"|(?P<email>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)")
TRAILING = ".,;:!?'\""


def _trim(url):
    """Cut sentence punctuation off a URL's end; keep a ')' it opened itself."""
    while url:
        if url[-1] in TRAILING:
            url = url[:-1]
        elif url[-1] == ")" and url.count(")") > url.count("("):
            url = url[:-1]
        elif url[-1] == "]" and url.count("]") > url.count("["):
            url = url[:-1]
        else:
            break
    return url


def _text(s):
    # HTML collapses runs of spaces; "&nbsp; " keeps an indent or a double space
    return html.escape(s, quote=False).replace("  ", "&nbsp; ")


def _line(line):
    out, pos = [], 0
    for m in LINK_RE.finditer(line):
        if m.start() < pos:
            continue
        raw = m.group(0)
        if m.group("url"):
            raw = _trim(raw)
            href = raw if raw.startswith("http") else "http://" + raw
        else:
            raw = raw.rstrip(".")
            href = "mailto:" + raw
        if not raw:
            continue
        out.append(_text(line[pos:m.start()]))
        out.append('<a href="%s">%s</a>' % (html.escape(href), html.escape(raw, quote=False)))
        pos = m.start() + len(raw)
    out.append(_text(line[pos:]))
    return "".join(out)


def to_html(text):
    """The body as Gmail's composer writes it: one <div> per line, an empty
    line as <div><br></div>, links as anchors, markup-looking text escaped.
    Raises ValueError on a blank body rather than return an empty email."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise ValueError("email body is blank")
    rows = ["<div>%s</div>" % _line(ln) if ln.strip() else "<div><br></div>" for ln in lines]
    return '<div dir="ltr">%s</div>' % "".join(rows)


def main(argv):
    args = argv[1:]
    check = bool(args) and args[0] == "--check"
    if check:
        args = args[1:]
    if len(args) != 1:
        sys.stderr.write("usage: gmail-html-body [--check] FILE\n")
        return 2
    try:
        with open(args[0], encoding="utf-8") as f:
            body = to_html(f.read())
    except (OSError, UnicodeDecodeError, ValueError) as e:
        sys.stderr.write("gmail-html-body: %s: %s\n" % (args[0], e))
        return 1
    if not check:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
