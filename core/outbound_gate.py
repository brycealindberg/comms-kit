"""One outbound gate for every send path (WhatsApp, Slack, iMessage, Gmail, Upwork).

Why it lives here and is called INSIDE each send tool: a gate the caller has to remember fails
open (iMessage, 2026-09-09: one script dropped its exit-code test and a BLOCKED draft went out).
The tools call it on the final resolved text, right before the transport call.

    from outbound_gate import OutboundPayload, Approvals, evaluate, begin, finish

    payload = OutboundPayload("whatsapp", target=portal_id, texts=[message], recipients=[...])
    verdict = evaluate(payload, approvals, dry_run=not args.yes, source="whatsapp send")
    if not verdict.allow:                 # only ever False in enforce mode
        print(verdict.deny_line()); sys.exit(DENY_EXIT)
    op = begin(payload, approvals)        # reserve right before the transport call
    ... transport ...
    finish(op, "sent", receipts=[event_id])      # or "uncertain" / "failed_before_send"

Checks, in order (an approval is read BY the check it waives, never after an earlier refusal):
  1. markup + private key blocks        no waiver
  2. developer names / never-tell       DEV_OK=1 only (logged to the guard's ACK log)
  3. draft_check style + budget         --override-check REASON only
  4. money lock                         --money-ok only
  5. duplicate send (24 h, sent_index)  --resend-ok only
A check that cannot run is an `error`. Nothing waives an error.

Modes: GATE_MODE env, else the file <comms>/gate_mode, else "report".
  report  never blocks; records would_allow + reasons in gate.jsonl; stdout/exit unchanged
  enforce blocks on any blocking finding or error (the caller prints deny_line, exits 7)
Any other mode value is treated as enforce (a typo must not silently turn the gate off).

Stdlib only, Python 3.9 compatible (hooks run under /usr/bin/python3 3.9).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import html
import importlib.util
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

DENY_EXIT = 7
MODES = ("report", "enforce")
DEFAULT_MODE = "report"          # P0 ships report mode (PLAN.md §3.4)

# ASSUMED thresholds (PLAN.md §3.4, INPUTS-NEEDED.md): change here.
DUP_WINDOW_S = 24 * 3600
DUP_SHORT_WINDOW_S = 10 * 60
DUP_SHORT_CHARS = 15
INDEX_KEEP_S = 7 * 24 * 3600
LOCK_TIMEOUT_S = 5.0
SNIPPET = 40

LT, GT = "<", ">"
CLOSE = LT + "/"
# Assembled from fragments so this file never contains the literal sequences (same as
# block-toolxml-in-outbound.py and imessage.py reject_tool_markup).
MARKERS = (CLOSE + "invoke" + GT, CLOSE + "parameter" + GT, CLOSE + "function_calls" + GT,
           LT + "function_calls" + GT, LT + "invoke name=", LT + "parameter name=", "antml" + ":")
INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u2028-\u202e\u2060-\u2064\ufeff]")


def _home() -> str:
    return os.path.expanduser("~")


def _core_dir() -> str:
    """The folder holding this file and kitconfig.py (abspath, so a symlinked copy still works)."""
    return os.path.dirname(os.path.abspath(__file__))  # kit: code path


def _kitconfig():
    here = os.path.dirname(os.path.abspath(__file__))  # kit: code path
    if here not in sys.path:  # kit: code path
        sys.path.insert(0, here)
    import kitconfig
    return kitconfig


def comms_dir() -> str:
    """The archive folder, as the kit config says (the same one commslib.ROOT uses)."""
    return _kitconfig().archive_dir()


def draft_check_path() -> str:
    """Your own checker when you keep one, else the kit's default one."""
    own = os.path.join(_home(), ".claude", "skills", "whatsapp", "draft_check.py")
    kit = os.path.join(_core_dir(), "..", "channels", "whatsapp", "draft_check.py")  # kit: code path
    return kit if not os.path.exists(own) and os.path.exists(kit) else own


def dev_guard_path() -> str:
    default = os.path.join(_home(), ".claude", "hooks", "dev-outbound-guard.py")
    return _kitconfig().path("outbound.dev_guard") or default


def dev_guard_is_optional() -> bool:
    """Only an unconfigured guard may be absent. A configured one that went missing is an error."""
    return not _kitconfig().path("outbound.dev_guard") and not os.path.exists(dev_guard_path())


class GateError(RuntimeError):
    """A check or the gate's own state could not give a trustworthy answer."""


class GateDenied(RuntimeError):
    """Enforce mode refused (raised by begin() when the reservation itself refuses)."""


