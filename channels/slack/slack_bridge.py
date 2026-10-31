#!/usr/bin/env python3
"""Small stdlib CLI for a self-hosted Slack bridge. Every personal value comes from kitconfig (slack.*)."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(os.path.abspath(__file__)).parent


# ---- config and core modules ------------------------------------------------------------------
def _core_dirs() -> list[str]:
    """Where the kit's shared modules live: COMMS_KIT_CORE, <this folder>/../../core, then the
    live Mac's ~/.claude/communications. abspath, never realpath, so a link keeps working."""
    paths = []
    if os.environ.get("COMMS_KIT_CORE"):
        paths.append(os.path.expanduser(os.environ["COMMS_KIT_CORE"]))
    paths += [str(HERE / ".." / ".." / "core"), os.path.expanduser("~/.claude/communications")]  # kit: code path
    return [p for p in dict.fromkeys(os.path.abspath(p) for p in paths) if os.path.isdir(p)]


def _comms_module(name: str):
    """core/<name>.py loaded by exact path (None when it cannot load)."""
    import importlib.util  # noqa: PLC0415
    for folder in _core_dirs():
        path = os.path.join(folder, name + ".py")
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
        except Exception:  # noqa: BLE001 - callers decide what a missing module means
            sys.modules.pop(name, None)
    return None


def _conf(key: str, default: Any = None) -> Any:
    kc = _comms_module("kitconfig")
    if kc is None:
        return default
    try:
        return kc.get(key, default)
    except kc.ConfigError as err:
        raise SystemExit(str(err)) from None


def _conf_need(key: str) -> str:
    """A config value the caller must have: stop with a one-line fix when it is missing."""
    kc = _comms_module("kitconfig")
    if kc is None:
        raise SystemExit("slack: kitconfig.py not found (set COMMS_KIT_CORE to the kit's core folder)")
    try:
        return str(kc.require(key))
    except kc.ConfigError as err:
        raise SystemExit(str(err)) from None


@dataclass(frozen=True)
class Config:
    repo_root: Path | None
    bridge_dir: Path
    bridge_url: str
    api_key: str


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# The old name of the repo-root variable held a person's name. It still works as an alias, built
# from parts so the name stays out of the source.
LEGACY_ROOT_ENV = "BR" "YCE_SYSTEMS_WORKSPACE"


def repo_root() -> Path | None:
    """The optional private workspace that holds .env, the outbound guards and the BI sync script.
    SLACK_BRIDGE_REPO_ROOT, else the legacy variable, else config slack.repo_root, else None."""
    value = (os.environ.get("SLACK_BRIDGE_REPO_ROOT") or os.environ.get(LEGACY_ROOT_ENV)
             or _conf("slack.repo_root"))
    return Path(str(value)).expanduser() if value else None


def local_bridge_dir(root: Path | None) -> Path:
    """The local bridge checkout (for `pair`): config slack.bridge_dir, else <root>/.mcp-servers/slack-bridge,
    else the bridge/ folder next to this file."""
    value = _conf("slack.bridge_dir")
    if value:
        return Path(str(value)).expanduser()
    return root / ".mcp-servers/slack-bridge" if root else HERE / "bridge"  # kit: code path


def default_bridge_url(root: Path) -> str:
    deploy_path = root / "apps/vps/deploy.json"
    deploy = json.loads(deploy_path.read_text(encoding="utf-8"))
    hostname = deploy["server"]["hostname"]
    prefix = deploy["services"]["slack-bridge"].get("path_prefix", "")
    return f"https://{hostname}{prefix}".rstrip("/")


def load_config() -> Config:
    root = repo_root()
    bridge_dir = local_bridge_dir(root)
    load_env_file(bridge_dir / ".env")
    if root:
        load_env_file(root / ".env")
    bridge_url = (os.environ.get("SLACK_BRIDGE_URL") or os.environ.get("REMOTE_BRIDGE_URL")
                  or _conf("slack.bridge_url") or (default_bridge_url(root) if root else ""))
    if not bridge_url:
        raise SystemExit("slack: set slack.bridge_url in the kit config (or SLACK_BRIDGE_URL)")
    api_key = os.environ.get("BRIDGE_API_KEY", "")
    if not api_key:
        raise SystemExit("BRIDGE_API_KEY missing from Slack bridge .env or repo .env")
    return Config(root, bridge_dir, bridge_url.rstrip("/"), api_key)


def json_print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


# ---- Outbound guards (opt-in, off by default) ------------------------------------------------
# These mirror the WhatsApp wrapper's wiring. The guard LOGIC lives once in
# <repo_root>/execution/personal_workflows/outbound_guards.py (tested there and by
# the JS drafting guards); here we only gate the send path on it. Everything is fail-closed:
# if OUTBOUND_GUARDS=1 but the guard cannot run, the send is refused, never silently allowed.

def _guards_on() -> bool:
    return os.environ.get("OUTBOUND_GUARDS") == "1"


def _load_outbound_guards():
    """Import the shared guard module from the systems workspace. One source of truth with the
    WhatsApp wrapper; the guards live in the private workspace named by slack.repo_root."""
    root = repo_root()
    if root is None:
        raise RuntimeError("slack.repo_root is not set, so outbound_guards.py cannot be found")
    path = str(root / "execution/personal_workflows")
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module("outbound_guards")


def _apply_recipient_guard(channel: str, exclude: list[str] | None) -> None:
    """Guard 2: refuse a send with no real destination, or to an excluded channel/id."""
    if not _guards_on():
        return
    try:
        guards = _load_outbound_guards()
    except Exception as err:  # noqa: BLE001 - fail-closed on any import failure
        raise SystemExit(f"OUTBOUND_GUARDS=1 but the guard module failed to load ({err}); refusing to send")
    # A Slack send targets exactly one channel/DM id, i.e. one intended destination.
    verdict = guards.evaluate_recipient([channel], group_intended=True, exclude=exclude or [])
    if not verdict["allow"]:
        raise SystemExit(f"recipient guard refused this send: {verdict['reason']}")


def _report_send_result(resp: Any) -> None:
    """Print the bridge response, and under the flag refuse to treat a non-ok result as sent
    (Guard 3, delivery receipt). Mirrors _report_sent in whatsapp_mautrix.py."""
    json_print(resp)
    if not _guards_on():
        return
    try:
        guards = _load_outbound_guards()
    except Exception as err:  # noqa: BLE001 - fail-closed on any import failure
        raise SystemExit(f"OUTBOUND_GUARDS=1 but the guard module failed to load ({err}); cannot verify delivery")
    verdict = guards.delivery_verdict("slack", json.dumps(resp))
    if verdict["status"] != "sent":
        raise SystemExit(
            f"delivery guard: Slack send NOT confirmed ({verdict['status']}: {verdict['reason']}) — "
            "do not report this as sent"
        )


def request_json(config: Config, path: str, params: dict[str, Any] | None = None, method: str = "GET", body: dict[str, Any] | None = None, timeout: int = 30) -> Any:
    url = f"{config.bridge_url}/{path.lstrip('/')}"
    if params:
        clean_params = {k: v for k, v in params.items() if v is not None}
        if clean_params:
            url += "?" + urllib.parse.urlencode(clean_params)
    data = None
    headers = {"X-Api-Key": config.api_key}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    verify_tls = os.environ.get("SLACK_BRIDGE_VERIFY_TLS") == "1"
    context = None if verify_tls else ssl._create_unverified_context()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=context, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Slack bridge HTTP {err.code}: {detail}") from err
    except urllib.error.URLError as err:
        raise SystemExit(f"Slack bridge unreachable at {config.bridge_url}: {err.reason}") from err
    return json.loads(payload.decode("utf-8"))


def download_file(config: Config, file_id: str, workspace: str | None, out_dir: Path) -> None:
    params = {"workspace": workspace} if workspace else None
    url = f"{config.bridge_url}/files/{urllib.parse.quote(file_id)}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    verify_tls = os.environ.get("SLACK_BRIDGE_VERIFY_TLS") == "1"
    context = None if verify_tls else ssl._create_unverified_context()
    req = urllib.request.Request(url, headers={"X-Api-Key": config.api_key})
    try:
        with urllib.request.urlopen(req, context=context, timeout=60) as response:
            name = file_id
            disposition = response.headers.get("Content-Disposition", "")
            if "filename=" in disposition:
                name = disposition.split("filename=", 1)[1].strip().strip('"') or file_id
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / Path(name).name
            target.write_bytes(response.read())
            print(target)
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Slack bridge HTTP {err.code}: {detail}") from err
    except urllib.error.URLError as err:
        raise SystemExit(f"Slack bridge unreachable at {config.bridge_url}: {err.reason}") from err


def flatten_workspaces(values: list[str]) -> str:
    slugs: list[str] = []
    for value in values:
        slugs.extend(part.strip() for part in value.split(",") if part.strip())
    if not slugs:
        raise SystemExit("at least one workspace slug is required")
    return ",".join(dict.fromkeys(slugs))


def run_sync(config: Config, args: argparse.Namespace) -> None:
    if config.repo_root is None:
        raise SystemExit("sync: set slack.repo_root (the workspace that holds bi_sync_slack.py)")
    cmd = ["python3", "execution/infrastructure/bi_sync_slack.py", "--days", str(args.days)]
    if args.dry_run:
        cmd.append("--dry-run")
    env = {**os.environ, "SLACK_BRIDGE_URL": config.bridge_url}
    raise SystemExit(subprocess.run(cmd, cwd=config.repo_root, env=env).returncode)


