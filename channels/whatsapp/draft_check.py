#!/usr/bin/env python3
"""Default pre-send check: message size and money words. Python stdlib only.

Replace this file to add your own rules, such as your writing style. Every channel and
core/outbound_gate.py use the same four names, so keep them:

  BUDGETS                       {channel: (max_bubbles, max_chars)}
  MONEY                         the regex for commercial terms
  normalize_for_matching(text)  a copy of the text for matching only
  check(bubbles, channel)       (findings, money_hits, stats); bubbles = [(name, text), ...]

Command line: draft_check.py [--channel NAME] FILE [FILE ...]
Exit codes: 0 clean, 1 a finding or a money term (always with a report on stdout), 2 usage.
"""
import argparse
import re
import sys
import unicodedata

CHAT = (10, 4000)
BUDGETS = {
    "whatsapp": CHAT,
    "imessage": CHAT,
    "slack-dm": CHAT,
    "slack-public": CHAT,
    "upwork": CHAT,
    "gmail": (1, 20000),
    "instantly": (1, 20000),
}

MONEY = re.compile(
    r"(?i)(?:\$\s*[\d,]|\b\d+\s*(?:usd|dollars|bucks|grand|k)\b"
    r"|\b(?:invoice|invoiced"
    r"|retainer|charge|charged|quote|quoted|price|priced|pricing|refund"
    r"|deposit|fee|discount|owe|owes|owed|free of charge|no charge"
    r"|paid in full|per month|/month|payment|payments|upfront|up front"
    r"|wire|venmo|zelle|paypal|cashapp|subscription)\b)"
)

# "$<U+200B>500" shows as "$500" on a phone, but a zero-width character splits the pattern.
INVISIBLE = re.compile(r"[­​-‏ -‮⁠-⁤﻿]")


def normalize_for_matching(text):
    """For matching only. The message itself is never changed."""
    return unicodedata.normalize("NFKC", INVISIBLE.sub("", text))


def check(bubbles, channel):
    max_bubbles, max_chars = BUDGETS[channel]
    total = sum(len(text) for _name, text in bubbles)
    findings = []
    if len(bubbles) > max_bubbles:
        findings.append("%d messages: the limit is %d" % (len(bubbles), max_bubbles))
    if total > max_chars:
        findings.append("%d characters: the limit is %d" % (total, max_chars))
    money_hits = [(name, m.group(0).strip()) for name, text in bubbles
                  for m in MONEY.finditer(normalize_for_matching(text))]
    stats = {"prose_bubbles": len(bubbles), "prose_chars": total, "paste_blocks": 0,
             "max_bubbles": max_bubbles, "max_chars": max_chars}
    return findings, money_hits, stats


def main(argv):
    ap = argparse.ArgumentParser(description="Default pre-send check.")
    ap.add_argument("files", nargs="+", help="one file per message, in send order")
    ap.add_argument("--channel", default="whatsapp", choices=sorted(BUDGETS))
    args = ap.parse_args(argv)

    bubbles = []
    for path in args.files:
        try:
            with open(path, encoding="utf-8") as fh:
                bubbles.append((path.rsplit("/", 1)[-1], fh.read().rstrip("\n")))
        except OSError as error:
            sys.stderr.write("error: cannot read %s: %s\n" % (path, error.strerror))
            return 2

    findings, money_hits, stats = check(bubbles, args.channel)
    print("%d message(s) / %d limit, %d character(s) / %d limit"
          % (stats["prose_bubbles"], stats["max_bubbles"], stats["prose_chars"],
             stats["max_chars"]))
    for finding in findings:
        print("FINDING: " + finding)
    if money_hits:
        print("MONEY LOCK: %d commercial term(s). Read the exact text before you send."
              % len(money_hits))
        for name, hit in money_hits:
            print('  [%s] "%s"' % (name, hit))
    if findings or money_hits:
        return 1
    print("Clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