@dataclass
class Approvals:
    money_ok: bool = False
    override_reason: str = ""        # style / budget findings only
    resend_ok: bool = False
    dev_ok: bool = False             # DEV_OK=1, or a name in outbound.dev_ok_env, in the environment

    @classmethod
    def from_env(cls, **kw: Any) -> "Approvals":
        names = ["DEV_OK"] + list(_kitconfig().get_list("outbound.dev_ok_env"))
        dev = any(os.environ.get(name) == "1" for name in names)
        return cls(dev_ok=dev, **kw)

    def override_valid(self) -> bool:
        return len((self.override_reason or "").strip()) >= 4


@dataclass
class OutboundPayload:
    channel: str                                  # whatsapp|slack|imessage|gmail|upwork
    target: str                                   # transport destination (room, channel, chat id, ...)
    texts: List[str] = field(default_factory=list)
    recipients: List[str] = field(default_factory=list)   # people: numbers, handles, emails
    scope: str = ""                               # Slack workspace, account, ...
    thread: str = ""                              # Slack thread_ts, Gmail thread, ...
    budget: str = ""                              # draft_check channel key; derived when empty
    filenames: List[str] = field(default_factory=list)
    attachment_texts: List[Tuple[str, str]] = field(default_factory=list)
    unread_attachments: List[str] = field(default_factory=list)
    attachment_hashes: List[str] = field(default_factory=list)
    raw_texts: List[str] = field(default_factory=list)
    inspection_errors: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)        # names the CLI resolved + what was typed
    is_group: Optional[bool] = None                        # None = the CLI does not know

    def all_words(self) -> List[str]:
        return list(self.texts) + list(self.filenames) + [t for _, t in self.attachment_texts]

    def budget_class(self) -> str:
        if self.budget:
            return self.budget
        if self.channel == "slack":
            return "slack-dm" if str(self.target).upper().startswith("D") else "slack-public"
        return self.channel

    def recipient_ids(self) -> List[str]:
        return sorted({str(r).strip().lower() for r in self.recipients if str(r or "").strip()})


@dataclass
class Verdict:
    mode: str
    would_allow: bool
    blocking: List[Tuple[str, str]] = field(default_factory=list)
    findings: List[Tuple[str, str]] = field(default_factory=list)
    checks: Dict[str, str] = field(default_factory=dict)
    hard: List[Tuple[str, str]] = field(default_factory=list)   # blocks in EVERY mode

    @property
    def allow(self) -> bool:
        if self.hard:
            return False
        return True if self.mode == "report" else self.would_allow

    def deny_line(self) -> str:
        reasons = list(self.hard) + list(self.blocking)
        why = "; ".join("%s: %s" % (c, r) for c, r in reasons) or "a check could not run"
        return "NOT SENT: outbound gate (%s): %s" % (self.mode, why)

    def to_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "would_allow": self.would_allow, "allow": self.allow,
                "hard": [list(h) for h in self.hard],
                "blocking": [list(b) for b in self.blocking],
                "findings": [list(f) for f in self.findings], "checks": dict(self.checks)}


# ------------------------------------------------------------------ mode

def gate_mode() -> Tuple[str, Optional[str]]:
    """(mode, problem). An unknown value is enforce: a typo must never turn the gate off."""
    raw = os.environ.get("GATE_MODE")
    where = "GATE_MODE"
    if raw is None:
        path = os.path.join(comms_dir(), "gate_mode")
        where = path
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return DEFAULT_MODE, None
        except OSError as e:
            return "enforce", "cannot read %s (%s)" % (path, e.__class__.__name__)
    value = (raw or "").strip().lower()
    if value in MODES:
        return value, None
    return "enforce", "invalid gate mode %r in %s" % (value[:20], where)


# ------------------------------------------------------------------ helpers

def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", INVISIBLE.sub("", text or ""))


def snippet(s: str) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= SNIPPET else s[: SNIPPET - 1] + "…"


_MODULES: Dict[str, Any] = {}


