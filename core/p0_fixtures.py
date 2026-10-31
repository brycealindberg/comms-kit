"""Golden fixtures for the comms readers and the outbound gate (P0, 2026-10-30). SIMULATED data.

Raw server shapes per channel, built in code so they are deterministic and readable:
  - WhatsApp: Matrix events + a fake `_request` that pages /messages like the homeserver
  - Slack: a JSON fixture + a fake bridge (lib/token-store.js, lib/slack-client.js) for the reader
  - iMessage: upstream `imsg history --json` rows (newest first)
Shared by the test suites and `comms selftest`. Every text carries SENTINEL so a leak into a real
archive can be counted.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any, Dict, List, Optional

SENTINEL = "P0SANDBOX-7f3a"
T0 = 1_761_800_000_000            # ms, 2025-10-30 (fixed clock)
ROOM = "!fixtureroom:example.invalid"
ME = "@owner:example.invalid"
NIR = "@whatsapp_12065550123:example.invalid"
SAM = "@whatsapp_12065550124:example.invalid"
BOT = "@whatsappbot:example.invalid"
SELF_NUMBER = "15550009999"

LONG = ("Here is how to set up the export, step by step. " + SENTINEL + " ") * 103   # > 5,000 chars
LONG = LONG[:5000] + " END-OF-LONG"
HOWTO = ("To reset the router: hold the button for ten seconds, wait for the light, then log in "
         "with the admin password from the sticker and choose the 5 GHz network. " + SENTINEL) * 8


# ------------------------------------------------------------------ WhatsApp

def _ev(i: int, sender: str, content: Dict[str, Any], etype: str = "m.room.message",
        **extra: Any) -> Dict[str, Any]:
    e = {"event_id": "$e%04d" % i, "type": etype, "sender": sender, "room_id": ROOM,
         "origin_server_ts": T0 + i * 60_000, "content": content}
    e.update(extra)
    return e


def whatsapp_events() -> List[Dict[str, Any]]:
    """Oldest first. 160 filler messages, then every case the full read must get right."""
    ev: List[Dict[str, Any]] = []
    i = 0
    for k in range(160):
        i += 1
        ev.append(_ev(i, NIR if k % 2 else ME, {"msgtype": "m.text", "body": "filler %d %s" % (k, SENTINEL)}))
    i += 1; ev.append(_ev(i, NIR, {"msgtype": "m.text", "body": LONG}))                       # $e0161
    i += 1; ev.append(_ev(i, NIR, {"msgtype": "m.text", "body": "price is 40 " + SENTINEL}))  # $e0162 edited
    i += 1; ev.append(_ev(i, NIR, {"msgtype": "m.text", "body": "* price is 45",
                                   "m.new_content": {"msgtype": "m.text", "body": "price is 45 " + SENTINEL},
                                   "m.relates_to": {"rel_type": "m.replace", "event_id": "$e0162"}}))
    i += 1; ev.append(_ev(i, SAM, {"msgtype": "m.text", "body": "* forged",                    # forged edit
                                   "m.new_content": {"msgtype": "m.text", "body": "FORGED"},
                                   "m.relates_to": {"rel_type": "m.replace", "event_id": "$e0162"}}))
    i += 1; ev.append(_ev(i, NIR, {"msgtype": "m.text", "body": "oops wrong chat"}))           # $e0165 deleted
    i += 1; ev.append(_ev(i, NIR, {"redacts": "$e0165"}, etype="m.room.redaction", redacts="$e0165"))
    i += 1; ev.append(_ev(i, NIR, {"msgtype": "m.image", "body": "look at this " + SENTINEL,
                                   "filename": "photo.jpg", "url": "mxc://example.invalid/img1",
                                   "info": {"mimetype": "image/jpeg", "size": 12345}}))      # $e0167
    i += 1; ev.append(_ev(i, NIR, {"msgtype": "m.audio", "body": "voice.ogg",
                                   "url": "mxc://example.invalid/voice1",
                                   "info": {"mimetype": "audio/ogg", "size": 4567}}))        # $e0168
    i += 1; ev.append(_ev(i, NIR, {"msgtype": "m.file", "body": "quote.pdf",
                                   "url": "mxc://example.invalid/file1",
                                   "info": {"mimetype": "application/pdf", "size": 999}}))   # $e0169
    i += 1; ev.append(_ev(i, ME, {"msgtype": "m.text", "body": "yes that works " + SENTINEL,  # $e0170 reply
                                  "m.relates_to": {"m.in_reply_to": {"event_id": "$e0162"}}}))
    i += 1; ev.append(_ev(i, ME, {"msgtype": "m.text", "body": "re the very first one",        # $e0171 reply out of window
                                  "m.relates_to": {"m.in_reply_to": {"event_id": "$e0001"}}}))
    i += 1; ev.append(_ev(i, NIR, {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$e0170",
                                                    "key": "\u2764\ufe0f"}}, etype="m.reaction"))
    i += 1; ev.append(_ev(i, NIR, {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$e0170",
                                                    "key": "\U0001f44d"}}, etype="m.reaction"))     # $e0173 removed
    i += 1; ev.append(_ev(i, NIR, {"redacts": "$e0173"}, etype="m.room.redaction", redacts="$e0173"))
    i += 1; ev.append(_ev(i, BOT, {"msgtype": "m.notice", "body": "bridge notice " + SENTINEL}))
    # the long-text window: a long how-to, then "." and a short reply
    i += 1; ev.append(_ev(i, NIR, {"msgtype": "m.text", "body": HOWTO}))                       # $e0176
    i += 1; ev.append(_ev(i, NIR, {"msgtype": "m.text", "body": "."}))
    i += 1; ev.append(_ev(i, NIR, {"msgtype": "m.text", "body": "let me know " + SENTINEL}))  # $e0178
    # a deleted message can carry no text at all (server-side redaction)
    for e in ev:
        if e["event_id"] == "$e0165":
            e["unsigned"] = {"redacted_because": {"event_id": "$e0166"}}
            e["content"] = {}
    return ev


class FakeMatrix(object):
    """Pages /messages like a homeserver: dir=b, `from` token, `end` token. GET only."""

    def __init__(self, events: Optional[List[Dict[str, Any]]] = None, unread: int = 3,
                 page_cap: Optional[int] = None) -> None:
        self.events = list(events if events is not None else whatsapp_events())
        self.unread = unread
        self.page_cap = page_cap
        self.calls: List[tuple] = []
        self.puts: List[tuple] = []

    def by_id(self, eid: str) -> Optional[Dict[str, Any]]:
        for e in self.events:
            if e["event_id"] == eid:
                return e
        return None

    def __call__(self, method: str, path: str, token: Any = None, body: Any = None,
                 params: Any = None) -> Dict[str, Any]:
        self.calls.append((method, path, dict(params or {})))
        room = "/rooms/" + urllib.parse.quote(ROOM, safe="")
        if method == "PUT" and "/send/m.room.message/" in path:
            self.puts.append((path, body))
            return {"event_id": "$sent%03d" % len(self.puts)}
        if method != "GET":
            raise AssertionError("fake homeserver: unexpected %s %s" % (method, path))
        if path.endswith(room + "/messages"):
            p = dict(params or {})
            start = int(p.get("from") or len(self.events))
            limit = int(p.get("limit") or 10)
            if self.page_cap:
                limit = min(limit, self.page_cap)
            lo = max(0, start - limit)
            chunk = list(reversed(self.events[lo:start]))
            out = {"chunk": chunk, "start": str(start)}
            if lo > 0:
                out["end"] = str(lo)
            return out
        if (room + "/context/") in path:
            eid = urllib.parse.unquote(path.rsplit("/", 1)[1])
            idx = [e["event_id"] for e in self.events].index(eid)
            return {"event": self.events[idx], "start": str(idx), "end": str(idx + 1)}
        if (room + "/event/") in path:
            eid = urllib.parse.unquote(path.rsplit("/", 1)[1])
            e = self.by_id(eid)
            if e is None:
                from_mod = AssertionError("no event %s" % eid)
                raise from_mod
            return e
        if path.endswith("/sync"):
            return {"rooms": {"join": {ROOM: {
                "unread_notifications": {"notification_count": self.unread},
                "timeline": {"events": [self.events[-1]]}}}}}
        if path.endswith(room + "/joined_members"):
            return {"joined": {NIR: {"display_name": "Alex"}, ME: {"display_name": "Owner"},
                               BOT: {"display_name": "bot"}}}
        if path.endswith(room + "/state/m.room.name"):
            return {"name": "Alex Fixture"}
        if path.endswith(room + "/state"):
            return []
        raise AssertionError("fake homeserver: unexpected GET %s" % path)


# ------------------------------------------------------------------ Slack

SLACK_CHANNEL = "C0FIXTURE1"
SLACK_DM = "D0FIXTURE1"


def slack_fixture() -> Dict[str, Any]:
    """conversations.history (newest first, as Slack returns it) + replies per thread ts."""
    base = 1761800000.0
    ts = lambda k: "%.6f" % (base + k)  # noqa: E731
    msgs = [
        {"ts": ts(10), "user": "U1", "text": LONG},
        {"ts": ts(20), "user": "U2", "text": "edited one " + SENTINEL, "edited": {"user": "U2", "ts": ts(21)}},
        {"ts": ts(30), "bot_id": "B1", "subtype": "bot_message", "username": "Deploy Bot",
         "bot_profile": {"name": "Deploy Bot"}, "text": "",
         "attachments": [{"fallback": "build 42 passed " + SENTINEL, "title": "CI"}]},
        {"ts": ts(40), "user": "U1", "text": "spec attached " + SENTINEL,
         "files": [{"id": "F1", "name": "spec.pdf", "mimetype": "application/pdf", "size": 2048}],
         "reactions": [{"name": "thumbsup", "users": ["U2", "UME"], "count": 2}]},
        {"ts": ts(50), "user": "U2", "text": "",
         "blocks": [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
             {"type": "text", "text": "blocks only " + SENTINEL}]}]}]},
        {"ts": ts(60), "user": "UME", "text": "thread root " + SENTINEL, "reply_count": 150,
         "latest_reply": ts(60 + 150)},
    ]
    replies = [{"ts": ts(60), "user": "UME", "text": "thread root " + SENTINEL, "thread_ts": ts(60)}]
    for k in range(1, 151):
        text = ("reply %d " % k) + (LONG if k == 75 else SENTINEL)
        replies.append({"ts": ts(60 + k), "user": "U1" if k % 2 else "U2", "text": text, "thread_ts": ts(60)})
    return {"history": list(reversed(msgs)), "replies": {ts(60): replies},
            "users": {"U1": "Alex Slack", "U2": "Sam Slack", "UME": "Owner Doe"}, "me": "UME"}


FAKE_TOKEN_STORE = """
class TokenStore { constructor() { this.workspaces = { T1: { name: 'fixture', token: 'x', cookie: 'y' } }; } }
module.exports = { TokenStore };
"""

FAKE_SLACK_CLIENT = r"""
const fs = require('fs');
const F = JSON.parse(fs.readFileSync(process.env.FAKE_SLACK_FIXTURE, 'utf8'));
const PAGE = parseInt(process.env.FAKE_SLACK_PAGE || '60', 10);
function page(list, args) {
  const start = parseInt(args.cursor || '0', 10);
  const n = Math.min(args.limit || 100, PAGE);
  const slice = list.slice(start, start + n);
  const more = start + n < list.length;
  return { ok: true, messages: slice, has_more: more,
           response_metadata: { next_cursor: more ? String(start + n) : '' } };
}
function createSlackClient() {
  return {
    users: { info: async ({ user }) => {
      if (!F.users[user]) throw new Error('user_not_found');
      return { user: { real_name: F.users[user], name: user } };
    } },
    auth: { test: async () => ({ user_id: F.me }) },
    conversations: {
      history: async (args) => {
        let list = F.history;
        if (args.latest) list = list.filter(m => parseFloat(m.ts) < parseFloat(args.latest));
        return page(list, args);
      },
      replies: async (args) => {
        const all = F.replies[args.ts] || (F.history.find(m => m.ts === args.ts) ? [F.history.find(m => m.ts === args.ts)] : []);
        if (!all.length) return { ok: true, messages: [], has_more: false, response_metadata: { next_cursor: '' } };
        // Slack repeats the parent first on every page
        const rest = all.slice(1);
        const p = page(rest, args);
        p.messages = [all[0]].concat(p.messages);
        return p;
      },
    },
  };
}
module.exports = { createSlackClient };
"""


def write_fake_bridge(folder: str) -> Dict[str, str]:
    """A fake bridge dir for slack-thread-read.js. Returns the env the reader needs."""
    os.makedirs(os.path.join(folder, "lib"), exist_ok=True)
    with open(os.path.join(folder, "lib", "token-store.js"), "w") as fh:
        fh.write(FAKE_TOKEN_STORE)
    with open(os.path.join(folder, "lib", "slack-client.js"), "w") as fh:
        fh.write(FAKE_SLACK_CLIENT)
    fixture = os.path.join(folder, "fixture.json")
    with open(fixture, "w") as fh:
        json.dump(slack_fixture(), fh)
    return {"SLACK_BRIDGE_DIR": folder, "FAKE_SLACK_FIXTURE": fixture, "ENCRYPTION_KEY": "fixture",
            "TZ": "America/Los_Angeles"}


# ------------------------------------------------------------------ iMessage

def imessage_rows(extra_reactions: int = 0) -> List[Dict[str, Any]]:
    """Upstream imsg history rows, NEWEST first (as the pinned binary returns them)."""
    def iso(k: int) -> str:
        import datetime
        return datetime.datetime.fromtimestamp(T0 / 1000 + k * 60, datetime.timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rows = [
        {"id": 1, "guid": "g1", "chat_id": 104, "is_from_me": False, "sender": "+12065550123",
         "sender_name": "Alex", "text": LONG, "created_at": iso(1)},
        {"id": 2, "guid": "g2", "chat_id": 104, "is_from_me": False, "sender": "+12065550123",
         "sender_name": "Alex", "text": "\ufffc", "created_at": iso(2),
         "attachments": [{"filename": "~/Library/Messages/Attachments/aa/photo.heic",
                          "transfer_name": "photo.heic", "mime_type": "image/heic",
                          "total_bytes": 4321, "missing": False,
                          "original_path": "~/Library/Messages/Attachments/aa/photo.heic"}]},
        {"id": 3, "guid": "g3", "chat_id": 104, "is_from_me": True, "sender": "+12065550123",
         "text": "sounds good " + SENTINEL, "created_at": iso(3), "reply_to_guid": "g1"},
        {"id": 4, "guid": "g4", "chat_id": 104, "is_from_me": False, "sender": "+12065550123",
         "sender_name": "Alex", "text": "Loved \u201csounds good\u201d", "created_at": iso(4),
         "is_reaction": True, "is_reaction_add": True, "reaction_type": "love",
         "reaction_emoji": "\u2764\ufe0f", "reacted_to_guid": "g3"},
        {"id": 5, "guid": "g5", "chat_id": 104, "is_from_me": False, "sender": "+12065550123",
         "sender_name": "Alex", "text": "Liked \u201csounds good\u201d", "created_at": iso(5),
         "is_reaction": True, "is_reaction_add": True, "reaction_type": "like",
         "reaction_emoji": "\U0001f44d", "reacted_to_guid": "g3"},
        {"id": 6, "guid": "g6", "chat_id": 104, "is_from_me": False, "sender": "+12065550123",
         "sender_name": "Alex", "text": "Removed a like", "created_at": iso(6),
         "is_reaction": True, "is_reaction_add": False, "reaction_type": "like",
         "reaction_emoji": "\U0001f44d", "reacted_to_guid": "g3"},
        {"id": 7, "guid": "g7", "chat_id": 104, "is_from_me": False, "sender": "+12065550123",
         "sender_name": "Alex", "text": HOWTO, "created_at": iso(7)},
        {"id": 8, "guid": "g8", "chat_id": 104, "is_from_me": False, "sender": "+12065550123",
         "sender_name": "Alex", "text": ".", "created_at": iso(8)},
        {"id": 9, "guid": "g9", "chat_id": 104, "is_from_me": False, "sender": "+12065550123",
         "sender_name": "Alex", "text": "let me know " + SENTINEL, "created_at": iso(9)},
    ]
    for k in range(extra_reactions):
        rows.append({"id": 100 + k, "guid": "r%d" % k, "chat_id": 104, "is_from_me": False,
                     "sender": "+12065550123", "sender_name": "Alex", "text": "Emphasized",
                     "created_at": iso(10 + k), "is_reaction": True, "is_reaction_add": True,
                     "reaction_type": "emphasize", "reaction_emoji": "\u203c\ufe0f",
                     "reacted_to_guid": "g9"})
    return list(reversed(rows))


# ------------------------------------------------------------------ throwaway comms home

def make_comms_home(tmp: str) -> str:
    """A HOME whose ~/.claude/{skills,hooks,scripts} and communications code point at the REAL
    code next to this file, while every data file the code writes lands in `tmp`. Returns tmp."""
    code = os.path.dirname(os.path.realpath(__file__))
    claude = os.path.dirname(code)
    comms = os.path.join(tmp, ".claude", "communications")
    os.makedirs(comms, exist_ok=True)
    for name in os.listdir(code):  # kit: code path
        if name.endswith(".py") or name in ("bin",):
            dst = os.path.join(comms, name)
            if not os.path.lexists(dst):
                os.symlink(os.path.join(code, name), dst)  # kit: code path
    for sub in ("skills", "hooks", "scripts"):
        dst = os.path.join(tmp, ".claude", sub)
        if not os.path.lexists(dst):
            os.symlink(os.path.join(claude, sub), dst)  # kit: code path
    write_fixture_config(tmp)
    return tmp


def fixture_config_path(tmp: str) -> str:
    return os.path.join(tmp, ".config", "comms-kit", "config.json")


def write_fixture_config(tmp: str) -> str:
    """The kit config for a throwaway home: its archive in <tmp>/.claude/communications and fake
    channel values. Point COMMS_KIT_CONFIG at the returned path."""
    path = fixture_config_path(tmp)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"data_dir": os.path.join(tmp, ".claude", "communications"),
                   "whatsapp": {"matrix_user": ME.split(":")[0].lstrip("@"),
                                "homeserver": "https://example.invalid", "bot_user": BOT,
                                "self_number": SELF_NUMBER, "owner_lid": "4440100",
                                "recipient_guard_dir": os.path.join(tmp, "guards")}}, fh)
    return path


def comms_rows(home: str) -> List[Dict[str, Any]]:
    """Every archive row written under a throwaway home."""
    import glob
    rows = []
    for p in sorted(glob.glob(os.path.join(home, ".claude", "communications", "store", "*.jsonl"))):
        with open(p) as fh:
            rows += [json.loads(line) for line in fh if line.strip()]
    return rows


def gate_rows(home: str) -> List[Dict[str, Any]]:
    path = os.path.join(home, ".claude", "communications", "gate.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]
