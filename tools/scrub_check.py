#!/usr/bin/env python3
"""Fail when a file about to be published, or the git history, holds personal data.

The deny list and the allow list live outside the repo, because they are personal data too.
Hits print as path:line and a rule id. The matched text never prints, so a CI log cannot
leak what this tool exists to catch.

Exit codes: 0 clean, 1 hits, 2 could not check. A check that read nothing is never a pass.
"""
import argparse
import ipaddress
import os
import re
import subprocess
import sys

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "comms-kit")
DEFAULT_DENY = os.path.join(CONFIG_DIR, "scrub-deny.txt")
DEFAULT_ALLOW = os.path.join(CONFIG_DIR, "scrub-allow.txt")

EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
SAFE_EMAIL_DOMAINS = re.compile(
    r"(?:^|\.)(?:example\.(?:com|org|net)|example|invalid|test|localhost"
    r"|users\.noreply\.github\.com)$", re.I)
SAFE_EMAILS = {"noreply@anthropic.com"}
IPV4_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])")
PHONE_RE = re.compile(r"\+\d[\d ().-]{8,17}\d")
HOME_RE = re.compile(r"/(?:Users|home)/(?!you\b|YOU\b|<)[A-Za-z0-9._-]+")
FIXED_RULES = (
    ("tailnet-host", re.compile(r"[\w-]+\.ts\.net\b", re.I)),
    # Real Slack, Matrix and WhatsApp ids in test data. A fixture marks its id as fake.
    ("slack-id", re.compile(r"\b[CDGTUW]0(?!123456789)(?![A-Z0-9]*(?:FAKE|TEST|FIXTURE|DOCTOR|AAAA))"
                            r"[A-Z0-9]{8,10}\b")),
    ("matrix-room", re.compile(r"![A-Za-z0-9]{10,}:(?!example\.)[\w.-]+"
                               r"|!(?![Ff]ake)(?=[\w-]*\d)(?=[\w-]*[a-z])(?=[\w-]*[A-Z])[\w-]{18,}")),
    ("whatsapp-group", re.compile(r"\b120363(?!0{6})\d{12}\b")),
    ("secret-slack", re.compile(r"\bxox[a-z]-[0-9A-Za-z-]{10,}")),
    ("secret-github", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{20,}")),
    ("secret-google", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("secret-google-oauth", re.compile(r"\bGOCSPX-[0-9A-Za-z_-]{10,}")),
    ("secret-apikey", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("secret-matrix", re.compile(r"\bsyt_[0-9A-Za-z_]{10,}")),
    ("secret-private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


def safe_phone(text):
    digits = re.sub(r"\D", "", text)
    if len(digits) < 10:
        return True
    # +1 NXX 555 01XX is reserved for fiction.
    return len(digits) == 11 and digits[0] == "1" and digits[4:7] == "555" and digits[7:9] == "01"


def safe_ip(text):
    try:
        return not ipaddress.ip_address(text).is_global
    except ValueError:
        return True


def safe_email(text):
    if text.lower() in SAFE_EMAILS:
        return True
    return bool(SAFE_EMAIL_DOMAINS.search(text.rsplit("@", 1)[1]))


def load_deny(path):
    """Return [(rule id, compiled regex)]. Raises SystemExit(2) when nothing can be checked."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as err:
        print(f"scrub ERROR: cannot read the deny list: {err.strerror}", file=sys.stderr)
        raise SystemExit(2)
    rules = []
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        source = line[3:] if line.startswith("re:") else re.escape(line)
        try:
            rules.append((f"deny#{number}", re.compile(source, re.I)))
        except re.error:
            print(f"scrub ERROR: deny list line {number} is not a valid regex", file=sys.stderr)
            raise SystemExit(2)
    if not rules:
        print("scrub ERROR: the deny list has no rules", file=sys.stderr)
        raise SystemExit(2)
    return rules


def load_allow(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return {line.rstrip("\n") for line in fh if line.strip() and not line.startswith("#")}
    except FileNotFoundError:
        return set()


def line_rules(line, deny):
    """Every rule id this one line breaks."""
    hits = [rule for rule, regex in deny if regex.search(line)]
    hits += [rule for rule, regex in FIXED_RULES if regex.search(line)]
    if HOME_RE.search(line):
        hits.append("home-path")
    if any(not safe_email(m.group(0)) for m in EMAIL_RE.finditer(line)):
        hits.append("email")
    if any(not safe_ip(m.group(1)) for m in IPV4_RE.finditer(line)):
        hits.append("public-ip")
    if any(not safe_phone(m.group(0)) for m in PHONE_RE.finditer(line)):
        hits.append("phone")
    return hits


def repo_files(repo):
    """Tracked files plus new files git would add. Ignored files never reach the public."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        print("scrub ERROR: not a git repo", file=sys.stderr)
        raise SystemExit(2)
    return sorted({name for name in out.decode("utf-8", "surrogateescape").split("\0") if name})


def scan_files(repo, deny, allow):
    names, hits = repo_files(repo), []
    for name in names:
        path = os.path.join(repo, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            text = fh.read().decode("latin-1")
        hits.extend((f"{name}:0", rule) for rule in line_rules(name, deny))
        for number, line in enumerate(text.split("\n"), 1):
            if f"{name}:{line}" in allow:
                continue
            hits.extend((f"{name}:{number}", rule) for rule in line_rules(line, deny))
    return names, hits


def scan_history(repo, deny, allow):
    allowed_lines = {entry.split(":", 1)[1] for entry in allow if ":" in entry}
    try:
        out = subprocess.run(
            ["git", "-C", repo, "log", "--all", "-p", "--format=%H%n%an%n%ae%n%cn%n%ce%n%B"],
            capture_output=True, check=False).stdout.decode("latin-1")
    except OSError:
        print("scrub ERROR: git log failed", file=sys.stderr)
        raise SystemExit(2)
    hits = {}
    for line in out.split("\n"):
        body = line[1:] if line[:1] in "+- " else line
        if body in allowed_lines or line in allowed_lines:
            continue
        for rule in line_rules(line, deny):
            hits[rule] = hits.get(rule, 0) + 1
    return [(f"history({count})", rule) for rule, count in sorted(hits.items())]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--deny", default=DEFAULT_DENY)
    ap.add_argument("--allow", default=DEFAULT_ALLOW)
    ap.add_argument("--history", action="store_true", help="also scan every commit")
    args = ap.parse_args()
    deny = load_deny(args.deny)
    allow = load_allow(args.allow)
    names, hits = scan_files(args.repo, deny, allow)
    if not names:
        print("scrub ERROR: the repo has no files to check", file=sys.stderr)
        return 2
    if args.history:
        hits += scan_history(args.repo, deny, allow)
    for where, rule in hits[:300]:
        print(f"{where} {rule}")
    if len(hits) > 300:
        print(f"... and {len(hits) - 300} more")
    state = "clean" if not hits else "FAILED"
    print(f"scrub {state}: {len(names)} files, {len(hits)} hits")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