def _load(path: str, name: str) -> Any:
    if path in _MODULES:
        return _MODULES[path]
    if not os.path.isfile(path):
        raise GateError("missing %s" % path)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise GateError("cannot load %s" % path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _MODULES[path] = mod
    return mod


def strict_devs(dg: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The developer list plus every problem loading it. dev-outbound-guard's own load_devs()
    skips a broken profile silently; here a broken or unreadable registry is an error."""
    errors: List[str] = []
    devs: Dict[str, Any] = {}
    for raw in dg.BUILTIN_DEVS:
        devs[raw["slug"]] = dg.compile_dev(raw)
    registry = dg.REGISTRY
    try:
        names = sorted(os.listdir(registry))
    except OSError as e:
        return list(devs.values()), ["developer registry unreadable (%s)" % e.__class__.__name__]
    for slug in names:
        folder = os.path.join(registry, slug)
        path = os.path.join(folder, "profile.json")
        if not os.path.isdir(folder) or not os.path.exists(path):
            continue                       # README or a folder still being set up
        try:
            if os.path.getsize(path) > dg.MAX_FILE:
                errors.append("profile too large: %s" % slug)
                continue
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as e:
            errors.append("profile unreadable: %s (%s)" % (slug, e.__class__.__name__))
            continue
        dev = dg.compile_dev(raw) if isinstance(raw, dict) and raw.get("slug") == slug else None
        if dev is None:
            errors.append("profile invalid: %s" % slug)
            continue
        devs[slug] = dev
    return list(devs.values()), errors


# ------------------------------------------------------------------ checks

def _check_markup(p: OutboundPayload, v: Verdict) -> None:
    text_extensions = (".txt", ".md", ".json", ".csv", ".tsv", ".html", ".xml", ".yaml", ".yml",
                       ".py", ".js", ".ts", ".sh", ".log", ".sql", ".rtf")
    if p.inspection_errors or any(str(f).lower().endswith(text_extensions)
                                  for f in p.unread_attachments):
        raise GateError("outgoing text could not be inspected completely")
    for text in p.all_words() + p.raw_texts:
        text = html.unescape(text or "")
        for marker in MARKERS:
            if marker in (text or ""):
                v.blocking.append(("markup", "tool-call markup in the outgoing text"))
                v.checks["markup"] = "finding"
                return
        if "-----BEGIN" in (text or "") and "PRIVATE KEY-----" in (text or ""):
            v.blocking.append(("markup", "a private key block in the outgoing text"))
            v.checks["markup"] = "finding"
            return
    v.checks["markup"] = "ok"


def _check_dev(p: OutboundPayload, a: Approvals, v: Verdict, source: str) -> None:
    if dev_guard_is_optional():
        v.checks["dev"] = "off"
        return
    dg = _load(dev_guard_path(), "_gate_dev_outbound_guard")
    devs, errors = strict_devs(dg)
    if errors:
        raise GateError("; ".join(errors))
    tg = p.recipient_ids()
    hits = [h for t in p.all_words() for h in dg.name_hits(t, devs)]
    bad = dg.names_problem(hits, tg, devs)
    nt = [h for t in p.all_words() for h in dg.never_tell_hits(t, devs, tg)] if tg else []
    if not bad and not nt:
        if not tg:
            raise GateError("recipients unresolved: never-tell could not be checked")
        v.checks["dev"] = "ok"
        return
    words = sorted({w for _, w in (bad or [])} | {m for _, _, m in nt})
    if a.dev_ok:
        v.checks["dev"] = "waived"
        v.findings.append(("dev", "DEV_OK waived: %s" % snippet(", ".join(words))))
        try:
            dg.log_ack({"cwd": os.getcwd()}, "outbound_gate %s -> %s" % (source, p.target), tg, words)
        except Exception:  # noqa: BLE001 - the ack log never changes the outcome
            pass
        return
    v.checks["dev"] = "finding"
    who = "names a hidden developer" if bad else "tells a developer something kept from him"
    v.blocking.append(("dev", "%s (%s)" % (who, snippet(", ".join(words)))))


def _check_style_and_money(p: OutboundPayload, a: Approvals, v: Verdict) -> None:
    dc = _load(draft_check_path(), "_gate_draft_check")
    budget = p.budget_class()
    if budget not in dc.BUDGETS:
        raise GateError("no draft_check budget for %r" % budget)
    bubbles = [("bubble%d" % (i + 1), t) for i, t in enumerate(p.texts) if (t or "").strip()]
    findings: List[str] = []
    if bubbles:
        findings, _money, _stats = dc.check(bubbles, budget)
    if findings:
        first = findings[0].split("\n")[0]
        if a.override_valid():
            v.checks["style"] = "waived"
            v.findings.append(("style", "override (%s): %d finding(s)" % (snippet(a.override_reason), len(findings))))
        else:
            v.checks["style"] = "finding"
            v.blocking.append(("style", "%d draft_check finding(s), first: %s" % (len(findings), snippet(first))))
    else:
        v.checks["style"] = "ok"
    hits = []
    for t in p.all_words():
        hits += [m.group(0).strip() for m in dc.MONEY.finditer(dc.normalize_for_matching(t or ""))]
    if not hits:
        v.checks["money"] = "ok"
    elif a.money_ok:
        v.checks["money"] = "waived"
        v.findings.append(("money", "--money-ok: %s" % snippet(", ".join(hits))))
    else:
        v.checks["money"] = "finding"
        v.blocking.append(("money", "money lock: %s (show the owner the exact text; --money-ok after their yes)"
                           % snippet(", ".join(hits))))
    if p.unread_attachments:
        v.findings.append(("attachments", "not read: %s" % snippet(", ".join(p.unread_attachments))))


def dup_key(p: OutboundPayload) -> str:
    texts = "\x1e".join(" ".join(normalize(t).lower().split()) for t in p.texts)
    raw = "\x1f".join([p.channel, p.scope or "", str(p.target), p.thread or "", texts,
                       ",".join(sorted(p.attachment_hashes))])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _text_len(p: OutboundPayload) -> int:
    return len(" ".join(normalize(" ".join(p.texts)).split()))


def _dup_hit(index: Dict[str, Any], key: str, short: bool, now: float) -> Optional[Dict[str, Any]]:
    window = DUP_SHORT_WINDOW_S if short else DUP_WINDOW_S
    for op in index.get("ops", {}).values():
        if op.get("key") == key and op.get("status") in ("pending", "sent", "uncertain") \
                and now - float(op.get("ts") or 0) <= window:
            return op
    return None


def _check_dup(p: OutboundPayload, a: Approvals, v: Verdict) -> None:
    with strict_lock(index_path()):
        index = load_index()
    hit = _dup_hit(index, dup_key(p), _text_len(p) < DUP_SHORT_CHARS and not p.attachment_hashes, time.time())
    if not hit:
        v.checks["dup"] = "ok"
        return
    age = int((time.time() - float(hit.get("ts") or 0)) / 60)
    what = "an UNCERTAIN send" if hit.get("status") == "uncertain" else "the same message"
    if a.resend_ok:
        v.checks["dup"] = "waived"
        v.findings.append(("dup", "--resend-ok: %s went to this target %d min ago" % (what, age)))
    else:
        v.checks["dup"] = "finding"
        v.blocking.append(("dup", "%s went to this target %d min ago (--resend-ok only if the owner says resend)"
                           % (what, age)))


def evaluate(p: OutboundPayload, approvals: Optional[Approvals] = None, *, dry_run: bool = False,
             source: str = "") -> Verdict:
    """Run every check on the final resolved payload. Never raises; never sends; never reserves."""
    a = approvals or Approvals()
    mode, problem = gate_mode()
    v = Verdict(mode=mode, would_allow=True)
    if problem:
        v.blocking.append(("mode", problem))
        v.checks["mode"] = "error"
    steps = (("markup", lambda: _check_markup(p, v)),
             ("dev", lambda: _check_dev(p, a, v, source)),
             ("style", lambda: _check_style_and_money(p, a, v)),
             ("dup", lambda: _check_dup(p, a, v)),
             ("dnc", lambda: _check_dnc(p, v)),
             ("typed_yes", lambda: _check_typed_yes(p, v)))
    for name, step in steps:
        try:
            step()
        except Exception as e:  # noqa: BLE001 - a check that cannot run is an error, never a pass
            v.checks[name] = "error"
            if name == "typed_yes":  # an odd transcript must not stop every send: record, never block
                v.findings.append((name, "check could not run: %s" % snippet("%s: %s" % (e.__class__.__name__, e))))
                continue
            if name == "dnc":  # a safety list fails closed when it is on
                why = "check could not run: %s" % snippet("%s: %s" % (e.__class__.__name__, e))
                (v.hard if dnc_mode() == "block" else v.findings).append((name, why))
                continue
            if name == "style":
                v.checks["money"] = "error"
            v.blocking.append((name, "check could not run: %s" % snippet("%s: %s" % (e.__class__.__name__, e))))
    v.would_allow = not v.blocking and not v.hard
    _audit(p, v, dry_run=dry_run, source=source)
    return v


# ------------------------------------------------------------------ do-not-contact list
#
# outbound.dnc: "block" (a DM to a listed person blocks in EVERY mode, and a missing or broken list
# blocks every send), "log" (hits are recorded only), "off" or unset (no check). outbound.dnc_file:
# the list (default <archive>/do_not_contact.json). Names and aliases match whole words of the chat
# label the sender resolved; handles match the target and the recipients. A group only logs: a name
# in a group title does not show the person is in it, and LIDs hide members. An entry with
# "mode": "log" only logs, for a name that may belong to someone else.

DNC_MODES = ("block", "log", "off")


def dnc_mode() -> str:
    value = str(_kitconfig().get("outbound.dnc", "off") or "off").strip().lower()
    return value if value in DNC_MODES else "block"


def dnc_path() -> str:
    return _kitconfig().path("outbound.dnc_file") or os.path.join(comms_dir(), "do_not_contact.json")


def _whole_word(term: Any, text: Any) -> bool:
    t = _fold(term).strip()
    return bool(t) and re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", _fold(text)) is not None


def _handle_norm(value: Any) -> str:
    h = str(value or "").strip().lower()
    digits = re.sub(r"[\s()+.-]", "", h)
    return digits if digits.isdigit() else h


def _check_dnc(p: OutboundPayload, v: Verdict) -> None:
    mode = dnc_mode()
    if mode == "off":
        v.checks["dnc"] = "off"
        return
    path = dnc_path()
    try:
        with open(path, encoding="utf-8") as fh:
            entries = json.load(fh)["entries"]
        if not isinstance(entries, list):
            raise TypeError("entries is not a list")
    except (OSError, ValueError, KeyError, TypeError) as e:
        v.checks["dnc"] = "list error"
        why = ("the do-not-contact list %s could not be read (%s). Restore it, then send again"
               % (path, e.__class__.__name__))
        (v.hard if mode == "block" else v.findings).append(("dnc", why))
        return
    labels = [str(x) for x in (p.labels or []) if str(x or "").strip()]
    handles = {_handle_norm(x) for x in [p.target] + list(p.recipients or []) if str(x or "").strip()}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        terms = [entry.get("name")] + list(entry.get("aliases") or [])
        by_name = any(t and _whole_word(t, label) for t in terms for label in labels)
        listed = set()
        for values in (entry.get("handles") or {}).values():
            listed.update(_handle_norm(x) for x in (values or []) if str(x or "").strip())
        if not by_name and not (listed & handles):
            continue
        why = ("%s is on the do-not-contact list (%s). Ask the owner"
               % (entry.get("name") or "a listed person", entry.get("reason") or "no reason given"))
        if p.is_group:
            v.checks["dnc"] = "group"
            v.findings.append(("dnc", why + " (group: logged only)"))
        elif mode == "block" and str(entry.get("mode") or "block") != "log":
            v.checks["dnc"] = "hit"
            v.hard.append(("dnc", why))
            return
        else:
            v.checks["dnc"] = "hit (log)"
            v.findings.append(("dnc", why))
    v.checks.setdefault("dnc", "clear")


def dnc_lookup(labels: List[str]) -> Optional[str]:
    """The listed name a chat label hits (names and aliases, whole words), or None."""
    with open(dnc_path(), encoding="utf-8") as fh:
        entries = json.load(fh)["entries"]
    for entry in entries:
        if isinstance(entry, dict):
            terms = [entry.get("name")] + list(entry.get("aliases") or [])
            if any(t and _whole_word(t, label) for t in terms for label in labels):
                return str(entry.get("name"))
    return None


def dnc_add(name: str, aliases: List[str] = (), handles: Optional[Dict[str, List[str]]] = None,
            reason: str = "", added_by: str = "") -> Dict[str, Any]:
    """Add a person, or merge new aliases and handles into their entry. It never removes an entry,
    a handle or an alias, and never rewrites a reason: only the owner removes (by hand for now).
    A broken list raises and is left as it is."""
    path = dnc_path()
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            data = {"version": 1, "entries": []}
        entries = data.setdefault("entries", [])
        key = _fold(name).strip()
        entry = next((e for e in entries if isinstance(e, dict) and _fold(e.get("name")).strip() == key), None)
        if entry is None:
            entry = {"name": name.strip(), "aliases": [], "handles": {}, "reason": reason.strip(),
                     "added_by": added_by, "added": time.strftime("%Y-%m-%d")}
            entries.append(entry)
        have = entry.setdefault("aliases", [])
        for alias in aliases or ():
            if alias.strip() and _fold(alias).strip() not in [_fold(x).strip() for x in have] + [key]:
                have.append(alias.strip())
        for channel, values in (handles or {}).items():
            listed = entry.setdefault("handles", {}).setdefault(channel, [])
            for value in values or ():
                norm = _handle_norm(value)
                if norm and norm not in listed:
                    listed.append(norm)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    return entry

# ------------------------------------------------------------------ typed yes (the owner's own words)
#
# The 2026-11-02 case: the owner typed "Send it to <name>", the tool found no contact by that name,
# and the agent sent to someone else. When the owner's last typed prompt names people after "to" in a sentence with a send
# word, a DM must reach one of them. A mismatch is a HARD finding (blocks in every mode) when
# outbound.typed_yes is "block"; "log" records it; "off" skips the check. Groups only log: a group
# title does not show who is in it. No session, no transcript or no label: recorded, never blocked.

TYPED_YES_MODES = ("block", "log", "off")
TYPED_TAIL_BYTES = 4 * 1024 * 1024
_SEND_WORD = re.compile(r"(?i)\b(?:send|sent|text|message|msg|reply|respond|forward|tell|dm|ping|"
                        r"email|e-mail|post|share)\b")
_NAME = r"[^\W\d_][\w'’-]*"
_TO_NAMES = re.compile(r"\b[Tt]o\s+(" + _NAME + r")((?:\s*(?:,|&|\band\b)\s*" + _NAME + r")*)")
_LIST_NAME = re.compile(_NAME)
# Capitalized words after "to" that are not people. Lowercase words are never read as names.
NOT_A_NAME = frozenset("""
i me you him her us them it my your his our their this that these those everyone everybody anyone
someone the a an all both each whatsapp slack email gmail mail upwork imessage imessages sms telegram
linkedin instantly discord github clickup notion comms bot grok chatgpt claude codex loom
monday tuesday wednesday thursday friday saturday sunday today tomorrow tonight january february
march april may june july august september october november december dm dms channel group thread
send sent text message msg reply respond forward tell ping post share done draft drafted approved
""".split())
# A pasted block is someone else's text (a draft, a relay, a log), not the owner's own words.
_PASTE = re.compile(r"(?s)<pasted_content\b[^>]*>.*?(?:</pasted_content\b[^>]*>|\Z)")


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def _clean_name(word: str) -> str:
    word = re.sub(r"['’]s$", "", word)
    return _fold(word).strip("'’-")


def named_recipients(prompt: str) -> List[str]:
    """People the prompt sends to: capitalized words after "to", in a sentence with a send word."""
    names: List[str] = []
    own = _PASTE.sub("\n", str(prompt or ""))
    for sentence in re.split(r"[.!?\n]+", own):
        if not _SEND_WORD.search(sentence):
            continue
        for m in _TO_NAMES.finditer(sentence):
            words = [m.group(1)] + _LIST_NAME.findall(m.group(2) or "")
            for w in words:
                if w.lower() == "and" or not w[:1].isupper() or (len(w) > 1 and w.isupper()):
                    continue  # lowercase = not a name; ALL CAPS = a status word ("to SENT")
                name = _clean_name(w)
                if len(name) >= 2 and name not in NOT_A_NAME and name not in names:
                    names.append(name)
    return names


def _label_tokens(labels: List[str]) -> List[str]:
    out: List[str] = []
    for label in labels or []:
        for tok in re.split(r"[^\w]+", _fold(label)):
            if tok and not tok.isdigit() and tok not in ("wa", "dm") and tok not in out:
                out.append(tok)
    return out


def names_match(named: List[str], labels: List[str]) -> bool:
    """True when one named person is in the labels: same word, or one is a 3+ letter start of the other."""
    tokens = _label_tokens(labels)
    for n in named:
        for t in tokens:
            if n == t or (len(n) >= 3 and t.startswith(n)) or (len(t) >= 3 and n.startswith(t)):
                return True
    return False


def archive_label(channel: str, target: str) -> str:
    """The chat's name from the archive index (threads.json), or "" when it is not known.
    For senders whose own resolver gives no name (Slack sends take a bare channel id)."""
    try:
        with open(os.path.join(comms_dir(), "threads.json"), encoding="utf-8") as fh:
            rows = json.load(fh)
    except (OSError, ValueError):
        return ""
    row = rows.get(str(target or "").strip().lower()) if isinstance(rows, dict) else None
    if not isinstance(row, dict) or row.get("channel") != channel:
        return ""
    label = str(row.get("label") or "").strip()
    return "" if label in ("None", "null") else label


def typed_yes_mode() -> str:
    value = str(_kitconfig().get("outbound.typed_yes", "block") or "block").strip().lower()
    return value if value in TYPED_YES_MODES else "block"


def _prompt_text(row: Dict[str, Any]) -> Optional[str]:
    if row.get("type") != "user" or row.get("isSidechain"):
        return None
    # "queued" = typed while a turn was running; still the owner's own words (2026-11-07).
    if (row.get("origin") or {}).get("kind") != "human" or row.get("promptSource") not in ("typed", "queued"):
        return None
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        if parts and not any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return "\n".join(parts)
    return None


def transcript_path(session_id: str) -> Optional[str]:
    root = _kitconfig().path("outbound.transcripts_dir") or os.path.join(_home(), ".claude", "projects")
    if not session_id or not re.match(r"^[A-Za-z0-9-]{8,80}$", session_id):
        return None
    try:
        folders = os.listdir(root)
    except OSError:
        return None
    for folder in folders:
        candidate = os.path.join(root, folder, session_id + ".jsonl")
        if os.path.isfile(candidate):
            return candidate
    return None


def last_typed_prompt(path: str) -> Optional[str]:
    """The newest prompt the owner typed in this session (main thread only), from the file's tail."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > TYPED_TAIL_BYTES:
            fh.seek(size - TYPED_TAIL_BYTES)
            fh.readline()
        lines = fh.read().splitlines()
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            text = _prompt_text(row)
            if text is not None:
                return text
    return None


def _check_typed_yes(p: OutboundPayload, v: Verdict) -> None:
    mode = typed_yes_mode()
    if mode == "off":
        v.checks["typed_yes"] = "off"
        return
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not session:
        v.checks["typed_yes"] = "no session"
        return
    path = transcript_path(session)
    if not path:
        v.checks["typed_yes"] = "no transcript"
        return
    labels = [str(x) for x in (p.labels or []) if str(x or "").strip()]
    if not labels:
        v.checks["typed_yes"] = "no label"
        return
    prompt = last_typed_prompt(path)
    if prompt is None:
        v.checks["typed_yes"] = "no typed prompt"
        return
    named = named_recipients(prompt)
    if not named:
        v.checks["typed_yes"] = "no name"
        return
    if not _label_tokens(labels):  # a bare number: nothing to compare the name with
        v.checks["typed_yes"] = "label has no name"
        return
    if names_match(named, labels):
        v.checks["typed_yes"] = "match"
        return
    said = ", ".join(n.title() for n in named)
    why = ("the owner's last typed prompt says to %s; this chat is %s. Ask him who he meant"
           % (said, snippet(labels[0])))
    if p.is_group:
        v.checks["typed_yes"] = "group"
        v.findings.append(("typed_yes", why + " (group: logged only)"))
    elif mode == "block":
        v.checks["typed_yes"] = "mismatch"
        v.hard.append(("typed_yes", why))
    else:
        v.checks["typed_yes"] = "mismatch (log)"
        v.findings.append(("typed_yes", why))


# ------------------------------------------------------------------ reservation

def index_path() -> str:
    return os.path.join(comms_dir(), "sent_index.json")


@contextlib.contextmanager
def strict_lock(path: str, timeout: float = None):
    """An exclusive flock or GateError. Unlike commslib.filelock, never runs the body unlocked."""
    timeout = LOCK_TIMEOUT_S if timeout is None else timeout
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        raise GateError("cannot open lock %s (%s)" % (path, e.__class__.__name__))
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise GateError("lock timeout on %s" % os.path.basename(path))
                time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def load_index() -> Dict[str, Any]:
    path = index_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"version": 1, "ops": {}}
    except (OSError, ValueError) as e:
        raise GateError("sent index unreadable (%s)" % e.__class__.__name__)
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("ops"), dict):
        raise GateError("sent index has an unknown shape or version")
    return data