def run_pair(config: Config, args: argparse.Namespace) -> None:
    workspaces = flatten_workspaces(args.workspaces)
    cmd = ["npm", "run", "sync", "--", "--workspace", workspaces]
    if args.sign_in:
        cmd.append("--sign-in")
    if not args.sync_after:
        cmd.append("--no-sync")
    raise SystemExit(subprocess.run(cmd, cwd=config.bridge_dir).returncode)


# Thread replies never reach the bridge buffer (it polls conversations.history only), so
# `threads` runs scripts/slack-thread-read.js on the VPS with the bridge's own token store.
# The script goes over ssh stdin on every run: no remote copy to go stale or vanish on reboot.
THREAD_SCRIPT = Path(__file__).resolve().parent / "scripts" / "slack-thread-read.js"
def _ssh_host() -> str:
    """The ssh alias or host of the server that runs the bridge: SLACK_BRIDGE_SSH_HOST, else config slack.ssh_host."""
    return os.environ.get("SLACK_BRIDGE_SSH_HOST") or _conf_need("slack.ssh_host")


def _remote_prefix() -> str:
    """Start of every remote command: enter the bridge folder and tell the piped script where it is
    (config slack.vps_bridge_dir). The scripts hold no path of their own."""
    folder = shlex.quote(_conf_need("slack.vps_bridge_dir"))
    return f"cd {folder} && SLACK_BRIDGE_DIR={folder}"


def _local_zone() -> str:
    """The Mac's IANA zone (macOS sets it by location, so it follows travel). UTC if unreadable."""
    try:
        zone = os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "UTC"
    return zone if re.fullmatch(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*", zone) else "UTC"


def _thread_script(channel: str, workspace: str, limit: int, mode: str = "",
                   timeout: int = 180, extra: str | None = None) -> subprocess.CompletedProcess:
    if not re.fullmatch(r"[A-Z0-9]{8,15}", channel):
        raise SystemExit(f"not a Slack channel/DM ID: {channel}")
    if extra is not None and not SLACK_TS_RE.fullmatch(str(extra)):
        raise SystemExit(f"not a Slack message ts: {extra}")
    limit = max(1, min(limit, 200))
    # TZ: the VPS's own zone is not where the owner is; the reader prints times in the Mac's zone.
    remote = (f"{_remote_prefix()} TZ={shlex.quote(_local_zone())} node - {shlex.quote(channel)} "
              f"{shlex.quote(workspace)} {limit}" + (f" {shlex.quote(mode)}" if mode else "")
              + (f" {shlex.quote(str(extra))}" if (mode and extra is not None) else ""))
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", _ssh_host(), remote]
    try:
        return subprocess.run(cmd, input=THREAD_SCRIPT.read_text(), text=True,
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"threads: no answer from {_ssh_host()} within {timeout}s")


def run_threads(args: argparse.Namespace) -> None:
    # --json: one JSON object with every message AND thread reply (`comms unread` reads it).
    workspace = args.workspace
    if not workspace:
        try:
            workspace = _workspace_of(load_config(), args.channel)
        except RuntimeError as exc:
            raise SystemExit(f"threads: {exc}")
    if getattr(args, "full", False):
        return _threads_full(args, workspace)
    if getattr(args, "before", None):
        raise SystemExit("threads: --before needs --full")
    proc = _thread_script(args.channel, workspace, args.limit, mode="feed" if args.json else "")
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"threads failed on {_ssh_host()} (exit {proc.returncode})")


# ---- P0 (2026-10-30): full read, outbound gate, CLI logging ---------------------------------
GATE_DENY_EXIT = 7          # outbound gate refused in enforce mode; nothing was sent


def _gate_mode_fallback() -> str:
    value = os.environ.get("GATE_MODE")
    if value is None:
        kc = _comms_module("kitconfig")
        try:
            value = (Path(kc.data_dir()) / "gate_mode").read_text() if kc else "enforce"
        except FileNotFoundError:
            value = "report"
        except Exception:  # noqa: BLE001 - unreadable mode or config: stay strict
            value = "enforce"
    value = value.strip().lower()
    return value if value in ("report", "enforce") else "enforce"


def _gate_check(args: argparse.Namespace, *, target: str, texts: list[str], scope: str = "",
                thread: str = "", filenames: list[str] | None = None,
                attachment_texts: list | None = None, unread: list[str] | None = None,
                hashes: list | None = None, dry_run: bool, source: str):
    """Outbound gate on the FINAL text. Enforce mode refusing -> exit 7, nothing sent."""
    G = _comms_module("outbound_gate")
    if G is None:
        if _gate_mode_fallback() == "enforce":
            print("NOT SENT: outbound gate (enforce): the gate module could not load.")
            raise SystemExit(GATE_DENY_EXIT)
        sys.stderr.write("outbound gate: module could not load; report mode, continuing\n")
        return None
    approvals = G.Approvals.from_env(money_ok=bool(getattr(args, "money_ok", False)),
                                     override_reason=getattr(args, "override_check", None) or "",
                                     resend_ok=bool(getattr(args, "resend_ok", False)))
    payload = G.OutboundPayload("slack", target=target, texts=[t for t in texts if t], scope=scope or "",
                                thread=thread or "", filenames=list(filenames or []),
                                attachment_texts=list(attachment_texts or []),
                                unread_attachments=list(unread or []),
                                attachment_hashes=[h for h in (hashes or []) if h])
    verdict = G.evaluate(payload, approvals, dry_run=dry_run, source=source)
    if not verdict.allow:
        print(verdict.deny_line())
        raise SystemExit(GATE_DENY_EXIT)
    return (G, payload, approvals)


def _gate_begin(ctx):
    if ctx is None:
        return None
    G, payload, approvals = ctx
    try:
        return G.begin(payload, approvals)
    except G.GateDenied as e:
        print(str(e))
        raise SystemExit(GATE_DENY_EXIT)


def _gate_finish(ctx, op, status: str, receipts=()) -> None:
    if ctx is not None and op:
        ctx[0].finish(op, status, [r for r in receipts if r])


def _post(config: Config, path: str, body: dict[str, Any], ctx, op, timeout: int = 30) -> Any:
    """A proxy/HTTP failure cannot prove Slack did not receive the request."""
    try:
        resp = request_json(config, path, method="POST", body=body, timeout=timeout)
    except SystemExit:
        _gate_finish(ctx, op, "uncertain")
        raise
    except BaseException:
        _gate_finish(ctx, op, "uncertain")
        raise
    ok = isinstance(resp, dict) and resp.get("ok") is True
    _gate_finish(ctx, op, "sent" if ok else "uncertain", [str(resp.get("ts"))] if ok and resp.get("ts") else [])
    return resp


def _log_send(channel: str, text: str | None, resp: Any, verb: str, files: list[str] | None = None,
              workspace: str | None = None) -> None:
    C = _comms_module("commslib")
    if C is None or not isinstance(resp, dict) or resp.get("ok") is not True:
        return
    C.log_event("slack", "send", channel, text=text, message_id=resp.get("ts"),
                scope=f"{workspace}:{resp.get('channel') or channel}" if workspace else None,
                verb=verb, files=files,
                command=" ".join(sys.argv))


def _slack_message(S, row: dict[str, Any], channel: str, me: str | None, parents: dict[str, Any]):
    """One reader `full` row -> msgschema Message (nothing dropped: extras go to `extra`)."""
    ts = str(row.get("ts") or "")
    msg = S.Message(id=ts, chat_id=channel, channel="slack", sender=row.get("name") or "?",
                    sender_id=row.get("user") or row.get("bot_id"),
                    is_from_me=bool(me) and row.get("user") == me,
                    ts=float(ts) if ts else None, text=row.get("text") or "")
    msg.deleted = row.get("subtype") in ("tombstone", "message_deleted")
    for f in row.get("files") or []:
        msg.attachments.append(S.Attachment(id=f.get("id"), name=f.get("name"), mime=f.get("mimetype"),
                                            size=f.get("size"), transcript_status="not_read_here"))
    if row.get("files"):
        msg.extra["transcribe_hint"] = "slack_bridge.py transcribe FILE_ID --workspace WS"
    for r in row.get("reactions") or []:
        msg.reactions.append(S.Reaction(key=r.get("key") or "", sender=r.get("sender") or r.get("sender_id") or "?"))
    parent = row.get("thread_ts")
    if parent:
        p = parents.get(parent)
        msg.reply_to = S.ReplyRef(id=parent, text=(p or {}).get("text"), incomplete=p is None)
    msg.extra["ts_raw"] = ts
    for key in ("subtype", "thread_ts", "reply_count", "latest_reply", "edited", "bot_id", "blocks_text"):
        if row.get(key):
            msg.extra[key] = row[key]
    if row.get("attachments"):
        msg.extra["attachments"] = row["attachments"]
    if row.get("replies_capped"):
        msg.truncated = True
        msg.more = "this thread has more than 2000 replies; read the rest in Slack"
    return msg


