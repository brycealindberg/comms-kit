#!/usr/bin/env python3
"""PostToolUse hook: log every iMessage / WhatsApp / Slack / Gmail / Upwork call.

Runs as a separate OS process. It never returns anything to the model, so it
costs zero tokens. It must never fail loudly and never block a tool call:
every path exits 0.

Two capture paths:
  Bash  -> the four CLI channels, parsed out of the command string
  MCP   -> Upwork, parsed out of the already-structured tool_input

Writes are BATCHED. One row at a time cost an open + chmod + lock + full index
rewrite each, so a single command chaining ~700 sends took longer than the
hook's 10 second budget and was killed — 211 of 900 real sends were lost with
no error and no trace. Everything now lands in one append and one locked index
update, whatever the row count.
"""
import json
import os
import re
import sys


def _core_dir():
    """Where the kit's core lives: COMMS_KIT_CORE, then core/ two folders above this file (the
    repo layout), then ~/.claude/communications (the installed layout)."""
    here = os.path.dirname(os.path.abspath(__file__))  # kit: code path
    for folder in (os.environ.get("COMMS_KIT_CORE"),
                   os.path.join(here, "..", "..", "core"),
                   os.path.expanduser("~/.claude/communications")):
        if folder and os.path.exists(os.path.join(folder, "commslib.py")):
            return os.path.abspath(os.path.expanduser(folder))
    return None


CORE = _core_dir()
if CORE:
    sys.path.insert(0, CORE)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    try:
        import commslib as C
    except Exception as err:
        if type(err).__name__ == "ConfigError":
            # a broken kit config: say so in one line, archive nothing, never fail the call
            sys.stderr.write("%s\n" % err)
        return

    tool = payload.get("tool_name") or ""
    ctx = {"routes": C._load(C.ROUTES, {}),
           "previews": C._load(C.PREVIEWS, {})}
    items = []

    if tool == "Bash":
        command = (payload.get("tool_input") or {}).get("command") or ""
        if not command:
            return
        # `send A ; send B` is two messages. Logging only the first loses one
        # forever, so every invocation in the line gets its own row.
        segments = list(C.detect_all(command))
        # ONE Bash line can call `gws gmail` twice, and both rows are handed
        # the same merged output blob. The headers in it belong to whichever
        # call printed them, so with more than one gmail row here nothing in
        # the body can be attributed and the message id stays the key.
        # `for id in a b; do gws gmail +read --message-id $id; done` is ONE
        # segment to `detect_all`, which strips shell keywords and wrappers on
        # purpose -- so N real calls counted as one and this said "safe to
        # attribute". Same for `xargs`. A loop is never sole.
        sole_gmail = (sum(1 for ch, _s in segments if ch == "gmail") == 1
                      and not LOOP_RE.search(command))
        ctx["receipt_attributable"] = len(segments) == 1 and not LOOP_RE.search(command)
        for channel, segment in segments:
            info = C.parse(channel, segment)
            direction = C.VERBS[channel].get(info["verb"])
            if not direction:
                continue        # status / doctor / pair / login: noise
            if info["dry_run"]:
                direction = "draft"
            got = _record(C, payload, ctx, channel, info["verb"], direction,
                          info["target"], info["text"], info.get("fallback"),
                          segment, command, info.get("recipients"),
                          sole_gmail=sole_gmail)
            if got:
                items.append(got)
        _flush(C, payload, items)
        return

    if not tool.startswith("mcp__"):
        return
    info = C.parse_mcp(tool, payload.get("tool_input"))
    if not info:
        return
    segment = tool + " " + json.dumps(payload.get("tool_input") or {})[:800]
    got = _record(C, payload, ctx, info["channel"], info["verb"],
                  info["direction"], info["target"], info["text"], None,
                  segment, "", info.get("recipients"))
    _flush(C, payload, [got] if got else [])


LOOP_RE = re.compile(r"(?:^|[;&|(`]|\s)(?:for|while|until|xargs)\s", re.I)