def _save_index(data: Dict[str, Any]) -> None:
    now = time.time()
    data["ops"] = {k: o for k, o in data["ops"].items() if now - float(o.get("ts") or 0) <= INDEX_KEEP_S}
    path = index_path()
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def begin(p: OutboundPayload, approvals: Optional[Approvals] = None) -> Optional[str]:
    """Reserve the send right before the transport call. Lookup + reservation happen under one
    strict lock, so two identical sends at the same moment cannot both pass. Returns an op id.

    enforce: a duplicate without --resend-ok, a lock failure or an unreadable index raises
             GateDenied (nothing was sent). report: problems go to stderr and None is returned.
    """
    a = approvals or Approvals()
    mode, problem = gate_mode()
    try:
        if problem:
            raise GateError(problem)
        key = dup_key(p)
        with strict_lock(index_path()):
            index = load_index()
            now = time.time()
            hit = _dup_hit(index, key, _text_len(p) < DUP_SHORT_CHARS and not p.attachment_hashes, now)
            if hit and not a.resend_ok and mode == "enforce":
                raise GateDenied("NOT SENT: outbound gate (enforce): dup: the same message went to this "
                                 "target recently (--resend-ok only if the owner says resend)")
            op = uuid.uuid4().hex[:16]
            index["ops"][op] = {"key": key, "ts": now, "status": "pending", "channel": p.channel,
                                "parts": len(p.texts) or len(p.filenames), "receipts": []}
            _save_index(index)
            return op
    except GateDenied:
        raise
    except Exception as e:  # noqa: BLE001
        if mode == "enforce":
            raise GateDenied("NOT SENT: outbound gate (enforce): could not reserve the send (%s)"
                             % snippet(str(e)))
        _warn("could not reserve (%s); report mode, continuing" % snippet(str(e)))
        return None