def _threads_full(args: argparse.Namespace, workspace: str) -> None:
    S = _comms_module("msgschema")
    if S is None:
        raise SystemExit("threads --full: msgschema.py could not load from the kit core folder")
    proc = _thread_script(args.channel, workspace, args.limit, mode="full", extra=args.before)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"threads failed on {_ssh_host()} (exit {proc.returncode})")
    try:
        raw = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise SystemExit("threads --full: the reader gave no JSON") from None
    rows = raw.get("messages") or []
    parents = {str(r.get("ts")): r for r in rows if not r.get("thread_ts")}
    messages = [_slack_message(S, r, args.channel, raw.get("me"), parents) for r in rows]
    tops = [r for r in rows if not r.get("thread_ts")]
    end = bool(raw.get("end_reached"))
    capped = any(r.get("replies_capped") for r in rows)
    oldest = str(tops[0]["ts"]) if tops else None
    more = f"--before {oldest}" if (oldest and not end) else None
    env = S.envelope("slack", {"id": args.channel, "workspace": workspace}, messages,
                     complete=not capped, truncated=capped, more=more,
                     requested_top_level=args.limit,
                     scope="the N newest top-level messages and all their replies")
    print(S.dumps(env))


def _thread_messages(config: Config, channel: str, workspace: str | None, thread_ts: str) -> list[dict[str, Any]]:
    """The thread's replies as Slack has them NOW. Raises when the thread cannot be read or its
    root is not a message in this channel."""
    workspace = workspace or _workspace_of(config, channel)
    proc = _thread_script(channel, workspace, 1, mode="thread", timeout=60, extra=thread_ts)
    if proc.returncode != 0:
        raise RuntimeError(f"live thread read failed on {_ssh_host()}: {_one_line(proc.stderr, 200)}")
    try:
        raw = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise RuntimeError(f"live thread read gave no JSON: {_one_line(proc.stdout, 120)!r}") from None
    if not raw.get("found_root"):
        raise LookupError(f"{thread_ts} is not a message in {channel}")
    if raw.get("capped"):
        raise RuntimeError("the thread has more than 2000 replies; cannot check it fully")
    return raw.get("messages") or []


def thread_unchanged_guard(config: Config, channel: str, workspace: str | None, since: float | None,
                           thread_ts: str) -> None:
    """Same contract as chat_unchanged_guard, scoped to one thread. Replies are compared as exact
    decimals, strictly newer, no tolerance (a same-second reply must stop a second answer)."""
    from decimal import Decimal  # noqa: PLC0415
    try:
        msgs = _thread_messages(config, channel, workspace, thread_ts)
    except LookupError as err:
        print(f"NOT SENT: {err}.")
        raise SystemExit(STALE_EXIT)
    except (RuntimeError, SystemExit, ValueError, OSError) as err:
        if since is None:
            print(f"  (could not read the thread: {err})")
            return
        print(f"NOT SENT: could not re-read the thread before sending ({err}).")
        raise SystemExit(STALE_EXIT)
    anchor = Decimal(thread_ts)
    if msgs:
        top = max(msgs, key=lambda m: Decimal(str(m.get("ts") or "0")))
        anchor = Decimal(str(top.get("ts")))
        print(f"  newest reply in this thread: [{_et(float(anchor))}] {top.get('name') or '?'}: "
              f"{_one_line(top.get('text'), 80)}  (--since {anchor})")
    else:
        print(f"  no reply in this thread yet  (--since {thread_ts})")
    if since is None:
        return
    cut = Decimal(f"{since:.6f}")
    newer = [m for m in msgs if Decimal(str(m.get("ts") or "0")) > cut]
    if newer:
        print(f"NOT SENT: {len(newer)} newer reply(ies) in this thread since the one you read:")
        for m in sorted(newer, key=lambda m: Decimal(str(m.get("ts")))):
            print(f"  [{_et(float(m['ts']))}] {m.get('name') or '?'}: {_one_line(m.get('text'), 200)}")
        print("Re-read the thread, redraft, and get a fresh yes before sending.")
        raise SystemExit(STALE_EXIT)


def _read_logged(args: argparse.Namespace) -> None:
    """Run a READ command; outside a hook runtime (no CLAUDECODE, no CLAUDE_CODE_SESSION_ID:
    Claude Code and pi's Claude-hooks bridge log reads in their hook) and outside comms' own
    scans (no COMMS_INTERNAL), archive what it printed. stdout passes through unchanged."""
    if (os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("COMMS_INTERNAL")):
        return handle(args)
    C = _comms_module("commslib")
    if C is None or C.VERBS.get("slack", {}).get(args.command) != "read":
        return handle(args)
    tee = C.StdoutTee(sys.stdout)
    sys.stdout, ok = tee, False
    try:
        handle(args)
        ok = True
    except SystemExit as e:
        ok = e.code in (None, 0)
        raise
    finally:
        sys.stdout = tee._s
        target = getattr(args, "channel", None) or getattr(args, "file_id", None)
        if ok:
            C.log_event("slack", "read", target, body=tee.getvalue(), verb=args.command,
                        command=" ".join(sys.argv))


# Reactions: the bridge has no route for them, so `react` pipes scripts/slack-react.js
# over ssh, the same way `threads` does. Its only writes are reactions.add/remove.
REACT_SCRIPT = Path(__file__).resolve().parent / "scripts" / "slack-react.js"
MONEY_LOCK_EXIT = 6          # same code as whatsapp_mautrix.py's react money lock
SLACK_TS_RE = re.compile(r"\d{9,11}\.\d{1,6}")
SLACK_NAME_RE = re.compile(r"[a-z0-9_+'-]{1,100}")
# The owner's own habits (WhatsApp, measured 2026-10-28): ❤️ 75%, 👍 12%, then 😂 and 🙏.
REACTION_ALIASES = {
    "heart": "heart", "love": "heart",
    "thumbsup": "+1", "thumbs-up": "+1", "thumbs_up": "+1", "like": "+1", "+1": "+1",
    "laugh": "joy", "haha": "joy", "lol": "joy", "joy": "joy",
    "pray": "pray", "thanks": "pray", "fire": "fire", "wow": "open_mouth", "sad": "cry",
    "check": "white_check_mark", "clap": "clap", "raised_hands": "raised_hands",
    "eyes": "eyes", "100": "100", "party": "tada", "tada": "tada",
}
EMOJI_TO_SLACK = {
    "❤": "heart", "👍": "+1", "😂": "joy", "🙏": "pray", "🔥": "fire", "😮": "open_mouth",
    "😢": "cry", "✅": "white_check_mark", "👏": "clap", "🙌": "raised_hands", "👀": "eyes",
    "💯": "100", "🎉": "tada", "😊": "blush", "🤣": "rolling_on_the_floor_laughing",
}


def _strip_emoji_modifiers(text: str) -> str:
    return "".join(ch for ch in text if ord(ch) not in (0xFE0E, 0xFE0F)
                   and not 0x1F3FB <= ord(ch) <= 0x1F3FF)


def _slack_reaction_name(value: Any) -> str:
    """An alias, a known raw emoji, or `:name:` -> the Slack reaction name.
    Raises ValueError on a word, two emojis, or an emoji with no known name."""
    text = str(value or "").strip()
    if text.lower() in REACTION_ALIASES:
        return REACTION_ALIASES[text.lower()]
    if text.startswith(":") and text.endswith(":") and SLACK_NAME_RE.fullmatch(text[1:-1]):
        return text[1:-1]
    name = EMOJI_TO_SLACK.get(_strip_emoji_modifiers(text))
    if name:
        return name
    raise ValueError(f"a reaction is ONE known emoji, a name like heart, thumbsup, laugh, "
                     f"pray, or a Slack :name: (got {value!r})")


def _norm_slack_name(name: str) -> str:
    """Slack's spellings of one reaction: `thumbsup` is `+1`, and a skin tone is
    stored as `+1::skin-tone-3`."""
    base = str(name or "").split("::", 1)[0]
    return {"thumbsup": "+1", "thumbsdown": "-1"}.get(base, base)


def _react_call(channel: str, workspace: str, ts: str, name: str, mode: str,
                timeout: int = 90) -> dict[str, Any]:
    """Run slack-react.js on the VPS. Returns its JSON object (ok False on failure)."""
    remote = (f"{_remote_prefix()} node - {shlex.quote(channel)} {shlex.quote(workspace)} "
              f"{shlex.quote(ts)} {shlex.quote(name)} {shlex.quote(mode)}")
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", _ssh_host(), remote]
    try:
        proc = subprocess.run(cmd, input=REACT_SCRIPT.read_text(), text=True,
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"no answer from {_ssh_host()} within {timeout}s"}
    line = (proc.stdout.strip().splitlines() or [""])[-1]
    try:
        return json.loads(line)
    except ValueError:
        return {"ok": False, "error": (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:400]}


def _money_hits(text: str) -> list[str] | None:
    """The WhatsApp skill's draft_check.py MONEY terms in `text`, or None when the
    check cannot run (the caller then locks: a reaction on a price reads as a yes)."""
    try:
        # draft_check.py sits in the WhatsApp channel folder, next to this one in both layouts.
        for folder in (HERE.parent / "whatsapp", Path.home() / ".claude" / "skills" / "whatsapp"):  # kit: code path
            if (folder / "draft_check.py").exists():
                sys.path.insert(0, str(folder))
                break
        import draft_check  # noqa: PLC0415
        flat = draft_check.normalize_for_matching(str(text or ""))
        return [m.group(0).strip() for m in draft_check.MONEY.finditer(flat)]
    except Exception:  # noqa: BLE001 — a check that cannot run must lock, not pass
        return None


