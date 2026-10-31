"""Shared helpers for the comms-kit channel adapters (telegram, gchat, chatwoot).

Why this file exists: the money lock, the outbound gate calls and the config reader were copied
into every adapter. A fix had to be repeated by hand. They live here once. An adapter keeps a
short bootstrap that finds this file (COMMS_KIT_CORE, <adapter>/../../core, ~/.claude/communications)
and loads it by path. If the file cannot be found, the adapter refuses to run (exit 2). It never
runs with the guards off.

Nothing here touches sys.path. Nothing here reads a secret. Standard library only, Python 3.9
compatible. Importing this never raises.

The adapter passes in `load`, a function that returns a core module by name (or None). That
keeps the adapter in control of what it loads, and lets its tests swap in fakes.
"""
from __future__ import annotations

import importlib.util
import os
import sys

GATE_DENY_EXIT = 7
DEFAULT_WHATSAPP_SKILL = "~/.claude/skills/whatsapp"


# ---- finding and loading core modules -------------------------------------------------------
def core_dirs(env_name, candidates, home_dir, environ=None):
    """Existing folders, in the order CONTRIBUTING.md gives: the env variable, the adapter's own
    candidates (<file>/../../core), then the maintainer's live folder."""
    environ = os.environ if environ is None else environ
    paths = []
    if env_name and environ.get(env_name):
        paths.append(os.path.expanduser(environ[env_name]))
    paths += list(candidates)
    paths.append(os.path.expanduser(home_dir))
    out = []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p) and p not in out:
            out.append(p)
    return out


def load_module(name, dirs):
    """<dir>/<name>.py loaded by exact path from the first folder that has it. None when it
    cannot load. A module that fails halfway is not left in sys.modules."""
    for d in dirs:
        path = os.path.join(d, name + ".py")
        if not os.path.exists(path):
            continue
        have = sys.modules.get(name)
        if have is not None and getattr(have, "__file__", None) == path:
            return have
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
        except Exception:  # noqa: BLE001 - the caller decides what a missing module means
            sys.modules.pop(name, None)
    return None


# ---- the money check ------------------------------------------------------------------------
_draft_cache = {}


def _draft_module(path):
    if path in _draft_cache:
        return _draft_cache[path]
    name = "_channel_support_draft_check_%d" % len(_draft_cache)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        if not hasattr(module, "normalize_for_matching") or not hasattr(module, "MONEY"):
            raise AttributeError("draft_check has no money check")
    except Exception:  # noqa: BLE001
        sys.modules.pop(name, None)
        module = None
    _draft_cache[path] = module
    return module


def money_hits(text, here, extra_dirs=None):
    """Money terms found by the WhatsApp skill's draft_check, or None when the check cannot run.
    The caller must treat None as money (lock). draft_check is loaded by path: sys.path is never
    changed. The first draft_check file found decides; a broken one does not fall through."""
    dirs = [os.path.join(here, "..", "whatsapp")]  # kit: code path
    dirs += [os.path.expanduser(d) for d in (extra_dirs if extra_dirs is not None else [DEFAULT_WHATSAPP_SKILL])]
    for d in dirs:
        path = os.path.join(d, "draft_check.py")
        if not os.path.exists(path):
            continue
        module = _draft_module(os.path.abspath(path))
        if module is None:
            return None
        try:
            flat = module.normalize_for_matching(str(text or ""))
            return [m.group(0).strip() for m in module.MONEY.finditer(flat)]
        except Exception:  # noqa: BLE001 - a check that cannot run must lock, not pass
            return None
    return None


# ---- the outbound gate ----------------------------------------------------------------------
def gate_mode_fallback():
    """The gate mode when the gate module cannot load: GATE_MODE, else the gate_mode file, else
    report. A value that is not report or enforce means enforce (a typo must not turn it off)."""
    value = os.environ.get("GATE_MODE")
    if value is None:
        try:
            with open(os.path.expanduser("~/.claude/communications/gate_mode")) as fh:
                value = fh.read()
        except FileNotFoundError:
            value = "report"
        except OSError:
            value = "enforce"
    value = value.strip().lower()
    return value if value in ("report", "enforce") else "enforce"


def gate_check(load, mode_fallback, channel, money_ok, *, target, text, scope, thread, dry_run, source):
    """Ask the outbound gate. Returns (gate, payload, approvals), or None when the gate cannot load
    in report mode. Exits 7 on a refusal, or when the gate cannot load in enforce mode."""
    G = load("outbound_gate")
    if G is None:
        if mode_fallback() == "enforce":
            print("NOT SENT: outbound gate (enforce): the gate module could not load.")
            raise SystemExit(GATE_DENY_EXIT)
        sys.stderr.write("outbound gate: module could not load; report mode, continuing\n")
        return None
    approvals = G.Approvals.from_env(money_ok=bool(money_ok))
    payload = G.OutboundPayload(channel, target=target, texts=[text], recipients=[target],
                                scope=scope or "", thread=thread or "")
    verdict = G.evaluate(payload, approvals, dry_run=dry_run, source=source)
    if not verdict.allow:
        print(verdict.deny_line())
        raise SystemExit(GATE_DENY_EXIT)
    return (G, payload, approvals)


def gate_begin(ctx):
    """Reserve the send right before the transport call. None when there is no gate."""
    if ctx is None:
        return None
    G, payload, approvals = ctx
    try:
        return G.begin(payload, approvals)
    except G.GateDenied as exc:
        print(str(exc))
        raise SystemExit(GATE_DENY_EXIT)


def gate_finish(ctx, op, status, receipts=()):
    if ctx is not None and op:
        ctx[0].finish(op, status, [r for r in receipts if r])
