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

    @property
    def allow(self) -> bool:
        return True if self.mode == "report" else self.would_allow

    def deny_line(self) -> str:
        why = "; ".join("%s: %s" % (c, r) for c, r in self.blocking) or "a check could not run"
        return "NOT SENT: outbound gate (%s): %s" % (self.mode, why)

    def to_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "would_allow": self.would_allow, "allow": self.allow,
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
             ("dup", lambda: _check_dup(p, a, v)))
    for name, step in steps:
        try:
            step()
        except Exception as e:  # noqa: BLE001 - a check that cannot run is an error, never a pass
            v.checks[name] = "error"
            if name == "style":
                v.checks["money"] = "error"
            v.blocking.append((name, "check could not run: %s" % snippet("%s: %s" % (e.__class__.__name__, e))))
    v.would_allow = not v.blocking
    _audit(p, v, dry_run=dry_run, source=source)
    return v


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