def run_react(args: argparse.Namespace) -> None:
    """React to ONE Slack message (by channel + ts), or remove your reaction.
    Always previews first. The ts pins the message, so a newer message can never
    become the target."""
    _apply_recipient_guard(args.channel, getattr(args, "exclude", None))
    if not SLACK_TS_RE.fullmatch(str(args.ts or "")):
        print(f"NOT SENT: {args.ts!r} is not a Slack message ts (history/threads print it, "
              "like 1790190627.123456).")
        raise SystemExit(1)
    name = None
    if args.emoji or not args.remove:
        try:
            name = _slack_reaction_name(args.emoji)
        except ValueError as exc:
            print(f"NOT SENT: {exc}")
            raise SystemExit(1)
    workspace = args.workspace
    if not workspace:
        try:
            workspace = _workspace_of(load_config(), args.channel)
        except RuntimeError as exc:
            raise SystemExit(f"NOT SENT: {exc}")

    info = _react_call(args.channel, workspace, args.ts, name or "-", "check")
    if not info.get("ok"):
        print(f"NOT SENT: could not read that message ({info.get('error')}).")
        raise SystemExit(1)
    msg = info.get("message") or {}
    text = msg.get("text") or ""
    self_id = info.get("self")
    reactions = info.get("reactions") or []
    mine = [r["name"] for r in reactions if self_id and self_id in (r.get("users") or [])]
    who = "your" if msg.get("user") == self_id else f"{msg.get('name') or msg.get('user')}'s"
    action = (f"remove :{name or (mine[0] if mine else '?')}: from" if args.remove
              else f"react :{name}: to")
    print(f'About to {action} {who} message in {args.channel} ({workspace}): '
          f'"{_one_line(text, 120)}"  [ts {args.ts}]')
    if reactions:
        print("  reactions there now: " + ", ".join(
            f":{r['name']}: x{r.get('count', 1)}" + (" (you)" if r["name"] in mine else "")
            for r in reactions))

    if args.remove:
        if name is None:
            if len(mine) != 1:
                print("NOT SENT: " + ("you have no reaction on this message." if not mine else
                      f"you have {len(mine)} reactions here ({', '.join(mine)}); name the one to remove."))
                raise SystemExit(1 if mine else 0)
            name = mine[0]
        hit = [m for m in mine if _norm_slack_name(m) == _norm_slack_name(name)]
        if not hit:
            print(f"NOT SENT: you have no :{name}: on this message.")
            return
        if not args.yes:
            print("Dry run — re-run with --yes to remove it.")
            return
        res = _react_call(args.channel, workspace, args.ts, hit[0], "remove")
        if not res.get("ok"):
            print(f"NOT SENT: Slack refused the removal ({res.get('error')}).")
            raise SystemExit(4)
        print(f"Removed :{hit[0]}: from ts {args.ts}.")
        return

    if any(_norm_slack_name(m) == _norm_slack_name(name) for m in mine):
        print(f"NOT SENT: you already reacted :{name}: to this message. Nothing to do.")
        return
    hits = _money_hits(text)
    locked = hits is None or bool(hits)
    if hits is None:
        print("  MONEY LOCK: the money check (draft_check.py) could not run, so treat this message "
              "as money. Show the owner the message, get a fresh yes, then add --money-ok.")
    elif hits:
        print(f"  MONEY LOCK: the message mentions {', '.join(sorted(set(hits)))}. A reaction can "
              "read as a yes. Show the owner the message, get a fresh yes, then add --money-ok.")
    if "?" in text:
        print("  note: this message asks a question. A reaction does not answer it.")
    if not args.yes:
        print("Dry run — re-run with --yes to react" + (" (and --money-ok)" if locked else "") + ".")
        return
    if locked and not args.money_ok:
        print("NOT SENT: money lock. Show the owner the message, get a fresh yes, then add --money-ok.")
        raise SystemExit(MONEY_LOCK_EXIT)
    res = _react_call(args.channel, workspace, args.ts, name, "add")
    if not res.get("ok"):
        print(f"NOT SENT: Slack refused the reaction ({res.get('error')}).")
        raise SystemExit(4)
    if res.get("noop"):
        print(f"NOT SENT: you already reacted :{name}: to this message. Nothing to do.")
        return
    print(f"Reacted :{name}: on ts {args.ts}.")


# `send --since`: the channel moved after the message the caller read, or it could not
# be re-read. Nothing was sent. Same code as whatsapp_mautrix.py's STALE_EXIT.
STALE_EXIT = 5


def parse_since(value: str) -> float:
    """--since: a Slack ts / epoch seconds (the send preview prints it), epoch ms, or
    `empty` (0.0: the channel had no message; refused if one is there now). A
    minute-precision clock time is refused: a message in the same minute would hide."""
    if str(value).strip().lower() == "empty":
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"--since needs a Slack ts or epoch seconds (the preview prints it), got {value!r}") from None
    if v > 1e12:
        v /= 1000.0
    if v <= 0:
        raise argparse.ArgumentTypeError("--since must be a real time, not 0")
    return v


def _activity(m: dict[str, Any]) -> float:
    return max(float(m.get("ts") or 0), float(m.get("latest_reply") or 0))


def newer_than(messages: list[dict[str, Any]], since: float, tolerance: float = 1.0) -> list[dict[str, Any]]:
    """Top-level messages, or thread replies, newer than `since`, oldest first. Pure.
    Any sender counts: you replying from your phone, or in a thread the bridge buffer
    never sees, must stop a second answer just as a new message from them must."""
    cut = since + tolerance
    out = []
    for m in messages or []:
        ts, reply = float(m.get("ts") or 0), float(m.get("latest_reply") or 0)
        if ts > cut:
            out.append({"ts": ts, "who": m.get("name") or "?", "text": m.get("text") or ""})
        elif reply > cut:
            out.append({"ts": reply, "who": "a thread reply",
                        "text": "under: " + (m.get("text") or "")})
    return sorted(out, key=lambda x: x["ts"])