def finish(op: Optional[str], status: str, receipts: Optional[List[str]] = None) -> None:
    """status: sent | uncertain | failed_before_send. Never raises (the send already happened or
    certainly did not); a failure is printed to stderr."""
    if not op:
        return
    try:
        with strict_lock(index_path()):
            index = load_index()
            rec = index["ops"].get(op)
            if rec is None:
                return
            if status == "failed_before_send":
                index["ops"].pop(op, None)
            else:
                rec["status"] = status
                rec["receipts"] = list(receipts or [])
                rec["done_ts"] = time.time()
            _save_index(index)
    except Exception as e:  # noqa: BLE001
        _warn("could not record the send outcome (%s)" % snippet(str(e)))


# ------------------------------------------------------------------ audit

def _warn(msg: str) -> None:
    try:
        sys.stderr.write("outbound gate: %s\n" % msg)
    except Exception:  # noqa: BLE001
        pass


def _audit(p: OutboundPayload, v: Verdict, *, dry_run: bool, source: str) -> None:
    """One line per evaluation in gate.jsonl: hashes, lengths and reasons, never the full text."""
    words = "\x1e".join(p.texts)
    row = {"ts": round(time.time(), 3), "channel": p.channel, "target": str(p.target)[:120],
           "source": source, "dry_run": bool(dry_run), "text_sha": hashlib.sha256(words.encode()).hexdigest()[:16],
           "text_len": len(words), "parts": len(p.texts), "files": len(p.filenames)}
    row.update(v.to_dict())
    try:
        path = os.path.join(comms_dir(), "gate.jsonl")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as e:  # noqa: BLE001 - the audit log never changes a send's outcome
        _warn("could not write gate.jsonl (%s)" % e.__class__.__name__)


