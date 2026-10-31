"""One message shape for every comms reader's full read (`--full`).

Why: every reader was written for a human glance (a preview) and then reused by agents as if it
were the full record. A preview may cut; a full read may not. This module is the contract for
the full read: a reader that has to stop early says so (`truncated`, `more`) instead of cutting.

Stdlib only, Python 3.9 compatible (the comms hooks run under /usr/bin/python3 3.9).

    from msgschema import Message, Attachment, Reaction, ReplyRef, envelope, dumps
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

SCHEMA = "msgschema/1"


@dataclass
class Attachment:
    id: Optional[str] = None          # mxc:// uri, Slack file id, iMessage filename/guid
    name: Optional[str] = None
    mime: Optional[str] = None
    size: Optional[int] = None
    local_path: Optional[str] = None  # set only when the file is on this Mac
    transcript: Optional[str] = None
    # ok | off (asked not to) | unavailable (tried, failed) | not_read_here | none (not audio)
    transcript_status: str = "none"


@dataclass
class Reaction:
    key: str
    sender: str


@dataclass
class ReplyRef:
    id: Optional[str]
    text: Optional[str] = None
    deleted: bool = False
    incomplete: bool = False          # parent not in the window and not fetched


@dataclass
class Edit:
    ts: Optional[float]
    text: str


@dataclass
class Message:
    id: str
    chat_id: str
    channel: str
    sender: str                       # human label ("Me" for the owner where the channel has one)
    sender_id: Optional[str] = None   # raw id (mxid, Slack user id, handle)
    is_from_me: bool = False
    ts: Optional[float] = None        # epoch seconds
    text: str = ""                    # FULL text, never cut
    reply_to: Optional[ReplyRef] = None
    edits: List[Edit] = field(default_factory=list)
    deleted: bool = False
    reactions: List[Reaction] = field(default_factory=list)
    attachments: List[Attachment] = field(default_factory=list)
    truncated: bool = False
    more: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def envelope(channel: str, chat: Dict[str, Any], messages: List[Message], *,
             complete: bool, truncated: bool = False, more: Optional[str] = None,
             **extra: Any) -> Dict[str, Any]:
    """One chat's full read. `complete` is True only when nothing in the asked window was skipped."""
    out = {
        "schema": SCHEMA,
        "channel": channel,
        "chat": chat,
        "messages": [m.to_dict() for m in messages],
        "count": len(messages),
        "complete": bool(complete),
        "truncated": bool(truncated),
        "more": more,
    }
    out.update(extra)
    return out


def rooms_envelope(channel: str, rooms: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Several chats at once (`unread --full`). Each room dict carries its own messages/complete."""
    return {"schema": SCHEMA, "channel": channel, "rooms": rooms,
            "count": sum(len(r.get("messages") or []) for r in rooms)}


def room_entry(chat: Dict[str, Any], messages: List[Message], *, complete: bool,
               truncated: bool = False, more: Optional[str] = None,
               error: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    out = {"chat": chat, "messages": [m.to_dict() for m in messages], "complete": bool(complete),
           "truncated": bool(truncated), "more": more, "error": error}
    out.update(extra)
    return out


def dumps(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=False)


def validate(obj: Dict[str, Any]) -> List[str]:
    """Problems with a full-read envelope, [] when it is well formed. Used by tests and selftest."""
    problems = []
    if obj.get("schema") != SCHEMA:
        problems.append("schema is %r" % obj.get("schema"))
    rooms = obj.get("rooms") if "rooms" in obj else [obj]
    for r in rooms:
        msgs = r.get("messages")
        if not isinstance(msgs, list):
            problems.append("messages is not a list")
            continue
        if r.get("truncated") and not r.get("more"):
            problems.append("truncated without a `more` hint")
        for m in msgs:
            for key in ("id", "chat_id", "channel", "sender", "text"):
                if key not in m:
                    problems.append("message missing %s" % key)
            if m.get("truncated") and not m.get("more"):
                problems.append("message %s truncated without a `more` hint" % m.get("id"))
    return problems