def _record(C, payload, ctx, channel, verb, direction, target, text, fallback,
            segment, full_command, recipients=None, sole_gmail=True):
    """Build one message row. Returns (row, meta) or None; writes nothing."""

    body, interrupted = C.body_of(payload.get("tool_response"))
    if interrupted:
        return None
    target_as_typed = target or fallback      # the same key the outbound-gate hook used before
    ts, et, month = C._now()
    routes = ctx["routes"]

    # iMessage preview sends name a preview id, not a chat. Resolve it back to
    # the chat + exact text captured at prepare-send time.
    if channel == "imessage" and verb == "prepare-send":
        pid = _preview_id(body)
        if pid:
            with C.filelock(C.PREVIEWS):
                previews = C.load_strict(C.PREVIEWS, {}) or {}
                previews[pid] = {"target": target, "text": text}
                if len(previews) > 200:
                    previews = dict(list(previews.items())[-200:])
                C._save(C.PREVIEWS, previews)
    if channel == "imessage" and verb == "send-preview":
        got = ctx["previews"].get(str(target or "").strip())
        if got:
            target, text = got.get("target"), got.get("text")

    # `send "$ROOM" ...` logs the literal variable name; the shell expanded it
    # before we ever saw it. Recover the real id from the command's own output,
    # and drop the row rather than archive a garbage key.
    # ONE parse of the body answers every Gmail question below. It walks the
    # whole blob, and it used to run twice for the same row.
    party = C.gmail_party(body) if (channel == "gmail" and sole_gmail) else None
    recovered = False
    if not target or C.unresolved(target):
        recovered = True
        if channel == "gmail":
            # the correspondent, or -- when the header block names only the owner
            # -- a genuine note to self, which is a real thread and should not
            # be filed under a message id
            got = party or (C.gmail_self_note(body) if sole_gmail else None)
        else:
            got = C.recover_target(body, channel)
        target = got or fallback
    # an unexpanded `$id` from a loop would otherwise merge every recipient in
    # that loop into one thread — drop the row instead of forging a contact
    if not target or C.unresolved(target):
        return None

    # `gws gmail +reply --message-id 1a03...` names no person, so without this
    # the row files under the message id and `comms thread <email>` -- and
    # routing by contact -- can never reach it. The message's own headers say
    # who it is with; anything ambiguous keeps the id.
    if channel == "gmail" and party:
        if C.GMAIL_ID_RE.match(str(target).strip().lower()):
            target = party
        elif recovered and C.email_key(target) in C.self_emails():
            # ... and the same headers rescue the other half of the defect: an
            # id-less RECOVERY that landed on the owner's own address filed 7
            # archived rows as a thread with himself. Only when the target was
            # recovered: an explicit `--to owner@example.com` is the owner
            # naming the recipient, and a forwarded message's quoted headers
            # must not overrule him.
            target = party

    # A read that errored has no conversation in it, and a send that errored
    # never reached anyone — recording it claims a message was delivered.
    # Judged HERE, not before the block above, because one Bash call can chain
    # several sends and they all share one output blob: without this row's own
    # channel and recipient the verdict is whichever send the blob mentions
    # first, which archived a refused message and deleted a delivered one.
    if C.failed(body, empty_counts=(direction == "read"),
                channel=channel, target=target):
        return None

    # The CLI archived this send itself (2026-10-30, P0): one row per delivered part, keyed by the
    # transport's receipt. When EVERY receipt this output carries is already archived, a second
    # row here would be a duplicate. Any receipt missing, or none found: write the row as before.
    workspace = C._flag(C.argv_of(segment), "--workspace") if channel == "slack" else None
    if (direction == "send" and ctx.get("receipt_attributable", True)
            and C.receipts_known(C.receipts_in_body(channel, body, workspace=workspace))):
        return None

    # `--text-file "$D/final.txt"` is an unexpanded shell variable and the draft
    # file is deleted after sending, so the words are not in the command line.
    # Every tool keeps its own record of what it actually sent — read that.
    files = None
    draft_put = None
    source = "command" if text else None
    # `--body "$(cat draft.txt)"` reaches the hook as the shell's own source
    # text. Storing it verbatim archived `$(cat draft.txt)` as the message.
    if text and C.unexpanded_text(text):
        path = C.substitution_file(text)
        got = C._read_small(path) if path else None
        # the path is only readable when it was written literally; a `$VAR` in
        # it was already expanded away before the hook ever saw the command
        text, source = (got, "substitution-file") if got else (None, None)
    if direction in ("send", "draft"):
        # scoped to THIS row's recipient: one Bash call can hold several
        # sends and they all share one output blob
        got = C.recover_sent(body, channel, verb, direction, target)
        files = got.get("files")
        if not text and got.get("text"):
            text, source = got["text"], got["source"]
        # Slack prints the text ONLY on the dry run. The real send is the same
        # command plus `--yes`, seconds later, so it borrows what that dry run
        # composed — matched on the command itself, never on timing alone.
        # Keyed on the SEGMENT, not the whole line: tokenising a 143 KB batched
        # command once per row was quadratic and blew the hook's budget.
        if direction == "draft" and text:
            draft_put = (C.draft_key(channel, target, segment), text)
        elif direction == "send" and not text:
            prior = C.recall_draft(C.draft_key(channel, target, segment))
            if prior:
                text, source = prior, "draft-cache"


    # Gmail and Upwork have no local send tool to reserve the send in; record the delivered send
    # so the outbound gate's duplicate check sees it next time (best effort, never blocks).
    if direction == "send" and channel in ("gmail", "upwork"):
        _record_external(channel, target_as_typed, text)

    key = C.norm(target)
    hit = C.route(target, routes) or {}
    project = hit.get("project")
    label = hit.get("label")

    row = {
        "ts": round(ts, 3), "et": et, "channel": channel, "verb": verb,
        "dir": direction, "target": key, "raw_target": target,
        "project": project, "label": label,
        "text": C.redact((text or "")[:C.BODY_CAP]) or None,
        "body": C.redact(body[:C.BODY_CAP]),
        "truncated": len(body) > C.BODY_CAP or len(text or "") > C.BODY_CAP,
        # where the words came from: the command line, the tool's own preview
        # store, or its stdout. A null here on a send means genuinely no text.
        "text_source": source,
        # attachments were previously invisible: a file-only send recorded
        # nothing about what was sent
        "files": files or None,
        # everyone who received it, not just the primary recipient
        "recipients": [C.norm(r) for r in (recipients or [])] or None,
        "cwd": payload.get("cwd"),
        "session": (payload.get("session_id") or "")[:8],
        "command": C.redact((segment or "")[:1000]),
        # the ORIGINAL command too, not just the extracted segment: a later
        # cleanup pass cannot re-apply detect() to a segment, because the
        # segment already looks like a clean invocation by construction
        "full_command": C.redact(full_command[:2000]) or None,
        # Redaction can SHORTEN the stored copy, so its length is not a
        # reliable test of whether it was cut. `comms reindex --prune` needs to
        # know, because it re-runs detection on this field and a command cut
        # before its send verb looks like it was never a comms call at all.
        # Written as a real False, never null: the PRESENCE of the key is what
        # tells a later rebuild that this row is new enough to be trusted.
        "fc_cut": len(full_command) > 2000,
    }
    meta = {"month": month, "key": key, "channel": channel, "ts": ts,
            "et": et, "project": project, "label": label, "hit": hit,
            "target": target, "draft_put": draft_put}
    return row, meta


def _flush(C, payload, items):
    """All the disk work for one tool call, in one pass (commslib.write_rows, shared with the
    CLIs since 2026-10-30)."""
    if not items:
        return
    C.write_rows(items, cwd=payload.get("cwd"))


def _record_external(channel, target, text):
    try:
        import importlib.util
        path = os.path.join(CORE or os.path.dirname(os.path.abspath(__file__)),  # kit: code path
                            "outbound_gate.py")
        spec = importlib.util.spec_from_file_location("outbound_gate", path)
        G = importlib.util.module_from_spec(spec)
        sys.modules["outbound_gate"] = G
        spec.loader.exec_module(G)
        G.record_external_send(channel, target, text)
    except Exception:
        pass


def _preview_id(body):
    import re
    m = re.search(r'"preview_id"\s*:\s*"([^"]+)"', body or "")
    if m:
        return m.group(1)
    m = re.search(r"preview[_ ]?id[:= ]+([A-Za-z0-9_-]{6,})", body or "", re.I)
    return m.group(1) if m else None


try:
    main()
except Exception:
    pass
sys.exit(0)