# ------------------------------------------------------------------ Gmail / Upwork (no local CLI)

def external_payload(channel: str, target: str, text: Optional[str], recipients=None) -> OutboundPayload:
    raw = text or ""
    # Preserve angle-bracket text for EVERY policy, not just markup. A plaintext <$500>
    # is escaped by the Gmail HTML renderer and is still a money commitment to its recipient.
    # HTML tags may conservatively count against the budget; never erase policy evidence.
    body = html.unescape(raw) if channel == "gmail" else raw
    # An opaque room/message id is a destination, not an audience identity. A caller that
    # resolved real recipients must supply them; enforce denies unresolved audiences.
    identities = recipients if recipients is not None else (
        [target] if "@" in str(target) or re.fullmatch(r"\+?[0-9]{10,15}", str(target)) else [])
    return OutboundPayload(channel, target=str(target or ""), texts=[body] if body.strip() else [],
                           recipients=[str(r) for r in identities if r], raw_texts=[raw])


def record_external_send(channel: str, target: str, text: Optional[str]) -> None:
    """comms-log.py calls this after a delivered Gmail/Upwork send, so the next identical send
    is caught as a duplicate. Never raises."""
    try:
        p = external_payload(channel, target, text)
        if not p.texts:
            return
        with strict_lock(index_path()):
            index = load_index()
            index["ops"][uuid.uuid4().hex[:16]] = {"key": dup_key(p), "ts": time.time(), "status": "sent",
                                                   "channel": channel, "parts": 1, "receipts": [],
                                                   "external": True}
            _save_index(index)
    except Exception as e:  # noqa: BLE001
        _warn("could not record the %s send for the duplicate check (%s)" % (channel, snippet(str(e))))


# ------------------------------------------------------------------ for CLIs

def readable_text(path: str, cap: int = 16000) -> Optional[str]:
    """A small UTF-8 file's text (an attachment the gate should read), else None."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(cap + 1)
    except OSError:
        return None
    return readable_bytes(raw, cap)


def readable_bytes(raw: bytes, cap: int = 16000) -> Optional[str]:
    """Read the same immutable bytes that the transport will upload, never a prefix."""
    if len(raw) > cap or b"\x00" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if text.strip() else None


def file_sha(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None