def _et(ts: float) -> str:
    # The Mac's current zone, not a fixed one: the owner travels and macOS sets it by location.
    from datetime import datetime
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _one_line(text: Any, cap: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _workspace_of(config: Config, channel: str) -> str:
    """The one paired workspace whose channel/DM list holds `channel`. Raises when none
    or several do, so a read never lands on the wrong workspace."""
    hits = {str(r["workspace"]) for r in request_json(config, "/channels")
            if r.get("id") == channel and r.get("workspace")}
    if len(hits) != 1:
        raise RuntimeError(f"cannot tell which workspace has {channel} ({len(hits)} match); pass --workspace")
    return hits.pop()


def _live_messages(config: Config, channel: str, workspace: str | None) -> list[dict[str, Any]]:
    """The channel as Slack has it NOW (conversations.history over ssh, about 4s), not
    the bridge buffer and not the comms KB copy /inbox scans (20-40 minutes behind)."""
    workspace = workspace or _workspace_of(config, channel)
    proc = _thread_script(channel, workspace, 20, mode="json", timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"live read failed on {_ssh_host()}: {_one_line(proc.stderr, 200)}")
    try:
        msgs = json.loads(proc.stdout.strip().splitlines()[-1])["messages"]
    except (IndexError, KeyError, TypeError, ValueError):
        raise RuntimeError(f"live read gave no JSON: {_one_line(proc.stdout, 120)!r}") from None
    if not isinstance(msgs, list):
        raise RuntimeError("live read gave no message list")
    return msgs


def print_next_since(resp: Any) -> None:
    """After a send, print the sent message's Slack ts as `next --since`, so the NEXT
    message of the same reply is checked against it. No ts: say so, and the caller
    must not send the next message unchecked."""
    ts = str(resp.get("ts") or "") if isinstance(resp, dict) else ""
    try:
        print(f"next --since {float(ts):.6f}")
    except ValueError:
        print("next --since unknown (the bridge returned no message ts)")


def chat_unchanged_guard(config: Config, channel: str, workspace: str | None, since: float | None) -> None:
    """Re-read the channel live right before a send. Always prints the newest activity
    and the `--since` value that pins it, so the dry run doubles as the read. With
    `since`, refuses (STALE_EXIT) when anything is newer, and also when the channel
    cannot be read or shows no message: a re-read that sees nothing never passes."""
    try:
        msgs = _live_messages(config, channel, workspace)
    except (RuntimeError, SystemExit, ValueError, OSError) as err:
        if since is None:
            print(f"  (could not read the newest message: {err})")
            return
        print(f"NOT SENT: could not re-read the channel before sending ({err}).")
        raise SystemExit(STALE_EXIT)
    if msgs:
        top = max(msgs, key=_activity)
        print(f"  newest message here: [{_et(_activity(top))}] {top.get('name') or '?'}: "
              f"{_one_line(top.get('text'), 80)}  (--since {_activity(top):.6f})")
    else:
        print("  no message visible in this channel  (--since empty)")
    if since is None:
        return
    if not msgs:
        if since == 0.0:
            return                      # `--since empty`, and it still is
        print("NOT SENT: the re-read shows no message in this channel, so --since cannot be checked.")
        raise SystemExit(STALE_EXIT)
    newer = newer_than(msgs, since)
    if newer:
        print(f"NOT SENT: {len(newer)} newer message(s) in this channel since the one you read:")
        for m in newer:
            print(f"  [{_et(m['ts'])}] {m['who']}: {_one_line(m['text'], 200)}")
        print("Re-read the channel (`threads` for replies), redraft, and get a fresh yes before sending.")
        raise SystemExit(STALE_EXIT)


# ---- Admin verbs (2026-10-31): open-dm, find, mentions, edit, delete, channel-* ---------------
# The bridge has no route for these, so they pipe scripts/slack-admin.js over ssh stdin, like
# `react`. The script runs ONLY the methods in ADMIN_METHODS (checked here AND there). No file on
# the VPS changes, and the bridge is not restarted. Every write verb is a dry run unless --yes.
ADMIN_SCRIPT = Path(__file__).resolve().parent / "scripts" / "slack-admin.js"
ADMIN_METHODS = frozenset({
    "conversations.open", "chat.update", "chat.delete", "search.messages",
    "conversations.create", "conversations.invite", "conversations.kick",
    "conversations.rename", "conversations.leave", "conversations.archive",
    "conversations.members", "conversations.info", "auth.test",
})
ADMIN_FAIL_EXIT = 4          # Slack (or the box) said no, same code as react's refusal
SLACK_ID_RE = re.compile(r"[A-Z0-9]{8,15}")
SLACK_USER_RE = re.compile(r"[UW][A-Z0-9]{8,15}")
SLACK_CHANNEL_NAME_RE = re.compile(r"[a-z0-9_-]{1,80}")
BRIDGE_CHANNEL_REFRESH_MINUTES = 15       # lib/poller.js channelRefreshMs (a failing list stretches it, up to 2h)


def _admin_call_impl(workspace: str, method: str, params: dict[str, Any] | None = None,
                     timeout: int = 90) -> dict[str, Any]:
    """Run ONE allow-listed Slack method through slack-admin.js on the VPS. Returns the script's
    JSON ({ok, result} or {ok False, error}); `uncertain` is True when we cannot tell whether
    Slack acted (ssh timeout, no JSON). A method outside ADMIN_METHODS raises SystemExit
    before anything leaves this Mac."""
    if method not in ADMIN_METHODS:
        raise SystemExit(f"slack-admin: method {method!r} is not on the allow-list; refused")
    b64 = base64.b64encode(json.dumps(params or {}).encode("utf-8")).decode("ascii")
    remote = (f"{_remote_prefix()} node - {shlex.quote(workspace)} {shlex.quote(method)} {shlex.quote(b64)}")
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", _ssh_host(), remote]
    try:
        proc = subprocess.run(cmd, input=ADMIN_SCRIPT.read_text(), text=True,
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "uncertain": True, "error": f"no answer from {_ssh_host()} within {timeout}s"}
    line = (proc.stdout.strip().splitlines() or [""])[-1]
    try:
        res = json.loads(line)
        if isinstance(res, dict) and "ok" in res:
            return res
    except ValueError:
        pass
    return {"ok": False, "uncertain": True,
            "error": (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:400]}


def _admin_call(workspace: str, method: str, params: dict[str, Any] | None = None,
                timeout: int = 90) -> dict[str, Any]:
    return _admin_call_impl(workspace, method, params, timeout)


def _admin_fail(what: str, res: dict[str, Any], code: int = ADMIN_FAIL_EXIT) -> None:
    """Print a Slack refusal and exit non-zero. Never reads as done."""
    note = " Slack may or may not have acted; check before retrying." if res.get("uncertain") else ""
    print(f"{what} ({res.get('error')}).{note}")
    raise SystemExit(code)


def _admin_ok(workspace: str, method: str, params: dict[str, Any], what: str) -> dict[str, Any]:
    res = _admin_call(workspace, method, params)
    if not res.get("ok"):
        _admin_fail(what, res)
    return res.get("result") or {}


def _need(pattern: re.Pattern, value: Any, label: str) -> str:
    text = str(value or "")
    if not pattern.fullmatch(text):
        print(f"NOT DONE: {label} is not valid: {text[:60]!r}")
        raise SystemExit(1)
    return text


def _need_ts(value: Any) -> str:
    text = str(value or "")
    if not SLACK_TS_RE.fullmatch(text):
        print(f"NOT DONE: {text!r} is not a Slack message ts (history/threads print it, like 1790190627.123456).")
        raise SystemExit(1)
    return text


def _bridge_knows(config: Config, workspace: str, channel: str) -> bool:
    rows = request_json(config, "/channels", {"workspace": workspace})
    return any(r.get("id") == channel for r in rows or [])


def run_open_dm(args: argparse.Namespace) -> None:
    """Open (or find) the 1:1 DM with a user and print its id. Sends NO message.
    A DM the bridge's poller has not listed yet cannot be sent to: /send only accepts channels in
    the poller's channelMap, which conversations.list refills every 15 minutes (lib/poller.js)."""
    user = _need(SLACK_USER_RE, args.user_id, "user id")
    res = _admin_call(args.workspace, "conversations.open", {"users": user})
    if not res.get("ok"):
        _admin_fail("NOT DONE: Slack would not open the DM", res, code=ADMIN_FAIL_EXIT)
    dm = (res.get("result") or {}).get("channel")
    if not dm:
        print("NOT DONE: Slack gave no DM id.")
        raise SystemExit(ADMIN_FAIL_EXIT)
    print(f"DM with {user} in {args.workspace}: {dm}   (no message sent)")
    config = load_config()
    wait = max(0, int(getattr(args, "wait", 0) or 0))
    start = time.monotonic()
    known = _bridge_knows(config, args.workspace, dm)
    while not known and time.monotonic() - start < wait:
        time.sleep(min(20, max(1, wait - (time.monotonic() - start))))
        known = _bridge_knows(config, args.workspace, dm)
    if known:
        print(f"send works now: the bridge lists {dm}. Preview with: send {dm} \"text\" --workspace {args.workspace}")
        return
    if wait:
        print(f"still not in the bridge after {wait}s. `send` to {dm} answers 404 until the bridge's "
              f"next channel refresh. Try again, or re-run open-dm with a longer --wait.")
        raise SystemExit(3)
    print(f"not in the bridge yet: `send` to {dm} answers 404 until the bridge's next channel refresh "
          f"(every {BRIDGE_CHANNEL_REFRESH_MINUTES} minutes, longer after Slack errors). "
          f"Re-run with --wait 1200 to wait for it, or run open-dm again later.")


def _print_matches(matches: list[dict[str, Any]]) -> None:
    for m in matches:
        try:
            when = _et(float(m.get("ts")))
        except (TypeError, ValueError):
            when = "?"
        where = m.get("channel_name") or m.get("channel_id") or "?"
        who = m.get("username") or m.get("user") or "?"
        print(f"[{when}] {where} | {who}: {_one_line(m.get('text'), 300)}")
        print(f"    channel {m.get('channel_id')}  ts {m.get('ts')}")


def _search(args: argparse.Namespace, query: str) -> None:
    count = max(1, min(int(args.limit), 100))
    res = _admin_call(args.workspace, "search.messages", {"query": query, "count": count})
    if not res.get("ok"):
        _admin_fail("NOT DONE: Slack search failed", res)
    result = res.get("result") or {}
    matches = result.get("matches") or []
    if not matches:
        print("no match")
        return
    print(f"{len(matches)} of {result.get('total', len(matches))} match(es), newest first:")
    _print_matches(matches)


def run_find(args: argparse.Namespace) -> None:
    """Search messages in one workspace. Read only. Slack's index can lag a fresh post by a minute."""
    query = str(args.text or "").strip()
    if not query:
        print("NOT DONE: nothing to search for.")
        raise SystemExit(1)
    _search(args, query)


def run_mentions(args: argparse.Namespace) -> None:
    """Messages that mention you (<@your id>), newest first. Read only. The script swaps {ME}."""
    _search(args, "<@{ME}>")


def _own_message(channel: str, workspace: str, ts: str, verb: str) -> dict[str, Any]:
    """Read one message live (reactions.get, nothing written) and refuse unless you wrote it."""
    info = _react_call(channel, workspace, ts, "-", "check")
    if not info.get("ok"):
        print(f"NOT {verb}: could not read that message ({info.get('error')}).")
        raise SystemExit(1)
    msg = info.get("message") or {}
    self_id = info.get("self")
    if not self_id or msg.get("user") != self_id:
        print(f"NOT {verb}: you did not write that message "
              f"({msg.get('name') or msg.get('user') or 'unknown sender'} did). Nothing changed.")
        raise SystemExit(1)
    return msg


def _money_lock(texts: list[str], money_ok: bool, yes: bool, verb: str, quiet: bool = False) -> bool:
    """The `react` money lock, on the old text and the new one. Returns locked; with --yes and
    no --money-ok it exits MONEY_LOCK_EXIT after printing why."""
    hits: list[str] = []
    unknown = False
    for text in texts:
        found = _money_hits(text)
        if found is None:
            unknown = True
        else:
            hits.extend(found)
    locked = unknown or bool(hits)
    if quiet:
        pass
    elif unknown:
        print("  MONEY LOCK: the money check (draft_check.py) could not run, so treat this as money. "
              "Show the owner the exact text, get a fresh yes, then add --money-ok.")
    elif hits:
        print(f"  MONEY LOCK: the text mentions {', '.join(sorted(set(hits)))}. Show the owner the exact "
              "text, get a fresh yes, then add --money-ok.")
    if locked and yes and not money_ok:
        print(f"NOT {verb}: money lock. Show the owner the exact text, get a fresh yes, then add --money-ok.")
        raise SystemExit(MONEY_LOCK_EXIT)
    return locked


def run_edit(args: argparse.Namespace) -> None:
    """Edit ONE message you wrote. Guarded like `send`: recipient guard, live --since check,
    outbound gate on the final text, dry run unless --yes. The ts pins the message."""
    _apply_recipient_guard(args.channel, getattr(args, "exclude", None))
    channel = _need(SLACK_ID_RE, args.channel, "channel id")
    ts = _need_ts(args.ts)
    new_text = str(args.text or "")
    if not new_text.strip():
        print("NOT EDITED: the new text is empty (use `delete` to remove a message).")
        raise SystemExit(1)
    workspace = args.workspace
    msg = _own_message(channel, workspace, ts, "EDITED")
    old_text = msg.get("text") or ""
    print(f'About to edit your message in {channel} ({workspace}) [ts {ts}]:\n'
          f'  now:  "{_one_line(old_text, 200)}"\n  new:  "{_one_line(new_text, 200)}"')
    if new_text == old_text:
        print("NOT EDITED: the new text is the same as the message. Nothing to do.")
        return
    chat_unchanged_guard(load_config(), channel, workspace, getattr(args, "since", None))
    gate = _gate_check(args, target=channel, texts=[new_text], scope=workspace or "",
                       dry_run=not args.yes, source="slack edit")
    locked = _money_lock([old_text, new_text], bool(getattr(args, "money_ok", False)), False, "EDITED")
    if not args.yes:
        print("Dry run — re-run with --since TS --yes to edit it" + (" (and --money-ok)" if locked else "") + ".")
        return
    if getattr(args, "since", None) is None:
        print("NOT EDITED: --yes needs --since. Run without --yes first; the preview "
              "prints the value for the channel as you read it.")
        raise SystemExit(2)
    _money_lock([old_text, new_text], bool(getattr(args, "money_ok", False)), True, "EDITED", quiet=True)
    op = _gate_begin(gate)
    res = _admin_call(workspace, "chat.update", {"channel": channel, "ts": ts, "text": new_text})
    _gate_finish(gate, op, "uncertain" if res.get("uncertain") else ("sent" if res.get("ok") else "failed_before_send"),
                 [str((res.get("result") or {}).get("ts") or ts)] if res.get("ok") else [])
    if not res.get("ok"):
        _admin_fail("NOT EDITED: Slack refused", res)
    # No ts: the original send already spent it as its receipt, and this output gives the hook
    # none to match. With no receipt, log_event leaves a hook runtime to the hook.
    _log_send(channel, new_text, {"ok": True, "channel": channel}, "edit", workspace=workspace)
    print(f"Edited ts {ts}.")


def run_delete(args: argparse.Namespace) -> None:
    """Delete ONE message you wrote. Dry run unless --yes. Money lock like `react`."""
    _apply_recipient_guard(args.channel, getattr(args, "exclude", None))
    channel = _need(SLACK_ID_RE, args.channel, "channel id")
    ts = _need_ts(args.ts)
    workspace = args.workspace
    msg = _own_message(channel, workspace, ts, "DELETED")
    text = msg.get("text") or ""
    print(f'About to DELETE your message in {channel} ({workspace}) [ts {ts}]: "{_one_line(text, 200)}"')
    locked = _money_lock([text], bool(getattr(args, "money_ok", False)), bool(args.yes), "DELETED")
    if not args.yes:
        print("Dry run — re-run with --yes to delete it" + (" (and --money-ok)" if locked else "") + ".")
        return
    res = _admin_call(workspace, "chat.delete", {"channel": channel, "ts": ts})
    if not res.get("ok"):
        _admin_fail("NOT DELETED: Slack refused", res)
    print(f"Deleted ts {ts}.")


def _channel_preview(workspace: str, channel: str) -> dict[str, Any]:
    """conversations.info (read only): what the write is about to hit. Stops when unreadable."""
    res = _admin_call(workspace, "conversations.info", {"channel": channel})
    if not res.get("ok"):
        print(f"NOT DONE: could not read {channel} ({res.get('error')}).")
        raise SystemExit(1)
    return res.get("result") or {}


def _channel_only(info: dict[str, Any], channel: str) -> None:
    """invite, remove and rename make sense in a channel, never in a DM or group DM."""
    if info.get("is_im") or info.get("is_mpim") or channel.startswith("D"):
        print("NOT DONE: that is a DM or group DM, not a channel.")
        raise SystemExit(1)


def _describe(info: dict[str, Any], channel: str) -> str:
    kind = "DM" if info.get("is_im") else "group DM" if info.get("is_mpim") else \
        "private channel" if info.get("is_private") else "channel"
    name = f"#{info['name']}" if info.get("name") else channel
    members = f", {info['num_members']} members" if info.get("num_members") is not None else ""
    return f"{kind} {name} ({channel}{members})"


def _channel_write(args: argparse.Namespace, method: str, params: dict[str, Any], what: str,
                   done: str, info: dict[str, Any] | None) -> None:
    where = _describe(info, params.get("channel", "")) if info is not None else f"workspace {args.workspace}"
    print(f"About to {what} — {where} in {args.workspace}")
    if not args.yes:
        print("Dry run — re-run with --yes to do it.")
        return
    res = _admin_call(args.workspace, method, params)
    if not res.get("ok"):
        if method == "conversations.leave" and res.get("error") == "last_member":
            print("You are the last member, and Slack keeps a channel from losing its last member. "
                  "Use channel-archive to close it.")
        _admin_fail("NOT DONE: Slack refused", res)
    class _Keep(dict):
        def __missing__(self, key):
            return "?"
    print(done.format_map(_Keep({**params, **(res.get("result") or {})})))


def run_channel_create(args: argparse.Namespace) -> None:
    name = _need(SLACK_CHANNEL_NAME_RE, args.name, "channel name (lowercase letters, digits, - and _)")
    private = bool(getattr(args, "private", False))
    _channel_write(args, "conversations.create", {"name": name, "is_private": private},
                   f"create the {'private' if private else 'public'} channel #{name}",
                   "Created #{name} ({channel}).", None)


def run_channel_invite(args: argparse.Namespace) -> None:
    channel = _need(SLACK_ID_RE, args.channel, "channel id")
    user = _need(SLACK_USER_RE, args.user_id, "user id (one person per call)")
    _apply_recipient_guard(channel, getattr(args, "exclude", None))
    info = _channel_preview(args.workspace, channel)
    _channel_only(info, channel)
    _channel_write(args, "conversations.invite", {"channel": channel, "users": user},
                   f"INVITE {user} into", "Invited {users} to {channel}.", info)


def run_channel_remove(args: argparse.Namespace) -> None:
    channel = _need(SLACK_ID_RE, args.channel, "channel id")
    user = _need(SLACK_USER_RE, args.user_id, "user id")
    _apply_recipient_guard(channel, getattr(args, "exclude", None))
    info = _channel_preview(args.workspace, channel)
    _channel_only(info, channel)
    mine = _admin_call(args.workspace, "auth.test", {})
    if mine.get("ok") and (mine.get("result") or {}).get("user_id") == user:
        print("NOT DONE: that is you. Use channel-leave to leave a channel.")
        raise SystemExit(1)
    _channel_write(args, "conversations.kick", {"channel": channel, "user": user},
                   f"REMOVE {user} from", "Removed {user} from {channel}.", info)


def run_channel_rename(args: argparse.Namespace) -> None:
    channel = _need(SLACK_ID_RE, args.channel, "channel id")
    name = _need(SLACK_CHANNEL_NAME_RE, args.name, "channel name (lowercase letters, digits, - and _)")
    _apply_recipient_guard(channel, getattr(args, "exclude", None))
    info = _channel_preview(args.workspace, channel)
    _channel_only(info, channel)
    _channel_write(args, "conversations.rename", {"channel": channel, "name": name},
                   f"rename to #{name}:", "Renamed {channel} to #{name}.", info)


def run_channel_leave(args: argparse.Namespace) -> None:
    channel = _need(SLACK_ID_RE, args.channel, "channel id")
    _apply_recipient_guard(channel, getattr(args, "exclude", None))
    info = _channel_preview(args.workspace, channel)
    if info.get("is_im") or info.get("is_mpim") or channel.startswith("D"):
        print("NOT DONE: that is a DM; you cannot leave a DM.")
        raise SystemExit(1)
    _channel_write(args, "conversations.leave", {"channel": channel},
                   "LEAVE", "Left {channel}.", info)


def run_channel_archive(args: argparse.Namespace) -> None:
    """Close a channel. Slack lets the last member of a private channel archive it, not leave it."""
    channel = _need(SLACK_ID_RE, args.channel, "channel id")
    _apply_recipient_guard(channel, getattr(args, "exclude", None))
    info = _channel_preview(args.workspace, channel)
    _channel_only(info, channel)
    _channel_write(args, "conversations.archive", {"channel": channel},
                   "ARCHIVE", "Archived {channel}.", info)


def run_channel_members(args: argparse.Namespace) -> None:
    """Member ids of a channel or DM. Read only; follows Slack's pages up to --limit."""
    channel = _need(SLACK_ID_RE, args.channel, "channel id")
    limit = max(1, min(int(getattr(args, "limit", 200) or 200), 5000))
    members: list[str] = []
    cursor = ""
    while len(members) < limit:
        params: dict[str, Any] = {"channel": channel, "limit": min(200, limit - len(members))}
        if cursor:
            params["cursor"] = cursor
        res = _admin_call(args.workspace, "conversations.members", params)
        if not res.get("ok"):
            _admin_fail("NOT DONE: Slack would not list the members", res)
        result = res.get("result") or {}
        members.extend(result.get("members") or [])
        cursor = result.get("next_cursor") or ""
        if not cursor:
            break
    print(f"{len(members)} member(s) of {channel}:")
    for member in members[:limit]:
        print(member)
    if cursor and len(members) >= limit:
        print(f"(more members exist; raise -n above {limit})")


def print_rows(rows: list[dict[str, Any]], fields: list[str]) -> None:
    for row in rows:
        print(" | ".join(str(row.get(field, "")) for field in fields))


def handle(args: argparse.Namespace) -> None:
    config = load_config()
    match args.command:
        case "status":
            health = request_json(config, "/health")
            print(f"Bridge: {health.get('status')} buffered={health.get('total_messages', 0)} url={config.bridge_url}")
            print_rows(health.get("workspaces", []), ["name", "status", "channels", "buffered_messages", "last_poll"])
        case "workspaces":
            print_rows(request_json(config, "/workspaces"), ["name", "team_id", "status", "last_valid"])
        case "channels":
            rows = request_json(config, "/channels", {"workspace": args.workspace})
            if args.type:
                rows = [row for row in rows if row.get("type") == args.type]
            if args.with_messages:
                rows = [row for row in rows if int(row.get("message_count") or 0) > 0]
            rows = rows[: args.limit]
            print_rows(rows, ["workspace", "type", "name", "id", "user", "message_count", "last_activity"])
        case "contacts":
            rows = request_json(config, "/contacts", {"workspace": args.workspace, "name": args.name})
            print_rows(rows, ["workspace", "real_name", "name", "id", "email"])
        case "history":
            rows = request_json(config, f"/channels/{args.channel}/messages", {"limit": args.limit, "since": args.since})
            for msg in rows:
                attachments = msg.get("attachments") or []
                print(json.dumps({
                    "ts": msg.get("ts"),
                    "workspace": msg.get("workspace"),
                    "channel": msg.get("channel_name"),
                    "sender": msg.get("sender_name"),
                    "text": msg.get("text"),
                    "isVoiceNote": msg.get("isVoiceNote"),
                    "attachments": [{"id": a.get("id"), "name": a.get("name"), "type": a.get("mimetype") or a.get("type"), "size": a.get("size")} for a in attachments],
                }, ensure_ascii=False))
        case "files":
            resp = request_json(config, f"/channels/{args.channel}/files", {"workspace": args.workspace, "limit": args.limit})
            rows = resp.get("files", []) if isinstance(resp, dict) else resp
            print_rows(rows, ["id", "name", "mimetype", "size", "created", "user"])
        case "download":
            download_file(config, args.file_id, args.workspace, Path(args.out).expanduser())
        case "transcribe":
            json_print(request_json(config, f"/files/{args.file_id}/transcribe", {"workspace": args.workspace}))
        case "transcribe-channel":
            json_print(request_json(config, f"/channels/{args.channel}/transcribe", {"workspace": args.workspace}))
        case "send":
            _apply_recipient_guard(args.channel, getattr(args, "exclude", None))
            body = {"channel": args.channel, "text": args.text, "workspace": args.workspace}
            thread_ts = getattr(args, "thread_ts", None)
            if thread_ts:
                body["thread_ts"] = thread_ts
                thread_unchanged_guard(config, args.channel, args.workspace,
                                       getattr(args, "since", None), thread_ts)
            else:
                chat_unchanged_guard(config, args.channel, args.workspace, getattr(args, "since", None))
            gate = _gate_check(args, target=args.channel, texts=[args.text], scope=args.workspace or "",
                               thread=thread_ts or "", dry_run=not args.yes, source="slack send")
            if not args.yes:
                print("DRY RUN: would send Slack message")
                json_print({k: v for k, v in body.items() if v is not None})
                print("Re-run with --yes to send.")
                return
            if getattr(args, "since", None) is None:
                # Required since 2026-09-23 (owner's rule): a send with no anchor cannot tell
                # whether he already answered from his phone, so it does not go out.
                print("NOT SENT: --yes needs --since. Run without --yes first; the preview "
                      "prints the value for the channel as you read it.")
                raise SystemExit(2)
            op = _gate_begin(gate)
            resp = _post(config, "/send", body, gate, op)
            _report_send_result(resp)
            _log_send(args.channel, args.text, resp, "send", workspace=args.workspace)
            print_next_since(resp)
        case "upload":
            _apply_recipient_guard(args.channel, getattr(args, "exclude", None))
            path = Path(args.file).expanduser()
            if not path.is_file():
                raise SystemExit(f"file not found: {path}")
            raw = path.read_bytes()
            encoded = base64.b64encode(raw).decode("ascii")
            # The file travels base64-encoded (~33% larger); the bridge route caps the body at 25 MB.
            if len(encoded) > 25 * 1024 * 1024:
                raise SystemExit(
                    f"file too large: {len(raw)} bytes base64-encodes to {len(encoded)} bytes, "
                    "over the bridge's 25 MB upload limit"
                )
            body = {
                "channel": args.channel,
                "workspace": args.workspace,
                "filename": path.name,
                "initial_comment": args.caption,
                "data_base64": encoded,
            }
            G = _comms_module("outbound_gate")
            file_text = G.readable_bytes(raw) if G else None
            gate = _gate_check(args, target=args.channel, texts=[args.caption] if args.caption else [],
                               scope=args.workspace or "", filenames=[path.name],
                               attachment_texts=[(path.name, file_text)] if file_text else [],
                               unread=[] if file_text else [path.name],
                               hashes=[hashlib.sha256(raw).hexdigest()],
                               dry_run=not args.yes, source="slack upload")
            if not args.yes:
                print(f"DRY RUN: would upload {path} ({len(raw)} bytes) to {args.channel}")
                json_print({k: v for k, v in body.items() if v is not None and k != "data_base64"})
                print("Re-run with --yes to upload.")
                return
            op = _gate_begin(gate)
            resp = _post(config, "/files/upload", body, gate, op, timeout=120)
            _report_send_result(resp)
            _log_send(args.channel, args.caption, resp, "upload", files=[path.name], workspace=args.workspace)
        case "threads":
            run_threads(args)
        case "react":
            run_react(args)
        case "open-dm":
            run_open_dm(args)
        case "find":
            run_find(args)
        case "mentions":
            run_mentions(args)
        case "edit":
            run_edit(args)
        case "delete":
            run_delete(args)
        case "channel-create":
            run_channel_create(args)
        case "channel-invite":
            run_channel_invite(args)
        case "channel-remove":
            run_channel_remove(args)
        case "channel-rename":
            run_channel_rename(args)
        case "channel-leave":
            run_channel_leave(args)
        case "channel-archive":
            run_channel_archive(args)
        case "channel-members":
            run_channel_members(args)
        case "sync":
            run_sync(config, args)
        case "pair":
            run_pair(config, args)
        case _:
            raise SystemExit(f"unknown command: {args.command}")


def _add_gate_flags(sub: argparse.ArgumentParser) -> None:
    """Approvals the outbound gate reads (P0). Each waives exactly one check."""
    sub.add_argument("--money-ok", action="store_true",
                     help="the owner saw this exact money/price text and said yes (waives the money lock only)")
    sub.add_argument("--override-check", default=None, metavar="REASON",
                     help="the owner saw the exact text and accepted the draft_check findings "
                          "(waives style/budget only; reason of 4+ chars)")
    sub.add_argument("--resend-ok", action="store_true",
                     help="the owner said to send the same text again (waives the duplicate check only)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Use a self-hosted Slack bridge from any repo.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show bridge health and connected workspaces")
    sub.add_parser("workspaces", help="List paired Slack workspaces")

    channels = sub.add_parser("channels", help="List channels, DMs, and group DMs")
    channels.add_argument("--workspace")
    channels.add_argument("--type", choices=["channel", "private", "im", "mpim"])
    channels.add_argument("--with-messages", action="store_true")
    channels.add_argument("--limit", type=int, default=100)

    contacts = sub.add_parser("contacts", help="Search cached Slack contacts")
    contacts.add_argument("--workspace")
    contacts.add_argument("--name", default="")

    history = sub.add_parser("history", help="Read buffered messages from a channel or DM ID")
    history.add_argument("channel")
    history.add_argument("-n", "--limit", type=int, default=50)
    history.add_argument("--since", default="0")

    threads = sub.add_parser("threads", help="Read a channel/DM WITH its thread replies (read-only, runs on the VPS)")
    threads.add_argument("channel")
    threads.add_argument("--workspace", help="Workspace slug, as shown by status; found from the channel list when left out")
    out_mode = threads.add_mutually_exclusive_group()
    out_mode.add_argument("--json", action="store_true", help="One JSON object with every message and thread reply (for comms)")
    out_mode.add_argument("--full", action="store_true",
                          help="Lossless read (msgschema/1 JSON): full text, all replies (cursor paged), "
                               "files, reactions, bot senders, edits marked")
    threads.add_argument("--before", default=None, metavar="TS",
                         help="With --full: top-level messages older than this ts (the `more` hint)")
    threads.add_argument("-n", "--limit", type=int, default=30, help="Top-level messages to read (max 200)")

    files = sub.add_parser("files", help="List files in a channel or DM")
    files.add_argument("channel")
    files.add_argument("--workspace")
    files.add_argument("-n", "--limit", type=int, default=100)

    download = sub.add_parser("download", help="Download a Slack file by file ID")
    download.add_argument("file_id")
    download.add_argument("--workspace")
    download.add_argument("--out", default="./slack_downloads")

    transcribe = sub.add_parser("transcribe", help="Transcribe one Slack audio/video file by file ID")
    transcribe.add_argument("file_id")
    transcribe.add_argument("--workspace")

    transcribe_channel = sub.add_parser("transcribe-channel", help="Transcribe buffered voice/audio files in a channel")
    transcribe_channel.add_argument("channel")
    transcribe_channel.add_argument("--workspace")

    send = sub.add_parser("send", help="Preview or send a Slack message")
    send.add_argument("channel")
    send.add_argument("text")
    send.add_argument("--workspace")
    send.add_argument("--yes", action="store_true")
    send.add_argument("--since", type=parse_since, default=None, metavar="TS",
                      help=f"REQUIRED with --yes. Time of the newest message you read (the preview "
                           f"prints it; `empty` for a channel with no message). Refuses with exit "
                           f"{STALE_EXIT} if anyone wrote after it, you and thread replies included. "
                           "Message 2+ of a reply: pass the `next --since` the previous send printed.")
    send.add_argument("--thread-ts", dest="thread_ts", default=None, metavar="TS",
                      help="Reply inside this thread (the root message's ts). --since is then "
                           "checked against the thread's replies, not the channel")
    _add_gate_flags(send)
    send.add_argument("--exclude", action="append",
                      help="Refuse the send if the channel matches this name/id (repeatable); needs OUTBOUND_GUARDS=1")

    upload = sub.add_parser("upload", help="Preview or upload a local file to a Slack channel/DM")
    upload.add_argument("channel")
    upload.add_argument("file")
    upload.add_argument("--workspace")
    upload.add_argument("--caption", help="Optional message posted with the file (initial_comment)")
    upload.add_argument("--yes", action="store_true")
    _add_gate_flags(upload)
    upload.add_argument("--exclude", action="append",
                        help="Refuse the upload if the channel matches this name/id (repeatable); needs OUTBOUND_GUARDS=1")

    react = sub.add_parser("react", help="Preview or add an emoji reaction on one message (dry-run unless --yes)")
    react.add_argument("channel", help="Channel or DM ID")
    react.add_argument("ts", help="The message's ts (history/threads print it)")
    react.add_argument("emoji", nargs="?", default=None,
                       help="ONE emoji, a name (heart, thumbsup, laugh, pray, eyes, check), or :slack_name:")
    react.add_argument("--workspace", help="Workspace slug (found from the channel list when left out)")
    react.add_argument("--remove", action="store_true", help="Remove your reaction instead")
    react.add_argument("--money-ok", action="store_true",
                       help="the owner saw this money/price message and said yes to reacting")
    react.add_argument("--yes", action="store_true", help="Actually react (without it, preview only)")
    react.add_argument("--exclude", action="append", default=None, help=argparse.SUPPRESS)

    # ---- admin verbs: every write is a dry run unless --yes; --workspace is always required
    open_dm = sub.add_parser("open-dm", help="Open or find the 1:1 DM with a user and print its id (sends nothing)")
    open_dm.add_argument("user_id", help="Slack user id, like U0123456789")
    open_dm.add_argument("--workspace", required=True)
    open_dm.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                         help="Wait up to this long for the bridge to list the DM, so `send` works (a new DM "
                              "joins the bridge within about 15 minutes)")

    find = sub.add_parser("find", help="Search messages in one workspace (read only)")
    find.add_argument("text")
    find.add_argument("--workspace", required=True)
    find.add_argument("-n", "--limit", type=int, default=20)

    mentions = sub.add_parser("mentions", help="Messages that mention you in one workspace (read only)")
    mentions.add_argument("--workspace", required=True)
    mentions.add_argument("-n", "--limit", type=int, default=20)

    edit = sub.add_parser("edit", help="Preview or edit one message you wrote (dry-run unless --yes)")
    edit.add_argument("channel")
    edit.add_argument("ts", help="The message's ts (history/threads print it)")
    edit.add_argument("text")
    edit.add_argument("--workspace", required=True)
    edit.add_argument("--yes", action="store_true")
    edit.add_argument("--since", type=parse_since, default=None, metavar="TS",
                      help="REQUIRED with --yes, exactly as for `send`: the newest message you read "
                           f"(the preview prints it). Refuses with exit {STALE_EXIT} if anyone wrote after it.")
    _add_gate_flags(edit)
    edit.add_argument("--exclude", action="append", help=argparse.SUPPRESS)

    delete = sub.add_parser("delete", help="Preview or delete one message you wrote (dry-run unless --yes)")
    delete.add_argument("channel")
    delete.add_argument("ts")
    delete.add_argument("--workspace", required=True)
    delete.add_argument("--money-ok", action="store_true", help="the owner saw this money/price message and said yes to deleting it")
    delete.add_argument("--yes", action="store_true")
    delete.add_argument("--exclude", action="append", help=argparse.SUPPRESS)

    ch_create = sub.add_parser("channel-create", help="Preview or create a channel (dry-run unless --yes)")
    ch_create.add_argument("name")
    ch_create.add_argument("--private", action="store_true")
    ch_create.add_argument("--workspace", required=True)
    ch_create.add_argument("--yes", action="store_true")

    ch_invite = sub.add_parser("channel-invite", help="Preview or invite one user to a channel (dry-run unless --yes)")
    ch_invite.add_argument("channel")
    ch_invite.add_argument("user_id")
    ch_invite.add_argument("--workspace", required=True)
    ch_invite.add_argument("--yes", action="store_true")
    ch_invite.add_argument("--exclude", action="append", help=argparse.SUPPRESS)

    ch_remove = sub.add_parser("channel-remove", help="Preview or remove one user from a channel (dry-run unless --yes)")
    ch_remove.add_argument("channel")
    ch_remove.add_argument("user_id")
    ch_remove.add_argument("--workspace", required=True)
    ch_remove.add_argument("--yes", action="store_true")
    ch_remove.add_argument("--exclude", action="append", help=argparse.SUPPRESS)

    ch_rename = sub.add_parser("channel-rename", help="Preview or rename a channel (dry-run unless --yes)")
    ch_rename.add_argument("channel")
    ch_rename.add_argument("name")
    ch_rename.add_argument("--workspace", required=True)
    ch_rename.add_argument("--yes", action="store_true")
    ch_rename.add_argument("--exclude", action="append", help=argparse.SUPPRESS)

    ch_leave = sub.add_parser("channel-leave", help="Preview or leave a channel (dry-run unless --yes)")
    ch_leave.add_argument("channel")
    ch_leave.add_argument("--workspace", required=True)
    ch_leave.add_argument("--yes", action="store_true")
    ch_leave.add_argument("--exclude", action="append", help=argparse.SUPPRESS)

    ch_archive = sub.add_parser("channel-archive", help="Preview or archive a channel (dry-run unless --yes)")
    ch_archive.add_argument("channel")
    ch_archive.add_argument("--workspace", required=True)
    ch_archive.add_argument("--yes", action="store_true")
    ch_archive.add_argument("--exclude", action="append", help=argparse.SUPPRESS)

    ch_members = sub.add_parser("channel-members", help="List the member ids of a channel or DM (read only)")
    ch_members.add_argument("channel")
    ch_members.add_argument("--workspace", required=True)
    ch_members.add_argument("-n", "--limit", type=int, default=200)

    sync = sub.add_parser("sync", help="Sync Slack bridge data into BI Supabase")
    sync.add_argument("--days", type=int, default=3)
    sync.add_argument("--dry-run", action="store_true")

    pair = sub.add_parser("pair", help="Pair one or more new Slack workspaces and push tokens to VPS")
    pair.add_argument("workspaces", nargs="+", help="Workspace slug(s) or the name Slack shows, comma-separated or space-separated")
    pair.add_argument("--sign-in", action="store_true", help="Skip the workspace-list lookup and open <slug>.slack.com directly")
    pair.add_argument("--sync-after", action="store_true", help="Run BI sync after pairing; default pairs only")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "send" and getattr(args, "thread_ts", None) and not SLACK_TS_RE.fullmatch(args.thread_ts):
        raise SystemExit(f"--thread-ts is not a Slack message ts: {args.thread_ts}")
    _read_logged(args)


if __name__ == "__main__":
    main()
