#!/usr/bin/env python3
"""
WhatsApp (mautrix) CLI — read WhatsApp and perform explicitly confirmed sends
through a mautrix-whatsapp bridge over the Matrix client-server API.

This talks to a continuwuity homeserver (set `whatsapp.homeserver`) that
hosts a mautrix-whatsapp bridge. It started life (v1) as a deliberately READ-ONLY
shim. v2 added guarded text sending. v3 added media download, transcription, and
sending. v4 added GROUP-CHAT support. v5 added native cloned voice memos. v6
made voice sending a mandatory prepare/listen/confirm flow. v7 added performance
tags and faint fan ambience. v8 isolated Eleven v3 to short reaction inserts.
v9 routed ordinary speech through a Fish professional clone. v10 moves both
speech and reactions onto the ElevenLabs Professional Voice Clone the loom
pipeline blind-A/B'd on 2026-08-16 (same voice, model and settings), verifies
the clone's identity before spending credits, and preserves the exact-byte
approval boundary. Every other command remains read-only.

  *** SAFETY: In mautrix, plain text/media posted into a PORTAL room is relayed to  ***
  *** the real WhatsApp contact(s). The ONLY two CONTENT portal-write sites are     ***
  *** `cmd_send` (text) and `_upload_and_send_media` (files/voice). The user-facing ***
  *** `send` and `send-media` commands preview and require --yes. `send-voice` only ***
  *** PREPARES cached audio; only `send-voice-confirm <preview-id> --yes` can relay ***
  *** those exact hash-checked files. `download` and `transcribe` are READ-ONLY:    ***
  *** Transcription POSTs only to local whisper; most other commands only PUT bot  ***
  *** COMMANDS into the MANAGEMENT room (list-logins / search / start-chat /        ***
  *** create-portal). The content write sites are marked below.                     ***
  ***                                                                              ***
  *** THIRD portal-write — `sync` (cmd_sync), categorically different: it PUTs a    ***
  *** `!wa sync-portal` BOT COMMAND (prefixed) into the portal, NOT relayed         ***
  *** content. The bridge intercepts the `!wa ` prefix and runs it as a command;    ***
  *** it is NOT delivered to the WhatsApp contact(s). This was VALIDATED live in    ***
  *** the self-DM ("Message Yourself") before shipping: a `!wa id` was consumed as  ***
  *** a command (the bot answered, with no relay / no "not bridged" notice). It is  ***
  *** the ONLY portal write beyond the two content-write helpers above, and it only ***
  *** sends the fixed literal `!wa sync-portal` (never arbitrary text).             ***

Connects from two locations:
    - On the box itself:   MATRIX_HOMESERVER_URL=http://127.0.0.1:6167
                           (auto-loads token/room from /opt/matrix-stack/.login_session)
    - From the Tailscale mesh (e.g. the Mac): `whatsapp.homeserver`
                           from the kit config  + a `login` session

Usage:
    python whatsapp_mautrix.py login                      # cache a session (Tailscale/Mac)
    python whatsapp_mautrix.py status                     # reachability + WA login state
    python whatsapp_mautrix.py search "Acme Studio"  # find a WA contact's number
    python whatsapp_mautrix.py history 12065550123 -n 20  # read recent msgs (voice notes auto-transcribed)
    python whatsapp_mautrix.py history 12065550123 --no-transcribe  # skip transcription (faster)
    python whatsapp_mautrix.py history "!roomid..." -n 6  # read a GROUP by room_id
    python whatsapp_mautrix.py history "Book Club" -n 6       # read a GROUP (or DM) by name
    python whatsapp_mautrix.py chats -n 20                # list portal rooms (tagged [group]/[dm])
    python whatsapp_mautrix.py groups                     # list materialized GROUP portals (read-only)
    python whatsapp_mautrix.py unread                     # rooms with unread messages
    python whatsapp_mautrix.py send <self-number> "hi"      # PREVIEW only (dry-run, no send)
    python whatsapp_mautrix.py send <self-number> "hi" --since <ts> --yes  # send; <ts> from the preview
    python whatsapp_mautrix.py send "!roomid..." "hi"     # PREVIEW a GROUP send (dry-run)
    python whatsapp_mautrix.py download <self-number> -n 1 --out ./dl  # save recent media (read-only)
    python whatsapp_mautrix.py send-media <self-number> pic.png         # PREVIEW only (dry-run)
    python whatsapp_mautrix.py send-media <self-number> pic.png --since <ts> --yes  # send; <ts> from the preview
    python whatsapp_mautrix.py send-voice <self-number> "Quick update"  # generate saved previews only
    python whatsapp_mautrix.py send-voice <self-number> "[laughs] Okay" --expressive  # v3 reaction
    python whatsapp_mautrix.py send-voice-confirm <preview-id>          # re-show target/text/audio
    python whatsapp_mautrix.py send-voice-confirm <preview-id> --yes    # send exact approved files
    python whatsapp_mautrix.py transcribe <self-number>     # transcribe latest voice note (read-only)
    python whatsapp_mautrix.py sync "!roomid..."          # refresh a portal's metadata (!wa command)

Config (env vars, all optional — sensible defaults / session fallback):
    MATRIX_HOMESERVER_URL   default: `whatsapp.homeserver` in the kit config
    MATRIX_ACCESS_TOKEN     bearer token; falls back to session file / box login session
    MATRIX_MGMT_ROOM        management room_id; same fallback chain
    WHISPER_URL             transcribe service; if unset, derived from the homeserver:
                            Tailscale homeserver -> https://<same-host>:8765,
                            else (localhost box)  -> http://127.0.0.1:8765
    WHATSAPP_VOICE_PROVIDER must remain elevenlabs; any override fails before generation
    WHATSAPP_ELEVEN_MODEL must remain eleven_multilingual_v2; overrides fail early
    WHATSAPP_VOICE_ENV_FILE defaults to `whatsapp.voice_env_file`: the private .env that
                            holds the professional clone's voice id
    WHATSAPP_VOICE_PREVIEW_DIR defaults to a private OS temporary cache directory
    WHATSAPP_VOICE_BACKGROUND is pinned to the approved office/fan room tone
"""

import os
import sys
import json
import time
import uuid
import hashlib
import html
import re
import shutil
import mimetypes
import tempfile
import array
import wave
import ssl
import subprocess
import argparse
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime


# --- Kit core -----------------------------------------------------------------
# commslib, outbound_gate, msgschema and kitconfig live in the kit's `core` folder. Find
# it in this order: $COMMS_KIT_CORE, <this file>/../../core (the repo layout), then
# ~/.claude/communications (the live layout). abspath, never realpath: a private sibling
# file must still be found next to this file.

def _core_dir(module="commslib"):
    """The first core folder that holds <module>.py; the live folder when none does."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("COMMS_KIT_CORE") or "",
        os.path.join(here, os.pardir, os.pardir, "core"),  # kit: code path
        "~/.claude/communications",
    ]
    folders = [os.path.abspath(os.path.expanduser(c)) for c in candidates if c]
    for folder in folders:
        if os.path.isfile(os.path.join(folder, module + ".py")):
            return folder
    return folders[-1]


def _load_core(name):
    """core/<name>.py, loaded by exact path (None when it cannot load)."""
    import importlib.util  # noqa: PLC0415
    path = os.path.join(_core_dir(name), name + ".py")
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


kitconfig = _load_core("kitconfig")
if kitconfig is None:
    sys.exit("whatsapp: cannot load kitconfig.py from the kit core "
             f"({_core_dir('kitconfig')}). Set COMMS_KIT_CORE to the core folder.")


def _read_settings():
    """The config values read once at import. A broken config file reads as empty here;
    every safety check re-reads through kitconfig.require, which raises, and main() ends
    the command with the file's message and exit 2."""
    try:
        return {
            "homeserver": kitconfig.get("whatsapp.homeserver", ""),
            "bot_user": kitconfig.get("whatsapp.bot_user", ""),
            "matrix_user": kitconfig.get("whatsapp.matrix_user", ""),
            "voice_env_file": kitconfig.get("whatsapp.voice_env_file", ""),
            "voice_background": kitconfig.get("whatsapp.voice_background", ""),
            "owner_lid": kitconfig.get("whatsapp.owner_lid", ""),
        }
    except kitconfig.ConfigError:
        return {}


_SETTINGS = _read_settings()


# Copy of loom-pipeline-system loom_pipeline/loom/loom_tts_pronounce.py (2026-10-25). Keep the tables in step: a new misread word goes in every copy.
# Applied only to the text in the ElevenLabs request body (see _generate_eleven_voice_wav).
# Words that have one sense in sales/automation narration. "lead" the metal and "led"
# (spelled differently) never come up; "lead" the verb sounds like the noun.
TTS_ALWAYS = {
    "lead": "leed",
    "leads": "leeds",
    "leadgen": "leedgen",
}

# "live" is /laɪv/ in "goes live" / "a live demo" but /lɪv/ in "lives in Airtable",
# so it is respelled only next to a word that fixes the adjective sense.
_TTS_LIVE_AFTER = ("go", "goes", "going", "went", "gone",
               "is", "are", "was", "were", "be", "been", "being")
_TTS_LIVE_BEFORE = ("chat", "data", "demo", "dashboard", "feed", "preview", "view", "call",
                "calls", "stream", "site", "version", "update", "updates", "link",
                "test", "status", "traffic")
_TTS_LIVE_SPELLING = "lyve"

_TTS_ALWAYS_RE = re.compile(r"\b(" + "|".join(TTS_ALWAYS) + r")\b", re.IGNORECASE)
_TTS_LIVE_AFTER_RE = re.compile(r"\b((?:" + "|".join(_TTS_LIVE_AFTER) + r")\s+)(live)\b",
                            re.IGNORECASE)
_TTS_LIVE_BEFORE_RE = re.compile(r"\b(live)(?=\s+(?:" + "|".join(_TTS_LIVE_BEFORE) + r")\b)",
                             re.IGNORECASE)


def _tts_same_case(original: str, spelling: str) -> str:
    if original.isupper():
        return spelling.upper()
    if original[0].isupper():
        return spelling[0].upper() + spelling[1:]
    return spelling


def tts_speakable(text: str) -> str:
    """The text as the TTS provider should get it."""
    text = _TTS_ALWAYS_RE.sub(lambda m: _tts_same_case(m[1], TTS_ALWAYS[m[1].lower()]), text)
    text = _TTS_LIVE_AFTER_RE.sub(lambda m: m[1] + _tts_same_case(m[2], _TTS_LIVE_SPELLING), text)
    return _TTS_LIVE_BEFORE_RE.sub(lambda m: _tts_same_case(m[1], _TTS_LIVE_SPELLING), text)


# --- Config / constants -------------------------------------------------------

# The homeserver comes from MATRIX_HOMESERVER_URL, else `whatsapp.homeserver`. When both are
# empty, the first request stops with one line naming the missing key (see _homeserver).
DEFAULT_HOMESERVER = str(_SETTINGS.get("homeserver", "")).strip()
HOMESERVER_URL = os.environ.get("MATRIX_HOMESERVER_URL", DEFAULT_HOMESERVER).rstrip("/")


def _homeserver():
    return HOMESERVER_URL or str(kitconfig.require("whatsapp.homeserver")).rstrip("/")


# Whisper transcription microservice port (see `transcribe` / auto-transcription).
WHISPER_PORT = 8765
# Box-local default; used when the homeserver is localhost-ish.
DEFAULT_WHISPER_URL = f"http://127.0.0.1:{WHISPER_PORT}"


def _default_whisper_url(homeserver_url):
    """Derive a sensible whisper URL from the homeserver context.

    The whisper service runs on the SAME box as the homeserver/bridge:
      - On the box itself the homeserver is localhost -> http://127.0.0.1:8765.
      - Over the Tailscale mesh (homeserver host contains `ts.net`) the box is
        reachable at the same Tailscale host -> https://<host>:8765.
    Returning a Tailscale URL lets auto-transcription "just work" from the Mac
    with no manual WHISPER_URL. WHISPER_URL env always overrides this (below).
    """
    parsed = urllib.parse.urlparse(homeserver_url)
    host = parsed.hostname or ""
    if host.endswith("ts.net"):
        return f"https://{host}:{WHISPER_PORT}"
    return DEFAULT_WHISPER_URL


# WHISPER_URL env overrides; else derive from the effective homeserver context
# so the box (localhost) and the Mac (Tailscale) both work with no config.
WHISPER_URL = os.environ.get(
    "WHISPER_URL", _default_whisper_url(HOMESERVER_URL)
).rstrip("/")

BOT_USER = str(_SETTINGS.get("bot_user", "")).strip()  # the bridge bot's Matrix id
LOGIN_USER = str(_SETTINGS.get("matrix_user", "")).strip()  # Matrix localpart for `login`


def _bot_user():
    return BOT_USER or str(kitconfig.require("whatsapp.bot_user"))


def _login_user():
    return LOGIN_USER or str(kitconfig.require("whatsapp.matrix_user"))


def _owner_prefix():
    """`@<matrix user>`: the Matrix ids this tool sends as. Compared against in safety
    checks, so a missing key stops the command; an empty prefix would match everyone."""
    return "@" + str(kitconfig.require("whatsapp.matrix_user"))

# Session file written by `login` (Mac / Tailscale). chmod 600.
SESSION_FILE = os.path.expanduser("~/.config/whatsapp-mautrix/session.json")
# Session cached on the box by the bridge setup. {"token": "...", "room": "<mgmt_room_id>"}
BOX_SESSION_FILE = "/opt/matrix-stack/.login_session"

# How long to wait for an async bot reply in the management room.
REPLY_TIMEOUT_S = 15
REPLY_POLL_INTERVAL_S = 1.5

APPROVED_VOICE_PROVIDER = "elevenlabs"
# A blind A/B (2026-08-16) put the owner's ElevenLabs Professional
# Voice Clone ahead of both the older 30-second instant clone and Fish. Speech
# here uses the SAME voice, model, and settings as that winning read.
APPROVED_SPEECH_MODEL = "eleven_multilingual_v2"
# The PVC is only fine-tuned for the v2-era models; requesting anything else
# quietly changes the voice, so the model is locked exactly like the voice is.
VOICE_PROVIDERS = frozenset({APPROVED_VOICE_PROVIDER})
VOICE_PROVIDER = os.environ.get(
    "WHATSAPP_VOICE_PROVIDER", APPROVED_VOICE_PROVIDER
).strip().lower()
SPEECH_MODEL = os.environ.get(
    "WHATSAPP_ELEVEN_MODEL", APPROVED_SPEECH_MODEL
).strip()
ELEVEN_API_ROOT = "https://api.elevenlabs.io"
# Default (128k MP3) output format, matching the loom pipeline's approved call;
# every part is re-encoded to 48 kHz Opus below, so a higher tier-gated format
# would not survive to WhatsApp anyway.
# loom_pipeline/loom/upwork_loom_video.py::generate_elevenlabs_audio is the
# source of these three values. Re-run that A/B before touching them.
# 2026-10-26: moved to the loom's "+3" one-call values (was .48/.88/.07). The owner
# picked +3 by ear for the loom voice and asked that every tool on this clone match.
APPROVED_SPEECH_SETTINGS = {
    "stability": 0.28,
    "similarity_boost": 0.88,
    "style": 0.35,
    "use_speaker_boost": True,
}
# eleven_v3 only accepts stability 0.0 / 0.5 / 1.0 and ignores style.
APPROVED_REACTION_SETTINGS = {
    "stability": 0.5,
    "similarity_boost": 0.88,
    "use_speaker_boost": True,
}
# The PVC id lives in one private .env (`whatsapp.voice_env_file`), the one place it is
# kept current. Another env file can hold an OLD instant clone under the same variable
# name: reading from there would silently generate the wrong voice, which is why the
# identity check below refuses anything but a professional clone. The variable name is
# `whatsapp.voice_id_var`; `whatsapp.voice_id` can hold the id itself.
DEFAULT_VOICE_ENV_FILE = os.path.expanduser(str(_SETTINGS.get("voice_env_file", "")))
VOICE_ENV_FILE = os.path.abspath(os.path.expanduser(
    os.environ.get("WHATSAPP_VOICE_ENV_FILE", DEFAULT_VOICE_ENV_FILE)
)) if (os.environ.get("WHATSAPP_VOICE_ENV_FILE") or DEFAULT_VOICE_ENV_FILE) else ""
APPROVED_VOICE_CATEGORY = "professional"
# Room tone is a plain audio asset, not voice data. `whatsapp.voice_background` can move it.
DEFAULT_VOICE_BACKGROUND = os.path.abspath(os.path.expanduser(
    str(_SETTINGS.get("voice_background", ""))
    or "~/.claude/skills/video-walkthrough/assets/office-room-tone.wav"
))
VOICE_BACKGROUND_PATH = DEFAULT_VOICE_BACKGROUND
VOICE_BACKGROUND_LUFS = -42
VOICE_REACTION_PROVIDER = "elevenlabs"
VOICE_EXPRESSIVE_MODEL = "eleven_v3"
VOICE_HYBRID_MODEL = "eleven-multilingual-v2-speech-eleven-v3-reactions"
VOICE_REACTION_GAP_MS = 80
VOICE_REACTION_TAG_PATTERN = re.compile(
    r"\[(?:laughs|giggles|chuckles)\]",
    re.IGNORECASE,
)
VOICE_AUDIO_TAG_PATTERN = re.compile(
    r"(?<!\[)\[[A-Za-z][^\r\n]{0,60}?\](?!\])"
)
VOICE_NOTE_MIMETYPE = "audio/ogg; codecs=opus"
VOICE_WAVEFORM_SAMPLES = 64
VOICE_NOTE_LIMIT_MS = 60_000
# Generate below the hard limit so Opus container padding cannot cross 60 seconds.
VOICE_NOTE_TARGET_MS = 59_000
VOICE_PRE_SPLIT_WORDS = 100
# v3: previews carry the ElevenLabs voice identity and settings, so any preview
# prepared by the older Fish route can no longer be confirmed and sent.
VOICE_PREVIEW_VERSION = 3
DEFAULT_VOICE_PREVIEW_ROOT = os.path.join(
    tempfile.gettempdir(), "whatsapp-mautrix-voice-previews"
)
VOICE_PREVIEW_ROOT = os.path.abspath(os.path.expanduser(
    os.environ.get("WHATSAPP_VOICE_PREVIEW_DIR", DEFAULT_VOICE_PREVIEW_ROOT)
))


# --- HTTP / Matrix helpers ----------------------------------------------------

def _load_session():
    """Resolve (access_token, mgmt_room) from env, then session files.

    Priority:
      1. MATRIX_ACCESS_TOKEN / MATRIX_MGMT_ROOM env vars.
      2. ~/.config/whatsapp-mautrix/session.json (written by `login`).
      3. /opt/matrix-stack/.login_session (present when running on the box).
    Missing values are merged across sources (e.g. env token + file room).
    """
    token = os.environ.get("MATRIX_ACCESS_TOKEN", "")
    room = os.environ.get("MATRIX_MGMT_ROOM", "")

    for path in (SESSION_FILE, BOX_SESSION_FILE):
        if token and room:
            break
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        # session.json uses "mgmt_room"; the box file uses "room".
        token = token or data.get("token", "")
        room = room or data.get("mgmt_room") or data.get("room", "")

    return token, room


def _request(method, path, token=None, body=None, params=None):
    """Make a Matrix client-server API request and return parsed JSON.

    `path` is the API path (e.g. /_matrix/client/v3/joined_rooms). Caller is
    responsible for URL-encoding any room IDs embedded in `path`.
    """
    url = f"{_homeserver()}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise MatrixError(e.code, detail) from None
    except urllib.error.URLError as e:
        raise ConnectionError(f"Cannot reach homeserver {_homeserver()}: {e.reason}") from None


class MatrixError(Exception):
    """A non-2xx response from the homeserver."""

    def __init__(self, status, detail):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


class VoiceMemoError(Exception):
    """Voice preview, generation, encoding, or confirmation failed."""


class VoiceConfigError(VoiceMemoError):
    """A voice credential or setting is missing. The command prints one line and exits 2."""


def _encode_room(room_id):
    """URL-encode a room ID (contains '!' and ':')."""
    return urllib.parse.quote(room_id, safe="")


def _format_ts(ms):
    """Format an origin_server_ts (ms epoch) as local time."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return str(ms)


def _require_session():
    """Return (token, mgmt_room) or exit with a helpful message if unset."""
    token, room = _load_session()
    if not token:
        print("No access token. Run `whatsapp_mautrix.py login` (Mac/Tailscale),")
        print("or run on the box where /opt/matrix-stack/.login_session exists,")
        print("or set MATRIX_ACCESS_TOKEN.")
        sys.exit(1)
    return token, room


# --- Bridge-bot command/reply plumbing (MANAGEMENT room only) -----------------

def _latest_event_id(token, room):
    """Return the most recent event_id in the management room (for reply tracking)."""
    data = _request(
        "GET",
        f"/_matrix/client/v3/rooms/{_encode_room(room)}/messages",
        token=token,
        params={"dir": "b", "limit": 1},
    )
    chunk = data.get("chunk", [])
    return chunk[0].get("event_id") if chunk else None


def _send_mgmt_command(token, room, command):
    """Post a bot COMMAND into the MANAGEMENT room and return its event_id.

    SAFETY: This is the ONLY function in this module that PUTs m.room.message,
    and `room` MUST be the management room. It is used solely for read-only bot
    commands (list-logins / search / start-chat). It is never called with a
    portal room, and there is no public subcommand that sends arbitrary text.
    """
    txn = f"wamx{int(time.time() * 1000)}"
    _request(
        "PUT",
        f"/_matrix/client/v3/rooms/{_encode_room(room)}/send/m.room.message/{txn}",
        token=token,
        body={"msgtype": "m.text", "body": command},
    )
    return txn


def _await_bot_reply(token, room, since_event_id):
    """Poll the management room for a NEW @whatsappbot message after `since_event_id`.

    Returns the reply body string, or None on timeout. Replies are async, so we
    collect the most-recent events, find any from the bot that we have not seen
    before (i.e. newer than `since_event_id`), and return the newest such body.
    """
    deadline = time.time() + REPLY_TIMEOUT_S
    while time.time() < deadline:
        time.sleep(REPLY_POLL_INTERVAL_S)
        data = _request(
            "GET",
            f"/_matrix/client/v3/rooms/{_encode_room(room)}/messages",
            token=token,
            params={"dir": "b", "limit": 10},
        )
        # chunk is newest -> oldest. Walk newest-first; stop at the marker event.
        for event in data.get("chunk", []):
            if event.get("event_id") == since_event_id:
                break
            if (
                event.get("type") == "m.room.message"
                and event.get("sender", "").startswith("@whatsappbot")
            ):
                return event.get("content", {}).get("body", "")
    return None


# --- Reply parsers ------------------------------------------------------------

def _parse_search_results(body):
    """Parse a `search` reply into [(number, display)] tuples.

    Bot format:
        Search results:

        * `12065550123` / +12065550123 (WA)
    """
    results = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line.startswith("*"):
            continue
        # * `<number>` / <display>
        if "`" not in line:
            continue
        try:
            number = line.split("`")[1]
        except IndexError:
            continue
        display = ""
        if "/" in line:
            display = line.split("/", 1)[1].strip()
        results.append((number, display))
    return results


def _parse_room_id_from_start_chat(body):
    """Extract the portal room_id from a `start-chat` reply.

    Bot format:
        Created chat with ... (https://matrix.to/#/!os0S...:server)
    Returns the room_id (starts with '!') or None.
    """
    marker = "matrix.to/#/"
    if not body or marker not in body:
        return None
    tail = body.split(marker, 1)[1]
    # room_id runs until the closing ')' or whitespace.
    room_id = tail.split(")", 1)[0].split()[0].strip()
    room_id = urllib.parse.unquote(room_id)
    return room_id if room_id.startswith("!") else None


# --- Read helpers -------------------------------------------------------------

def _room_name(token, room_id):
    """Return a room's m.room.name, or '' if unset.

    A 429 is retried, not swallowed: the name lookups run in parallel, and a
    rate-limited room returned as '' looks nameless, so a name match could miss
    it and fall through to the contact-search/start-chat path.
    """
    for attempt in range(4):
        try:
            data = _request(
                "GET",
                f"/_matrix/client/v3/rooms/{_encode_room(room_id)}/state/m.room.name/",
                token=token,
            )
            return data.get("name", "")
        except MatrixError as e:
            if e.status != 429 or attempt == 3:
                return ""
            try:
                wait_ms = json.loads(e.detail).get("retry_after_ms", 1000)
            except (ValueError, AttributeError):
                wait_ms = 1000
            time.sleep(min(max(wait_ms, 100), 5000) / 1000)
    return ""


# Per-room GETs cost ~0.3s each over the tailnet and there are 160+ joined rooms,
# so a sequential loop took ~50s per name lookup and ~120s for `chats` (measured
# 2026-09-22). A bounded pool makes it ~3.5s with identical results.
_ROOM_WORKERS = 16


def _map_parallel(fn, items):
    """Return [fn(item) for item in items], run on a bounded thread pool.

    Order is kept. Only for GET-only per-room reads: callers must resolve
    `_self_number` BEFORE the pool, or every thread posts `list-logins`.
    """
    items = list(items)
    if len(items) < 2:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(_ROOM_WORKERS, len(items))) as pool:
        return list(pool.map(fn, items))


def _last_message(token, room_id):
    """Return (body, origin_server_ts) of the latest m.room.message, or ('', 0)."""
    event = _last_message_event(token, room_id)
    if event:
        return event.get("content", {}).get("body", ""), event.get("origin_server_ts", 0)
    return "", 0


def _last_message_event(token, room_id):
    """Return the latest m.room.message EVENT (full dict) in a room, or None.

    GET-only. Used where the caller needs more than (body, ts) — e.g. the sender
    and msgtype, so a voice-note preview can be transcribed inline.
    """
    data = _request(
        "GET",
        f"/_matrix/client/v3/rooms/{_encode_room(room_id)}/messages",
        token=token,
        params={"dir": "b", "limit": 5},
    )
    for event in data.get("chunk", []):
        if event.get("type") == "m.room.message":
            return event
    return None


def _sender_label(sender):
    """Map a Matrix sender MXID to a human label."""
    if sender.startswith(_owner_prefix()):
        return "Me"
    if sender.startswith("@whatsapp_"):
        # @whatsapp_<num>:server -> contact's number
        num = sender[len("@whatsapp_"):].split(":", 1)[0]
        return f"+{num}"
    if sender.startswith("@whatsappbot"):
        return "bot"
    return sender


def _resolve_portal(token, room, identifier):
    """Resolve an identifier to a JOINED portal room_id (or exit non-zero).

    Shared start-chat flow used by `history`, `download`, `transcribe` (read) and
    `send-media` (write). SAFETY: `start-chat` goes to the MANAGEMENT room (never a
    portal); we then JOIN the portal. Joining + reading are safe — only the
    explicit message-PUT in `send`/`send-media` ever relays to the contact.

    A bare number triggers a WhatsApp contact-sync (usync) query that fails for
    some numbers (notably your own); the full `<number>@s.whatsapp.net` JID skips
    that lookup, so append it for all-digit identifiers.
    """
    chat_arg = (f"{identifier}@s.whatsapp.net"
                if identifier.isdigit() else identifier)
    marker = _latest_event_id(token, room)
    _send_mgmt_command(token, room, f"start-chat {chat_arg}")
    reply = _await_bot_reply(token, room, marker)
    if reply is None:
        print("No reply from bridge bot within timeout.")
        sys.exit(1)

    portal_id = _parse_room_id_from_start_chat(reply)
    if not portal_id:
        print(f"Could not resolve a chat for '{identifier}'. Bot said:")
        print(f"  {reply.strip()}")
        sys.exit(1)

    # Join the portal (use rooms/{id}/join — continuwuity 400s on /join/{id}).
    try:
        _request("POST", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/join", token=token)
    except MatrixError as e:
        print(f"Could not join portal {portal_id}: {e}")
        sys.exit(1)
    return portal_id


def _join_portal(token, portal_id):
    """JOIN a known portal room_id (idempotent). Read/join only — never writes content.

    Used when we already hold a portal/room id (a group `!roomid` or a name match)
    and just need to be a member to GET its timeline. SAFETY: joining a portal does
    NOT relay anything; only the explicit message-PUTs in send/send-media/sync touch
    a portal's content/commands.
    """
    try:
        _request("POST", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/join", token=token)
    except MatrixError as e:
        print(f"Could not join portal {portal_id}: {e}")
        sys.exit(1)
    return portal_id


def _accept_portal_invites(token, mgmt_room):
    """Auto-join any PENDING portal invites — best-effort, READ-ONLY w.r.t. contacts.

    AUTONOMY: when a new chat/group gets activity, the bridge creates the portal and
    INVITES this user; until the invite is accepted the portal is invisible to
    `joined_rooms` (so `groups`/`chats`/`unread` miss it). Calling this first makes
    new chats appear on their own with no manual `create-portal`. SAFETY: accepting
    an invite only JOINs the room — it never relays anything to a WhatsApp contact
    (only the guarded PUTs in send/send-media/sync touch a portal). Failures are
    skipped silently (best-effort); returns the list of newly joined room_ids.
    """
    try:
        data = _request(
            "GET", "/_matrix/client/v3/sync", token=token,
            params={
                "filter": json.dumps({
                    "room": {"timeline": {"limit": 1}},
                    "presence": {"types": []},
                    "account_data": {"types": []},
                }),
                "timeout": "0",
            },
        )
    except (MatrixError, ConnectionError):
        return []
    invited = data.get("rooms", {}).get("invite", {})
    joined = []
    for room_id in invited:
        if room_id == mgmt_room:
            continue
        try:
            _request("POST", f"/_matrix/client/v3/rooms/{_encode_room(room_id)}/join", token=token)
            joined.append(room_id)
        except (MatrixError, ConnectionError):
            continue
    return joined


def _sync_portals(token, timeline_limit=15):
    """ONE /sync call returning parsed data for every JOINED room. GET-only.

    Fast path for list commands: rather than N sequential GETs per room (name +
    bridge state + last message) — which is painfully slow over a high-latency link
    with 100+ rooms — a single filtered /sync returns each room's name, bridge JID
    (for DM/group classification), unread count, and recent timeline in one round
    trip. Returns {room_id: {"name", "jid", "unread", "timeline"}} where timeline is
    the recent events oldest->newest. The authoritative WhatsApp JID comes from the
    `m.bridge`/`uk.half-shot.bridge` state event's channel.id (`@g.us` group,
    `@s.whatsapp.net` DM) — same signal as `_portal_channel_jid`, just batched.

    /sync splits a room's state: `state` holds it as of the START of the returned
    timeline, and any state event inside the timeline window arrives only there.
    The bridge re-sends `m.bridge` often, so reading `state` alone missed 38 of 51
    groups (measured 2026-09-22). Timeline state events are applied after it, in
    order, so the newest wins.
    """
    filt = json.dumps({
        "room": {
            "state": {"types": ["m.room.name", "m.bridge", "uk.half-shot.bridge"]},
            "timeline": {"limit": timeline_limit},
            "ephemeral": {"types": []},
        },
        "presence": {"types": []},
        "account_data": {"types": []},
    })
    data = _request(
        "GET", "/_matrix/client/v3/sync", token=token,
        params={"filter": filt, "timeout": "0"},
    )
    out = {}
    for room_id, rd in data.get("rooms", {}).get("join", {}).items():
        name, jid = "", ""
        for ev in rd.get("state", {}).get("events", []):
            etype = ev.get("type")
            content = ev.get("content", {}) or {}
            if etype == "m.room.name":
                name = content.get("name", "") or name
            elif etype in ("m.bridge", "uk.half-shot.bridge"):
                jid = (content.get("channel", {}) or {}).get("id", "") or jid
        for ev in rd.get("timeline", {}).get("events", []):
            if "state_key" not in ev:
                continue  # a message, not a state change
            etype = ev.get("type")
            content = ev.get("content", {}) or {}
            if etype == "m.room.name":
                name = content.get("name", "")
            elif etype in ("m.bridge", "uk.half-shot.bridge"):
                jid = (content.get("channel", {}) or {}).get("id", "") or jid
        out[room_id] = {
            "name": name,
            "jid": jid,
            "unread": rd.get("unread_notifications", {}).get("notification_count", 0),
            "timeline": rd.get("timeline", {}).get("events", []),
        }
    return out


# --- Group classification (READ-ONLY; GETs room state / joined_members only) ---

# Two signals, in order of authority:
#
#  1. AUTHORITATIVE — the bridge's own `m.bridge` state event carries the WhatsApp
#     JID in channel.id: a GROUP ends with `@g.us`, a DM with `@s.whatsapp.net`.
#     This is exactly how mautrix-whatsapp models the chat, so we trust it first.
#
#  2. FALLBACK (heuristic) — when the bridge state is missing/unreadable, count the
#     puppets that are NEITHER the bridge bot NOR the SELF puppet. The bridge places
#     the SELF puppet (@whatsapp_<self>) into EVERY portal, so a DM shows exactly the
#     ONE other contact (1), the self-DM none (0), and a group the self puppet PLUS
#     >=2 others. Hence: non-self puppets >= 2 => GROUP; <= 1 => DM. (NB: a small
#     group with stale/partial membership can momentarily under-count; signal #1
#     avoids that, and a `sync` refreshes membership.)
#
# The self number is read once (list-logins) and cached for the run; each room's
# classification is cached too (state/members are GETs per room).
_SELF_NUMBER = None
_GROUP_CACHE = {}

# The self number is fixed for a login, but reading it is a `list-logins` bot round
# trip (~2.5s, measured 2026-09-22) on every command. So it is also cached on disk,
# keyed to the access token + management room; `status` refreshes it.
SELF_NUMBER_CACHE = os.path.join(os.path.dirname(SESSION_FILE), "self_number.json")


def _self_number_key(token, room):
    return hashlib.sha256(f"{token}\n{room}".encode("utf-8")).hexdigest()[:16]


def _read_self_number_cache(token, room):
    """Return the cached self number for this token + room, or ''."""
    try:
        with open(SELF_NUMBER_CACHE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict) or data.get("key") != _self_number_key(token, room):
        return ""
    number = str(data.get("self_number", ""))
    return number if number.isdigit() else ""


def _write_self_number_cache(token, room, number):
    """Cache a digits-only self number (chmod 600). A failed write only costs speed."""
    if not number or not number.isdigit():
        return
    tmp = f"{SELF_NUMBER_CACHE}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(SELF_NUMBER_CACHE), mode=0o700, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"key": _self_number_key(token, room), "self_number": number}, f)
        os.replace(tmp, SELF_NUMBER_CACHE)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _parse_self_number(reply):
    """First "* `<number>` ..." line of a `list-logins` reply -> '<number>', else ''."""
    for line in (reply or "").splitlines():
        line = line.strip()
        if line.startswith("*") and "`" in line:
            return line.split("`")[1].strip()
    return ""


def _self_number(token, room):
    """Return the logged-in WhatsApp number (digits), cached for the run, or ''.

    Parsed from the bridge bot's `list-logins` reply (MANAGEMENT room — SAFE),
    e.g. "* `<self-number>` (+<self-number>) - `CONNECTED`". Used only to exclude the
    SELF puppet from group classification; failure degrades to '' (no exclusion).
    Read from the on-disk cache first (see SELF_NUMBER_CACHE).
    """
    global _SELF_NUMBER
    if _SELF_NUMBER is not None:
        return _SELF_NUMBER
    _SELF_NUMBER = ""
    if not room:
        return _SELF_NUMBER
    cached = _read_self_number_cache(token, room)
    if cached:
        _SELF_NUMBER = cached
        return _SELF_NUMBER
    try:
        marker = _latest_event_id(token, room)
        _send_mgmt_command(token, room, "list-logins")
        reply = _await_bot_reply(token, room, marker)
    except (MatrixError, ConnectionError):
        return _SELF_NUMBER
    _SELF_NUMBER = _parse_self_number(reply)
    _write_self_number_cache(token, room, _SELF_NUMBER)
    return _SELF_NUMBER


def _portal_members(token, room_id):
    """Return the joined_members dict for a room (GET only), or {} on error."""
    try:
        return _request(
            "GET",
            f"/_matrix/client/v3/rooms/{_encode_room(room_id)}/joined_members",
            token=token,
        ).get("joined", {})
    except MatrixError:
        return {}


def _portal_channel_jid(token, room_id):
    """Return the portal's WhatsApp JID from the bridge `m.bridge` state, or ''.

    The mautrix `m.bridge` state event records `channel.id` — the WhatsApp JID this
    portal maps to: a GROUP ends with `@g.us`, a DM with `@s.whatsapp.net`. This is
    the AUTHORITATIVE signal for classification. The event's state_key embeds the
    bridge instance (server/whatsapp), so we read full room state and find it rather
    than hardcoding the key. GET-only; '' if the event is absent/unreadable.
    """
    try:
        state = _request(
            "GET",
            f"/_matrix/client/v3/rooms/{_encode_room(room_id)}/state",
            token=token,
        )
    except MatrixError:
        return ""
    for event in state:
        if event.get("type") in ("m.bridge", "uk.half-shot.bridge"):
            jid = (event.get("content", {}) or {}).get("channel", {}).get("id", "")
            if jid:
                return jid
    return ""


def _classify_channel_jid(jid):
    """True if a bridge JID is a GROUP, False if a DM, None if unclassifiable.

    WhatsApp addresses one person two ways: the legacy `<number>@s.whatsapp.net`
    and the newer linked identity `<lid>@lid`, which is what portals carry after
    WhatsApp's LID migration (the owner's own self-DM is `<lid>@lid`). A GROUP
    is always `@g.us`, so `@lid` is necessarily one-to-one — treating it as
    unclassifiable blocked every voice memo to a migrated DM. Anything else
    (e.g. `@newsletter`) stays None so callers fail closed rather than guess.
    """
    if jid.endswith("@g.us"):
        return True
    if jid.endswith("@s.whatsapp.net") or jid.endswith("@lid"):
        return False
    return None


def _other_puppets(token, room_id, self_number):
    """Return the @whatsapp_ puppet MXIDs in a room EXCLUDING the bot and SELF.

    "@whatsappbot..." is the bridge bot (skip). "@whatsapp_<self>..." is your own
    puppet, present in every portal (skip). What remains is the real other party
    (one for a DM) or the group's other members (>=2 for a group).
    """
    members = _portal_members(token, room_id)
    self_prefix = f"@whatsapp_{self_number}:" if self_number else None
    others = []
    for mxid in members:
        if not mxid.startswith("@whatsapp_") or mxid.startswith("@whatsappbot"):
            continue
        if self_prefix and mxid.startswith(self_prefix):
            continue  # this is the SELF puppet
        others.append(mxid)
    return others


def _is_group(token, room_id, self_number=None, mgmt_room=None):
    """True if a portal room is a GROUP chat, False if a DM. READ-ONLY.

    Prefers the AUTHORITATIVE bridge JID (`channel.id` ends `@g.us` => group,
    `@s.whatsapp.net` => DM); falls back to the non-self puppet count (>=2 => group)
    when no bridge JID is available (see the module note above). Cached per room_id.
    `self_number`/`mgmt_room` let a caller pass values it already has so we avoid
    re-deriving them; otherwise we derive the self number via list-logins.
    """
    if room_id in _GROUP_CACHE:
        return _GROUP_CACHE[room_id]
    if self_number is None:
        self_number = _self_number(token, mgmt_room) if mgmt_room else ""

    classified = _classify_channel_jid(_portal_channel_jid(token, room_id))
    if classified is None:
        # No authoritative JID — fall back to the puppet-count heuristic.
        result = len(_other_puppets(token, room_id, self_number)) >= 2
    else:
        result = classified

    _GROUP_CACHE[room_id] = result
    return result


def _group_label(token, room_id, self_number):
    """Human label for a GROUP portal: its m.room.name, else a member-derived tag.

    Some groups' m.room.name never syncs (the portal shows as its room_id). When
    the name is missing we build a stable "Group: +A, +B (+N more)" label from the
    non-self puppet numbers so the chat is still recognizable without `sync`.
    """
    name = _room_name(token, room_id)
    if name:
        return name
    others = _other_puppets(token, room_id, self_number)
    nums = []
    for mxid in others:
        # @whatsapp_<num>:server  /  @whatsapp_lid-<id>:server  -> readable tag
        tag = mxid[len("@whatsapp_"):].split(":", 1)[0]
        nums.append(f"+{tag}" if tag.isdigit() else tag)
    if not nums:
        return room_id
    shown = ", ".join(nums[:3])
    extra = f" (+{len(nums) - 3} more)" if len(nums) > 3 else ""
    return f"Group: {shown}{extra}"


# --- Unified target resolution (group-aware) ----------------------------------

def _joined_portals(token, mgmt_room):
    """Return [(room_id, name)] for every materialized portal (joined non-admin).

    Excludes the management room and the homeserver Admin Room. Used by the
    name-match branch of `_resolve_chat` and by `cmd_groups`. GET-only.
    """
    joined = _request(
        "GET", "/_matrix/client/v3/joined_rooms", token=token
    ).get("joined_rooms", [])
    rooms = [room_id for room_id in joined if room_id != mgmt_room]
    names = _map_parallel(lambda room_id: _room_name(token, room_id), rooms)
    return [(room_id, name) for room_id, name in zip(rooms, names)
            if "Admin Room" not in name]


def _comms_contact(*args):
    """`comms contact` stdout, or None. The saved-contact directory only adds to name lookup, so
    it never stops a read or a send that works without it."""
    comms = os.path.join(_core_dir("commslib"), "bin", "comms")
    try:
        done = subprocess.run([sys.executable, comms, "contact", *args],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _saved_contact_number(name):
    """The WhatsApp number saved under exactly this name, or None."""
    number = (_comms_contact("get", name, "--channel", "whatsapp") or "").strip()
    return number if re.fullmatch(r"[0-9]{8,15}", number) else None


def _saved_contacts(query):
    """[(number, name)] for saved contacts whose name holds `query`."""
    try:
        rows = json.loads(_comms_contact("list", query, "--json") or "[]")
    except ValueError:
        return []
    return [(row["channels"]["whatsapp"], row["name"]) for row in rows
            if row.get("channels", {}).get("whatsapp")]


def _resolve_chat(token, mgmt_room, identifier):
    """Resolve an identifier to a portal, group-aware. Returns a dict:

        {"portal_id": <room_id>, "label": <str>, "is_group": <bool>}

    or exits non-zero (never guesses among multiple candidates). Shared by the
    read commands (history/download/transcribe) AND the guarded writes
    (send/send-media/sync) so a GROUP name or room_id works everywhere a DM number
    does. SAFETY: every branch only does management-room bot commands + JOIN +
    classification GETs; it NEVER posts content into a portal. The actual guarded
    content PUTs live only in `cmd_send` and `_upload_and_send_media`; the latter
    is reached by guarded `cmd_send_media` and `cmd_send_voice_confirm --yes`.
    The prefixed `!wa` command PUT remains isolated in `cmd_sync`. Branches:

      1. All-digit  -> DM. Existing `_resolve_portal` (appends `@s.whatsapp.net`,
         start-chat in MANAGEMENT room, join). is_group=False, label=number.
      2. `!`-prefixed (room_id) or contains `@g.us` -> a direct portal/group id:
         a room_id is JOINed and used as-is; a `@g.us` id is materialized via
         `create-portal` in the MANAGEMENT room, then parsed + joined. Classified
         via `_is_group`.
      3a. An exact saved contact (`comms contact add`) -> DM to that number.
      3. Otherwise (a NAME) -> case-insensitive substring match against joined
         portal names. EXACTLY one match -> use it directly (groups AND already-
         materialized DMs). ZERO -> fall back to the DM `search` flow (resolve to
         one number, start-chat). MULTIPLE -> print candidates and EXIT non-zero.
    """
    self_number = _self_number(token, mgmt_room)

    # Branch 1: bare number -> DM via the existing start-chat flow.
    if identifier.isdigit():
        portal_id = _resolve_portal(token, mgmt_room, identifier)
        return {"portal_id": portal_id, "label": identifier, "is_group": False}

    # Branch 2a: a literal room_id -> join + use directly.
    if identifier.startswith("!"):
        portal_id = _join_portal(token, identifier)
        is_group = _is_group(token, portal_id, self_number=self_number)
        label = (_group_label(token, portal_id, self_number) if is_group
                 else (_room_name(token, portal_id) or portal_id))
        return {"portal_id": portal_id, "label": label, "is_group": is_group}

    # Branch 2b: a WhatsApp group JID (<id>@g.us) -> create-portal, then join.
    if "@g.us" in identifier:
        marker = _latest_event_id(token, mgmt_room)
        _send_mgmt_command(token, mgmt_room, f"create-portal {identifier}")
        reply = _await_bot_reply(token, mgmt_room, marker)
        if reply is None:
            print("No reply from bridge bot within timeout.")
            sys.exit(1)
        portal_id = _parse_room_id_from_start_chat(reply)
        if not portal_id:
            print(f"Could not materialize a portal for '{identifier}'. Bot said:")
            print(f"  {reply.strip()}")
            sys.exit(1)
        _join_portal(token, portal_id)
        is_group = _is_group(token, portal_id, self_number=self_number)
        label = (_group_label(token, portal_id, self_number) if is_group
                 else (_room_name(token, portal_id) or portal_id))
        return {"portal_id": portal_id, "label": label, "is_group": is_group}

    # Branch 3a: an exact saved contact (`comms contact add`). It beats a substring hit on a room
    # name, which can be a group with that person's name in it.
    saved = _saved_contact_number(identifier)
    if saved:
        portal_id = _resolve_portal(token, mgmt_room, saved)
        return {"portal_id": portal_id, "label": f"{saved} ({identifier})", "is_group": False}

    # Branch 3: a NAME -> substring-match joined portals (case-insensitive).
    needle = identifier.lower()
    matches = [(rid, name) for rid, name in _joined_portals(token, mgmt_room)
               if name and needle in name.lower()]
    if len(matches) == 1:
        portal_id, name = matches[0]
        _join_portal(token, portal_id)
        is_group = _is_group(token, portal_id, self_number=self_number)
        label = (_group_label(token, portal_id, self_number) if is_group else name)
        return {"portal_id": portal_id, "label": label, "is_group": is_group}
    if len(matches) > 1:
        print(f"'{identifier}' is ambiguous ({len(matches)} portal matches) — "
              f"refusing to guess.")
        print("Disambiguate by passing the exact room_id or number:")
        for rid, name in matches:
            print(f"  {name}  [{rid}]")
        sys.exit(1)

    # Branch 3 fallback: ZERO portal-name matches -> existing DM contact search.
    number, display = _resolve_number(token, mgmt_room, identifier)
    portal_id = _resolve_portal(token, mgmt_room, number)
    label = f"{number} ({display})" if display else number
    return {"portal_id": portal_id, "label": label, "is_group": False}


# --- Media helpers (READ paths GET media; the local whisper POST is read-only) -

MEDIA_MSGTYPES = ("m.image", "m.audio", "m.video", "m.file")


def _parse_mxc(mxc):
    """Split an 'mxc://server/mediaId' URI into (server, media_id), or (None, None)."""
    if not isinstance(mxc, str) or not mxc.startswith("mxc://"):
        return None, None
    rest = mxc[len("mxc://"):]
    if "/" not in rest:
        return None, None
    server, media_id = rest.split("/", 1)
    return server, media_id


def _media_events(token, portal_id, limit):
    """Return media m.room.message events (newest -> oldest) from a portal timeline.

    GET only — never posts into the portal. `limit` bounds the backward scan.
    """
    data = _request(
        "GET",
        f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/messages",
        token=token,
        params={"dir": "b", "limit": limit},
    )
    events = []
    for event in data.get("chunk", []):
        if event.get("type") != "m.room.message":
            continue
        content = event.get("content", {})
        if content.get("msgtype") in MEDIA_MSGTYPES and content.get("url"):
            events.append(event)
    return events


def _download_media(token, mxc):
    """Download an mxc URI via the AUTHENTICATED media endpoint. Returns raw bytes.

    Uses GET /_matrix/client/v1/media/download/{server}/{mediaId} with the bearer
    token (continuwuity's authenticated media path). READ-ONLY.
    """
    server, media_id = _parse_mxc(mxc)
    if not server or not media_id:
        raise MatrixError(0, f"Bad mxc URI: {mxc}")
    url = (f"{_homeserver()}/_matrix/client/v1/media/download/"
           f"{urllib.parse.quote(server, safe='')}/"
           f"{urllib.parse.quote(media_id, safe='')}")
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise MatrixError(e.code, detail) from None
    except urllib.error.URLError as e:
        raise ConnectionError(f"Cannot reach homeserver {_homeserver()}: {e.reason}") from None


def _ext_for_mimetype(mimetype, fallback=""):
    """Best-effort file extension (with leading dot) for a mimetype, else `fallback`."""
    if mimetype:
        ext = mimetypes.guess_extension(mimetype.split(";", 1)[0].strip())
        if ext:
            return ext
    return fallback


def _media_filename(event, index):
    """Build a safe local filename for a media event from its body + mimetype."""
    content = event.get("content", {})
    info = content.get("info", {}) or {}
    mimetype = info.get("mimetype", "")
    body = (content.get("body") or "").strip()
    # Use the message body (often the original filename) when it looks like one.
    base = os.path.basename(body) if body else ""
    if base and not os.path.splitext(base)[1]:
        base += _ext_for_mimetype(mimetype)
    if not base:
        msgtype = content.get("msgtype", "media").split(".", 1)[-1]
        base = f"wa_{msgtype}_{index}{_ext_for_mimetype(mimetype, fallback='.bin')}"
    # Strip path separators / keep it tame.
    base = base.replace("/", "_").replace("\\", "_")
    # A WhatsApp caption becomes the body, so `base` can be a whole paragraph.
    # Filesystems cap a path component at 255 bytes (APFS/ext4), so truncate the
    # stem — leaving room for the `_{index}` de-dupe suffix the caller appends.
    stem, ext = os.path.splitext(base)
    max_stem = 200 - len(ext.encode("utf-8"))
    stem_bytes = stem.encode("utf-8")
    if len(stem_bytes) > max_stem:
        stem = stem_bytes[:max_stem].decode("utf-8", "ignore").rstrip()
    return f"{stem}{ext}"


def _upload_media(token, filepath, mimetype, *, raw_bytes=None):
    """Upload bytes to the media repo and return the mxc:// content URI.

    When `raw_bytes` is supplied, the caller has already read and validated the
    exact payload; this function must not reopen the path.
    """
    if raw_bytes is None:
        with open(filepath, "rb") as f:
            raw_bytes = f.read()
    filename = os.path.basename(filepath)
    url = (f"{_homeserver()}/_matrix/media/v3/upload"
           f"?{urllib.parse.urlencode({'filename': filename})}")
    req = urllib.request.Request(url, data=raw_bytes, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", mimetype)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise MatrixError(e.code, detail) from None
    except urllib.error.URLError as e:
        raise ConnectionError(f"Cannot reach homeserver {_homeserver()}: {e.reason}") from None
    return data.get("content_uri", "")


def _voice_provider():
    provider = VOICE_PROVIDER
    if provider not in VOICE_PROVIDERS:
        choices = ", ".join(sorted(VOICE_PROVIDERS))
        raise VoiceMemoError(
            f"Unsupported WhatsApp voice provider {provider!r}; choose {choices}."
        )
    if SPEECH_MODEL != APPROVED_SPEECH_MODEL:
        raise VoiceMemoError(
            "WhatsApp speech is locked to ElevenLabs model "
            f"{APPROVED_SPEECH_MODEL!r}; got {SPEECH_MODEL!r}."
        )
    return provider


def _eleven_credentials():
    """Resolve the ElevenLabs key and the owner's professional-clone voice ID.

    The API key is a secret: it comes from the environment or the voice env file, never
    from the kit config. The voice id comes from the variable named by
    `whatsapp.voice_id_var` (default WHATSAPP_VOICE_ID) in the environment, then
    `whatsapp.voice_id`, then that variable in the voice env file."""
    id_var = str(kitconfig.get("whatsapp.voice_id_var", "") or "WHATSAPP_VOICE_ID")
    values = {
        "ELEVENLABS_API_KEY": os.environ.get("ELEVENLABS_API_KEY", "").strip(),
        id_var: (os.environ.get(id_var, "").strip()
                 or str(kitconfig.get("whatsapp.voice_id", "")).strip()),
    }
    if not all(values.values()) and VOICE_ENV_FILE and os.path.isfile(VOICE_ENV_FILE):
        try:
            with open(VOICE_ENV_FILE, encoding="utf-8") as env_file:
                for raw_line in env_file:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    if key in values and not values[key]:
                        values[key] = value.strip().strip('"').strip("'")
        except OSError as e:
            raise VoiceMemoError(
                f"Could not read ElevenLabs voice credentials: {e}"
            ) from None
    missing = [key for key, value in values.items() if not value]
    if missing:
        where = (f"the voice env file {VOICE_ENV_FILE}" if VOICE_ENV_FILE
                 else "`whatsapp.voice_env_file` in the kit config")
        raise VoiceConfigError(
            "ElevenLabs voice settings are missing: "
            + ", ".join(missing)
            + f". Set them in {where} or the environment (voice id: also `whatsapp.voice_id`)."
        )
    voice_id = values[id_var]
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", voice_id):
        raise VoiceConfigError(f"Invalid ElevenLabs voice ID: {voice_id!r}")
    return values["ELEVENLABS_API_KEY"], voice_id


def _eleven_request(path, api_key, *, body=None, accept="application/json", timeout=180):
    """Call the ElevenLabs API with the stdlib and no retry-on-content."""
    request = urllib.request.Request(
        f"{ELEVEN_API_ROOT}{path}",
        data=body,
        headers={
            "xi-api-key": api_key,
            "Accept": accept,
            **({"Content-Type": "application/json"} if body else {}),
        },
        method="POST" if body else "GET",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=ssl.create_default_context()
        ) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()
        raise VoiceMemoError(
            f"ElevenLabs request failed with HTTP {e.code}: {detail[:300]}"
        ) from None
    except urllib.error.URLError as e:
        raise VoiceMemoError(
            f"Could not reach the ElevenLabs voice service: {e.reason}"
        ) from None


def _eleven_voice_identity(api_key, voice_id):
    """Confirm the configured voice really is the owner's professional clone.

    An older instant clone can sit under the same variable name in another env file.
    Both generate perfectly valid audio, so a wrong one is only
    audible, never an error. This read-only lookup turns that into a hard stop.
    """
    payload = _eleven_request(
        f"/v1/voices/{urllib.parse.quote(voice_id)}", api_key, timeout=60
    )
    try:
        voice = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise VoiceMemoError(f"Could not read the ElevenLabs voice record: {e}") from None
    name = str(voice.get("name") or "").strip()
    category = str(voice.get("category") or "").strip()
    if category != APPROVED_VOICE_CATEGORY:
        raise VoiceMemoError(
            f"WhatsApp voice memos require the owner's {APPROVED_VOICE_CATEGORY} voice "
            f"clone; voice {voice_id} is {category or 'unknown'!r} ({name or 'unnamed'}). "
            f"Point the voice id in {VOICE_ENV_FILE or 'the voice env file'} at the professional clone."
        )
    return {"voice_id": voice_id, "voice_name": name, "voice_category": category}


def _generate_eleven_voice_wav(message, out_dir, *, model, settings, label):
    """Generate one 48 kHz mono PCM WAV with the owner's professional clone."""
    api_key, voice_id = _eleven_credentials()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise VoiceMemoError(f"Invalid ElevenLabs model: {model!r}")

    payload = {
        "text": tts_speakable(message),  # respelled for the voice only; the chat text is unchanged
        "model_id": model,
        "voice_settings": dict(settings),
    }
    audio = _eleven_request(
        f"/v1/text-to-speech/{urllib.parse.quote(voice_id)}",
        api_key,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        accept="audio/mpeg",
    )
    if not audio:
        raise VoiceMemoError("ElevenLabs returned an empty audio response.")

    raw_path = os.path.join(out_dir, f"{label}-response.mp3")
    wav_path = os.path.join(out_dir, f"{label}-voice-message.wav")
    try:
        with open(raw_path, "wb") as output:
            output.write(audio)
        os.chmod(raw_path, 0o600)
    except OSError as e:
        raise VoiceMemoError(f"Could not cache the ElevenLabs response: {e}") from None

    try:
        converted = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-v", "error", "-i", raw_path,
                "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", wav_path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        raise VoiceMemoError(
            "ffmpeg is required to normalize ElevenLabs voice audio."
        ) from None
    except subprocess.TimeoutExpired:
        raise VoiceMemoError("ElevenLabs voice audio normalization timed out.") from None
    if converted.returncode != 0:
        detail = (converted.stderr or "unknown ffmpeg error").strip().splitlines()[-1]
        raise VoiceMemoError(f"ElevenLabs voice audio normalization failed: {detail}")
    if not os.path.isfile(wav_path) or os.path.getsize(wav_path) == 0:
        raise VoiceMemoError("ElevenLabs voice normalization produced no WAV file.")
    os.chmod(wav_path, 0o600)
    return wav_path


def _generate_speech_wav(message, out_dir):
    """Generate ordinary speech with the owner's approved professional clone."""
    _voice_provider()
    return _generate_eleven_voice_wav(
        message,
        out_dir,
        model=SPEECH_MODEL,
        settings=APPROVED_SPEECH_SETTINGS,
        label="speech",
    )


def _generate_reaction_wav(message, out_dir):
    """Generate one isolated nonverbal reaction on the same clone, via v3."""
    return _generate_eleven_voice_wav(
        message,
        out_dir,
        model=VOICE_EXPRESSIVE_MODEL,
        settings=APPROVED_REACTION_SETTINGS,
        label="reaction",
    )




def _reaction_prompt(tag):
    reaction = tag.strip("[]").lower()
    if reaction.startswith("giggl"):
        return "[giggling] Heh heh!"
    return "[chuckles] Heh heh."


def _hybrid_voice_segments(message):
    """Return ordered multilingual-v2 speech and Eleven v3 reaction segments."""
    audio_tags = VOICE_AUDIO_TAG_PATTERN.findall(message)
    unsupported = [
        tag for tag in audio_tags
        if not VOICE_REACTION_TAG_PATTERN.fullmatch(tag)
    ]
    if unsupported:
        raise VoiceMemoError(
            "Hybrid expressive mode supports only [laughs], [giggles], "
            "and [chuckles] reaction tags."
        )

    segments = []
    cursor = 0
    for match in VOICE_REACTION_TAG_PATTERN.finditer(message):
        speech = message[cursor:match.start()].strip()
        if speech:
            segments.append(("speech", speech))
        segments.append(("reaction", match.group(0)))
        cursor = match.end()
    speech = message[cursor:].strip()
    if speech:
        segments.append(("speech", speech))
    if not segments:
        raise VoiceMemoError("Expressive voice memo has no speakable content.")
    return segments


def _concat_pcm_voice_wavs(wav_paths, output_path):
    """Concatenate normalized PCM WAVs with a short natural reaction gap."""
    expected = (1, 2, 48_000, "NONE")
    gap_frames = round(48_000 * VOICE_REACTION_GAP_MS / 1000)
    try:
        with wave.open(output_path, "wb") as target:
            target.setnchannels(expected[0])
            target.setsampwidth(expected[1])
            target.setframerate(expected[2])
            target.setcomptype(expected[3], "not compressed")
            for index, wav_path in enumerate(wav_paths):
                with wave.open(wav_path, "rb") as source:
                    actual = (
                        source.getnchannels(),
                        source.getsampwidth(),
                        source.getframerate(),
                        source.getcomptype(),
                    )
                    if actual != expected:
                        raise VoiceMemoError(
                            f"Hybrid voice segment has unexpected WAV format: {actual}"
                        )
                    while True:
                        frames = source.readframes(48_000)
                        if not frames:
                            break
                        target.writeframes(frames)
                if index < len(wav_paths) - 1:
                    target.writeframes(b"\x00" * gap_frames * expected[1])
    except (EOFError, wave.Error) as e:
        raise VoiceMemoError(f"Could not assemble hybrid voice memo: {e}") from None
    return output_path


def _generate_hybrid_voice_wav(message, out_dir):
    """Generate professional-clone speech with v3-only nonverbal reactions.

    Both halves now come from the SAME voice — only the model changes, because
    v3 is the one that performs audio tags. A reaction rendered by a different
    clone is audibly a different person mid-memo.
    """
    wav_paths = []
    for index, (kind, value) in enumerate(_hybrid_voice_segments(message), 1):
        segment_dir = os.path.join(out_dir, f"segment-{index:02d}")
        os.mkdir(segment_dir, 0o700)
        if kind == "reaction":
            wav_path = _generate_reaction_wav(_reaction_prompt(value), segment_dir)
        else:
            wav_path = _generate_speech_wav(value, segment_dir)
        wav_paths.append(wav_path)
    return _concat_pcm_voice_wavs(
        wav_paths, os.path.join(out_dir, "hybrid-voice-message.wav")
    )


def _wav_voice_metadata(wav_path):
    """Return (duration_ms, 64-sample Matrix waveform) from a PCM WAV."""
    try:
        with wave.open(wav_path, "rb") as audio:
            if audio.getsampwidth() != 2 or audio.getcomptype() != "NONE":
                raise VoiceMemoError(
                    "Voice generator must produce uncompressed 16-bit PCM WAV."
                )
            frame_count = audio.getnframes()
            frame_rate = audio.getframerate()
            frames_per_bin = max(
                1, (frame_count + VOICE_WAVEFORM_SAMPLES - 1) // VOICE_WAVEFORM_SAMPLES
            )
            levels = []
            for _ in range(VOICE_WAVEFORM_SAMPLES):
                raw = audio.readframes(frames_per_bin)
                if not raw:
                    break
                samples = array.array("h")
                samples.frombytes(raw)
                if sys.byteorder != "little":
                    samples.byteswap()
                if samples:
                    mean_square = sum(sample * sample for sample in samples) / len(samples)
                    levels.append(mean_square ** 0.5)
    except (EOFError, wave.Error) as e:
        raise VoiceMemoError(f"Could not read generated WAV: {e}") from None

    if not frame_count or not frame_rate:
        raise VoiceMemoError("Generated voice memo is empty.")
    duration_ms = max(1, round(frame_count * 1000 / frame_rate))
    peak = max(levels, default=0)
    if peak:
        waveform = [min(1024, round(level * 1024 / peak)) for level in levels]
    else:
        waveform = [0] * max(1, len(levels))
    return duration_ms, waveform


def _encode_voice_note(wav_path, ogg_path, duration_ms=None):
    """Mix faint fan/room tone, then encode mono Opus-in-Ogg for WhatsApp."""
    background = os.path.abspath(os.path.expanduser(VOICE_BACKGROUND_PATH))
    if not os.path.isfile(background):
        raise VoiceMemoError(f"Voice background ambience not found: {background}")
    if duration_ms is None:
        duration_ms, _ = _wav_voice_metadata(wav_path)
    duration_s = duration_ms / 1000
    fade_in = min(0.15, duration_s / 3)
    fade_out = min(0.25, duration_s / 3)
    fade_out_start = max(fade_in, duration_s - fade_out)
    filter_graph = (
        "[0:a]aresample=48000,"
        "aformat=sample_fmts=fltp:channel_layouts=mono[voice];"
        f"[1:a]atrim=duration={duration_s:.3f},asetpts=PTS-STARTPTS,"
        f"loudnorm=I={VOICE_BACKGROUND_LUFS}:TP=-9:LRA=7,aresample=48000,"
        "aformat=sample_fmts=fltp:channel_layouts=mono,"
        f"afade=t=in:st=0:d={fade_in:.3f},"
        f"afade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}[fan];"
        "[voice][fan]amix=inputs=2:normalize=0:duration=first[mixed]"
    )
    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        wav_path,
        "-stream_loop",
        "-1",
        "-i",
        background,
        "-filter_complex",
        filter_graph,
        "-map",
        "[mixed]",
        "-map_metadata",
        "-1",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "libopus",
        "-b:a",
        "48k",
        "-vbr",
        "on",
        "-compression_level",
        "10",
        "-application",
        "voip",
        ogg_path,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=300
        )
    except FileNotFoundError:
        raise VoiceMemoError("ffmpeg is required to encode WhatsApp voice memos.") from None
    except subprocess.TimeoutExpired:
        raise VoiceMemoError("Voice memo ambience/Opus encoding timed out.") from None

    if result.returncode != 0:
        details = (result.stderr or "unknown error").strip()
        tail = details.splitlines()[-1] if details else "unknown error"
        raise VoiceMemoError(f"Voice memo ambience/Opus encoding failed: {tail}")
    if not os.path.isfile(ogg_path) or os.path.getsize(ogg_path) == 0:
        raise VoiceMemoError("ffmpeg completed without producing an Ogg file.")

def _ogg_voice_duration_ms(ogg_path):
    """Return the encoded Ogg duration, rounded up to the nearest millisecond."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                ogg_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise VoiceMemoError(
            "ffprobe is required to validate WhatsApp voice memo duration."
        ) from None
    except subprocess.TimeoutExpired:
        raise VoiceMemoError("Voice memo duration validation timed out.") from None
    if result.returncode != 0:
        detail = (result.stderr or "unknown ffprobe error").strip()
        raise VoiceMemoError(f"Could not read encoded voice duration: {detail}")
    try:
        duration_ms = max(1, int(float(result.stdout.strip()) * 1000 + 0.999))
    except (TypeError, ValueError):
        raise VoiceMemoError("Encoded voice memo has an invalid duration.") from None
    return duration_ms



def _split_voice_text_once(text):
    """Split text near its semantic midpoint, falling back to a word boundary."""
    normalized = " ".join(text.split())
    if not normalized:
        raise VoiceMemoError("Cannot split empty voice memo text.")

    midpoint = len(normalized) // 2
    candidates = [
        match.end()
        for match in re.finditer(r"(?<=[.!?;,:])\s+", normalized)
    ]
    if not candidates:
        candidates = [
            match.start()
            for match in re.finditer(r"\s+", normalized)
        ]
    if not candidates:
        raise VoiceMemoError(
            "A single unsplittable word generated a voice memo over one minute."
        )

    split_at = min(candidates, key=lambda position: abs(position - midpoint))
    left = normalized[:split_at].strip()
    right = normalized[split_at:].strip()
    if not left or not right:
        raise VoiceMemoError("Could not split an overlong voice memo safely.")
    return left, right


def _pre_split_voice_text(message):
    """Bound initial TTS requests while preserving sentence/clause boundaries."""
    pending = [" ".join(message.split())]
    parts = []
    while pending:
        text = pending.pop()
        if len(text.split()) <= VOICE_PRE_SPLIT_WORDS:
            parts.append(text)
            continue
        left, right = _split_voice_text_once(text)
        pending.extend((right, left))
    return parts


def _sha256_file(filepath):
    digest = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _voice_part_transaction_id(preview_id, index):
    """Return the stable Matrix transaction ID for one cached voice part."""
    return f"wamxv{preview_id}p{index}"


def _verified_voice_target(token, chat):
    """Return a voice target backed by the portal's authoritative bridge JID."""
    portal_id = chat["portal_id"]
    channel_jid = _portal_channel_jid(token, portal_id)
    is_group = _classify_channel_jid(channel_jid)
    if is_group is None:
        raise VoiceMemoError(
            "Cannot verify whether the WhatsApp voice target is a DM or group; "
            f"portal {portal_id} has no authoritative bridge JID."
        )
    if bool(chat["is_group"]) != is_group:
        raise VoiceMemoError(
            "WhatsApp voice target classification changed during resolution."
        )
    return {
        "portal_id": portal_id,
        "label": chat["label"],
        "is_group": is_group,
        "channel_jid": channel_jid,
    }


def _voice_approval_digest(manifest):
    """Hash immutable target, provider, script, and part approval metadata."""
    part_keys = (
        "index",
        "filename",
        "text",
        "duration_ms",
        "waveform",
        "size",
        "sha256",
    )
    payload = {
        "version": manifest.get("version"),
        "approval_nonce": manifest.get("approval_nonce"),
        "created_at": manifest.get("created_at"),
        "recipient_input": manifest.get("recipient_input"),
        "message": manifest.get("message"),
        "voice_provider": manifest.get("voice_provider"),
        "voice_model": manifest.get("voice_model"),
        "voice_id": manifest.get("voice_id"),
        "voice_name": manifest.get("voice_name"),
        "voice_category": manifest.get("voice_category"),
        "speech_provider": manifest.get("speech_provider"),
        "speech_model": manifest.get("speech_model"),
        "speech_settings": manifest.get("speech_settings"),
        "reaction_provider": manifest.get("reaction_provider"),
        "reaction_model": manifest.get("reaction_model"),
        "reaction_settings": manifest.get("reaction_settings"),
        "background_lufs": manifest.get("background_lufs"),
        "target": manifest.get("target"),
        "parts": [
            {key: part.get(key) for key in part_keys}
            for part in manifest.get("parts", [])
        ],
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()



def _prepare_voice_piece(text, preview_dir, model=None):
    """Generate one or more sub-minute cached Ogg parts for a text segment."""
    with tempfile.TemporaryDirectory(
        prefix=".voice-build-", dir=preview_dir
    ) as build_dir:
        if model == VOICE_HYBRID_MODEL:
            wav_path = _generate_hybrid_voice_wav(text, build_dir)
        else:
            wav_path = _generate_speech_wav(text, build_dir)
        wav_duration_ms, waveform = _wav_voice_metadata(wav_path)
        if wav_duration_ms > VOICE_NOTE_TARGET_MS:
            left, right = _split_voice_text_once(text)
            return (
                _prepare_voice_piece(left, preview_dir, model=model)
                + _prepare_voice_piece(right, preview_dir, model=model)
            )

        ogg_path = os.path.join(
            preview_dir, f".part-{uuid.uuid4().hex}.ogg"
        )
        _encode_voice_note(wav_path, ogg_path, duration_ms=wav_duration_ms)
        duration_ms = _ogg_voice_duration_ms(ogg_path)
        if duration_ms >= VOICE_NOTE_LIMIT_MS:
            os.unlink(ogg_path)
            left, right = _split_voice_text_once(text)
            return (
                _prepare_voice_piece(left, preview_dir, model=model)
                + _prepare_voice_piece(right, preview_dir, model=model)
            )
        os.chmod(ogg_path, 0o600)

    return [{
        "text": text,
        "path": ogg_path,
        "duration_ms": duration_ms,
        "waveform": waveform,
    }]


def _ensure_voice_preview_root():
    os.makedirs(VOICE_PREVIEW_ROOT, mode=0o700, exist_ok=True)
    os.chmod(VOICE_PREVIEW_ROOT, 0o700)
    return VOICE_PREVIEW_ROOT


def _voice_preview_dir(preview_id):
    if not re.fullmatch(r"[0-9a-f]{32}", preview_id or ""):
        raise VoiceMemoError("Invalid voice preview ID.")
    return os.path.join(VOICE_PREVIEW_ROOT, preview_id)


def _write_voice_preview_manifest(manifest):
    preview_dir = _voice_preview_dir(manifest.get("preview_id", ""))
    os.makedirs(preview_dir, mode=0o700, exist_ok=True)
    os.chmod(preview_dir, 0o700)
    manifest_path = os.path.join(preview_dir, "manifest.json")
    temp_path = os.path.join(preview_dir, f".manifest-{uuid.uuid4().hex}.tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, manifest_path)
    return manifest_path


def _load_voice_preview_manifest(preview_id):
    preview_dir = _voice_preview_dir(preview_id)
    manifest_path = os.path.join(preview_dir, "manifest.json")
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except FileNotFoundError:
        raise VoiceMemoError(f"Voice preview not found: {preview_id}") from None
    except (OSError, json.JSONDecodeError) as e:
        raise VoiceMemoError(f"Could not read voice preview {preview_id}: {e}") from None

    target = manifest.get("target")
    parts = manifest.get("parts")
    if (
        manifest.get("version") != VOICE_PREVIEW_VERSION
        or manifest.get("preview_id") != preview_id
        or not isinstance(manifest.get("approval_nonce"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", manifest["approval_nonce"])
        or not isinstance(manifest.get("recipient_input"), str)
        or not manifest["recipient_input"]
        or not isinstance(manifest.get("message"), str)
        or not manifest["message"]
        or not isinstance(target, dict)
        or not isinstance(target.get("portal_id"), str)
        or not target["portal_id"].startswith("!")
        or not isinstance(target.get("label"), str)
        or not target["label"]
        or not isinstance(target.get("is_group"), bool)
        or not isinstance(target.get("channel_jid"), str)
        or not isinstance(parts, list)
        or not parts
    ):
        raise VoiceMemoError(f"Voice preview manifest is invalid: {preview_id}")

    channel_jid = target["channel_jid"]
    classified_group = _classify_channel_jid(channel_jid)
    reaction_provider = manifest.get("reaction_provider")
    reaction_model = manifest.get("reaction_model")
    expressive = reaction_provider is not None or reaction_model is not None
    expected_voice_model = (
        VOICE_HYBRID_MODEL if expressive else APPROVED_SPEECH_MODEL
    )
    voice_id = manifest.get("voice_id")
    if (
        classified_group is None
        or target["is_group"] != classified_group
        or manifest.get("voice_provider") != APPROVED_VOICE_PROVIDER
        or manifest.get("voice_model") != expected_voice_model
        or not isinstance(voice_id, str)
        or not re.fullmatch(r"[A-Za-z0-9]{16,64}", voice_id)
        or manifest.get("voice_category") != APPROVED_VOICE_CATEGORY
        or manifest.get("speech_provider") != APPROVED_VOICE_PROVIDER
        or manifest.get("speech_model") != APPROVED_SPEECH_MODEL
        or manifest.get("speech_settings") != APPROVED_SPEECH_SETTINGS
        or (
            expressive
            and (
                reaction_provider != VOICE_REACTION_PROVIDER
                or reaction_model != VOICE_EXPRESSIVE_MODEL
                or manifest.get("reaction_settings") != APPROVED_REACTION_SETTINGS
            )
        )
        or (
            not expressive
            and (
                reaction_provider is not None
                or reaction_model is not None
                or manifest.get("reaction_settings") is not None
            )
        )
    ):
        raise VoiceMemoError(
            f"Voice preview provider or target route is invalid: {preview_id}"
        )

    approval_digest = manifest.get("approval_sha256")
    computed_digest = _voice_approval_digest(manifest)
    if (
        not isinstance(approval_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", approval_digest)
        or approval_digest != computed_digest
        or preview_id != computed_digest[:32]
    ):
        raise VoiceMemoError(f"Voice preview approval metadata changed: {preview_id}")
    return manifest


def _voice_preview_part_path(preview_id, part):
    filename = part.get("filename", "")
    if not re.fullmatch(r"part-\d{2,}\.ogg", filename):
        raise VoiceMemoError("Voice preview contains an invalid part filename.")
    return os.path.join(_voice_preview_dir(preview_id), filename)


def _validate_voice_preview_parts(manifest):
    """Read, verify, and retain every pending cached byte for exact upload."""
    validated = []
    preview_id = manifest["preview_id"]
    for part in manifest["parts"]:
        if part.get("event_id"):
            continue
        filepath = _voice_preview_part_path(preview_id, part)
        try:
            with open(filepath, "rb") as audio_file:
                audio_bytes = audio_file.read()
        except OSError as e:
            raise VoiceMemoError(
                f"Could not read voice preview audio {filepath}: {e}"
            ) from None
        duration_ms = part.get("duration_ms")
        size = part.get("size")
        waveform = part.get("waveform")
        transaction_id = part.get("transaction_id")
        if (
            not isinstance(duration_ms, int)
            or isinstance(duration_ms, bool)
            or duration_ms <= 0
            or duration_ms >= VOICE_NOTE_LIMIT_MS
        ):
            raise VoiceMemoError(f"Voice preview part exceeds one minute: {filepath}")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or len(audio_bytes) != size
        ):
            raise VoiceMemoError(f"Voice preview size changed: {filepath}")
        if (
            not isinstance(waveform, list)
            or not 1 <= len(waveform) <= VOICE_WAVEFORM_SAMPLES
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 1024
                for value in waveform
            )
        ):
            raise VoiceMemoError(f"Voice preview waveform is invalid: {filepath}")
        expected_transaction_id = _voice_part_transaction_id(
            preview_id, part.get("index")
        )
        if transaction_id != expected_transaction_id:
            raise VoiceMemoError(
                f"Voice preview transaction ID changed: {filepath}"
            )
        expected_hash = part.get("sha256")
        if (
            not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or hashlib.sha256(audio_bytes).hexdigest() != expected_hash
        ):
            raise VoiceMemoError(f"Voice preview audio changed: {filepath}")
        validated.append((part, filepath, audio_bytes))
    return validated


def _voice_event_content(part):
    return {
        "msgtype": "m.audio",
        "body": "",
        "filename": "Voice message.ogg",
        "info": {
            "mimetype": VOICE_NOTE_MIMETYPE,
            "size": part["size"],
            "duration": part["duration_ms"],
        },
        "org.matrix.msc1767.audio": {
            "duration": part["duration_ms"],
            "waveform": part["waveform"],
        },
        "org.matrix.msc3245.voice": {},
    }


def _voice_preview_target(manifest):
    target = manifest["target"]
    if target.get("is_group"):
        return f'GROUP "{target["label"]}"'
    return target["label"]


def _print_voice_preview(manifest):
    parts = manifest["parts"]
    print(
        f'Voice preview {manifest["preview_id"]} for '
        f'{_voice_preview_target(manifest)} [room {manifest["target"]["portal_id"]}]'
    )
    print(f"Status: {manifest.get('status', 'prepared')}")
    print(
        f"Voice provider: {manifest['voice_provider']}; "
        f"model: {manifest['voice_model']}; "
        f"fan/room tone: {manifest.get('background_lufs', VOICE_BACKGROUND_LUFS)} LUFS"
    )
    print(
        f"Voice: {manifest.get('voice_name') or 'unnamed'} "
        f"({manifest.get('voice_category')} clone, ...{str(manifest.get('voice_id'))[-4:]})"
    )
    settings = manifest.get("speech_settings") or {}
    print(
        f"Speech component: {manifest['speech_provider']}/"
        f"{manifest['speech_model']}"
        + (
            f" [stability {settings['stability']}, similarity "
            f"{settings['similarity_boost']}, style {settings['style']}]"
            if settings
            else ""
        )
    )
    if manifest.get("reaction_provider"):
        print(
            f"Reaction component: {manifest['reaction_provider']}/"
            f"{manifest['reaction_model']} (same voice)"
        )
    for part in parts:
        filepath = _voice_preview_part_path(manifest["preview_id"], part)
        print(
            f'\nPart {part["index"]}/{len(parts)} '
            f'({part["duration_ms"] / 1000:.1f}s):'
        )
        print(f"  Text: {part['text']}")
        print(f"  Audio: {filepath}")


def _prepare_voice_preview(identifier, message, chat, model=None):
    """Generate exact files, then derive their content-bound approval ID."""
    provider = _voice_provider()
    # Resolve and verify the clone BEFORE generating: a wrong-but-valid voice ID
    # would otherwise cost credits and only be caught by ear.
    api_key, voice_id = _eleven_credentials()
    identity = _eleven_voice_identity(api_key, voice_id)
    root = _ensure_voice_preview_root()
    staging_dir = tempfile.mkdtemp(prefix=".voice-preview-", dir=root)
    os.chmod(staging_dir, 0o700)
    final_dir = None

    try:
        prepared = []
        for text in _pre_split_voice_text(message):
            prepared.extend(_prepare_voice_piece(text, staging_dir, model=model))

        parts = []
        for index, part in enumerate(prepared, 1):
            filename = f"part-{index:02d}.ogg"
            filepath = os.path.join(staging_dir, filename)
            os.replace(part["path"], filepath)
            os.chmod(filepath, 0o600)
            parts.append({
                "index": index,
                "filename": filename,
                "text": part["text"],
                "duration_ms": part["duration_ms"],
                "waveform": part["waveform"],
                "size": os.path.getsize(filepath),
                "sha256": _sha256_file(filepath),
                "event_id": None,
            })

        expressive = model == VOICE_HYBRID_MODEL
        manifest = {
            "version": VOICE_PREVIEW_VERSION,
            "approval_nonce": uuid.uuid4().hex,
            "created_at": datetime.now().astimezone().isoformat(
                timespec="microseconds"
            ),
            "status": "prepared",
            "recipient_input": identifier,
            "message": message,
            "voice_provider": provider,
            "voice_model": model or APPROVED_SPEECH_MODEL,
            "voice_id": identity["voice_id"],
            "voice_name": identity["voice_name"],
            "voice_category": identity["voice_category"],
            "speech_provider": APPROVED_VOICE_PROVIDER,
            "speech_model": APPROVED_SPEECH_MODEL,
            "speech_settings": dict(APPROVED_SPEECH_SETTINGS),
            "reaction_provider": VOICE_REACTION_PROVIDER if expressive else None,
            "reaction_model": VOICE_EXPRESSIVE_MODEL if expressive else None,
            "reaction_settings": (
                dict(APPROVED_REACTION_SETTINGS) if expressive else None
            ),
            "background_lufs": VOICE_BACKGROUND_LUFS,
            "target": {
                "portal_id": chat["portal_id"],
                "label": chat["label"],
                "is_group": bool(chat["is_group"]),
                "channel_jid": chat["channel_jid"],
            },
            "parts": parts,
        }
        approval_digest = _voice_approval_digest(manifest)
        preview_id = approval_digest[:32]
        manifest["preview_id"] = preview_id
        manifest["approval_sha256"] = approval_digest
        for part in parts:
            part["transaction_id"] = _voice_part_transaction_id(
                preview_id, part["index"]
            )

        final_dir = _voice_preview_dir(preview_id)
        os.replace(staging_dir, final_dir)
        staging_dir = None
        _write_voice_preview_manifest(manifest)
        return manifest
    except Exception:
        if staging_dir:
            shutil.rmtree(staging_dir, ignore_errors=True)
        if final_dir:
            shutil.rmtree(final_dir, ignore_errors=True)
        raise


def _upload_and_send_media(
    token,
    portal_id,
    filepath,
    mimetype,
    content,
    *,
    raw_bytes=None,
    transaction_id=None,
):
    """Upload media and PUT its event into a confirmed target portal.

    *** CONTENT PORTAL WRITE SITE #2 of 2 (the other is `cmd_send`) ***
    Callers MUST resolve one target, preview it, and return unless `--yes` before
    reaching this helper. Voice retries supply immutable bytes and a stable
    transaction ID; ordinary media sends keep the one-shot defaults.
    """
    if transaction_id is not None and not re.fullmatch(
        r"[A-Za-z0-9._~-]{1,128}", transaction_id
    ):
        raise VoiceMemoError("Invalid Matrix transaction ID.")
    mxc = _upload_media(
        token, filepath, mimetype, raw_bytes=raw_bytes
    )
    if not mxc:
        return None

    event_content = dict(content)
    event_content["url"] = mxc
    txn = transaction_id or f"wamx{int(time.time() * 1000)}"
    return _request(
        "PUT",
        f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/send/m.room.message/{txn}",
        token=token,
        body=event_content,
    )


def _whisper_transcribe(audio_bytes, filename, language=None):
    """POST audio to the local whisper service /v1/transcribe (multipart). READ-ONLY.

    Builds a multipart/form-data body by hand (stdlib only) with a single
    `file` part. Returns the parsed JSON {"text","language","duration","elapsed"}.
    The whisper service is box-local and is NOT a portal — this never relays to a
    WhatsApp contact.
    """
    boundary = f"----wamx{uuid.uuid4().hex}"
    mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    crlf = b"\r\n"
    parts = [
        b"--" + boundary.encode() + crlf,
        (f'Content-Disposition: form-data; name="file"; filename="{filename}"'
         ).encode() + crlf,
        f"Content-Type: {mimetype}".encode() + crlf,
        crlf,
        audio_bytes,
        crlf,
        b"--" + boundary.encode() + b"--" + crlf,
    ]
    body = b"".join(parts)

    url = f"{WHISPER_URL}/v1/transcribe"
    if language:
        url = f"{url}?{urllib.parse.urlencode({'language': language})}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise MatrixError(e.code, detail) from None
    except urllib.error.URLError as e:
        raise ConnectionError(f"Cannot reach whisper service {WHISPER_URL}: {e.reason}") from None


def _voice_note_suffix(token, event, transcribe=True):
    """Build the inline `[voice...]` suffix for an m.audio event.

    READ-ONLY. With `transcribe`, downloads + transcribes via the local whisper
    service and returns `[voice: <transcript>]`. If whisper is unreachable or
    errors, returns a `[voice note — transcription unavailable]` placeholder
    rather than raising — a voice note must never crash the caller. With
    `transcribe=False` (opt-out, faster), returns a bare `[voice note]` and makes
    no whisper call. Returns '' for events that aren't a usable voice note.
    """
    content = event.get("content", {})
    if content.get("msgtype") != "m.audio" or not content.get("url"):
        return ""
    if not transcribe:
        return "  [voice note]"
    try:
        text = _transcribe_event(token, event)
    except (MatrixError, ConnectionError):
        return "  [voice note — transcription unavailable]"
    return f"  [voice: {text}]" if text else "  [voice note]"


def _transcribe_event(token, event, language=None):
    """Download a media event's audio and return its transcript text (or '').

    READ-ONLY: GETs the media, POSTs to the local whisper service, cleans up the
    temp file. Returns the transcript string, or '' if no usable audio/text.
    """
    content = event.get("content", {})
    mxc = content.get("url")
    if not mxc:
        return ""
    audio = _download_media(token, mxc)
    info = content.get("info", {}) or {}
    ext = _ext_for_mimetype(info.get("mimetype", ""), fallback=".ogg")
    fd, tmp_path = tempfile.mkstemp(prefix="wamx_voice_", suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        result = _whisper_transcribe(audio, os.path.basename(tmp_path), language=language)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return result.get("text", "").strip()


# --- P0 (2026-10-30): full reads, outbound gate, CLI logging ----------------------
# The default text output of every read command is unchanged byte for byte (`comms` and the
# KB exporter parse it). `--full` is the lossless read: one msgschema/1 JSON object, counted in
# real messages (not events), edits/redactions folded, reactions attached, reply parents filled,
# media listed. A full read that has to stop early says so (`truncated`, `more`).

FULL_PAGE = 100            # events per /messages page
FULL_MAX_PAGES = 20        # safety cap -> truncated + more
FULL_MAX_PARENTS = 20      # reply/edit parents fetched outside the window
UNREAD_MAX_PER_ROOM = 50
GATE_DENY_EXIT = 7         # outbound gate refused in enforce mode; nothing was sent


def _comms_module(name):
    """core/<name>.py, found with the core search order and loaded by exact path
    (None when it cannot load)."""
    return _load_core(name)


def _gate_mode_fallback():
    """The gate mode when outbound_gate.py itself cannot load: env, then the mode file.
    Unknown or unreadable means enforce, same rule as the gate."""
    value = os.environ.get("GATE_MODE")
    if value is None:
        try:
            with open(os.path.join(_core_dir("outbound_gate"), "gate_mode")) as fh:
                value = fh.read()
        except FileNotFoundError:
            value = "report"
        except OSError:
            value = "enforce"
    value = value.strip().lower()
    return value if value in ("report", "enforce") else "enforce"


def _gate_recipients(token, portal_id, chat, identifier):
    """The people a send reaches, as the developer guard knows them: phone numbers from the
    portal's WhatsApp puppets (DM or group). [] when unreadable; never trust a typed
    number as proof of the resolved room's audience."""
    out = set()
    try:
        for _mxid, localpart, _name in _ghost_members(token, portal_id):
            num = str(localpart or "")
            if num.isdigit():
                out.update({num, "+" + num})
            else:
                return []  # LID-to-person mapping unavailable: never assume a phone identity.
    except Exception:  # noqa: BLE001 - unresolved recipients are reported by the gate
        pass
    return sorted(out)


def _gate_check(args, *, target, texts, recipients, filenames=(), attachment_texts=(),
                unread=(), hashes=(), dry_run, source):
    """Evaluate the outbound gate on the FINAL text. Exits GATE_DENY_EXIT in enforce mode when
    it refuses. Returns a context for _gate_begin, or None when the gate could not load in
    report mode."""
    G = _comms_module("outbound_gate")
    if G is None:
        if _gate_mode_fallback() == "enforce":
            print("NOT SENT: outbound gate (enforce): the gate module could not load.")
            sys.exit(GATE_DENY_EXIT)
        sys.stderr.write("outbound gate: module could not load; report mode, continuing\n")
        return None
    approvals = G.Approvals.from_env(
        money_ok=bool(getattr(args, "money_ok", False)),
        override_reason=(getattr(args, "override_check", None) or ""),
        resend_ok=bool(getattr(args, "resend_ok", False)))
    payload = G.OutboundPayload(
        "whatsapp", target=target, texts=list(texts), recipients=list(recipients),
        filenames=list(filenames), attachment_texts=list(attachment_texts),
        unread_attachments=list(unread), attachment_hashes=[h for h in hashes if h])
    verdict = G.evaluate(payload, approvals, dry_run=dry_run, source=source)
    if not verdict.allow:
        print(verdict.deny_line())
        sys.exit(GATE_DENY_EXIT)
    return (G, payload, approvals)


def _gate_begin(ctx):
    """Reserve the send right before the PUT (never on a dry run)."""
    if ctx is None:
        return None
    G, payload, approvals = ctx
    try:
        return G.begin(payload, approvals)
    except G.GateDenied as e:
        print(str(e))
        sys.exit(GATE_DENY_EXIT)


def _gate_finish(ctx, op, status, receipts=()):
    if ctx is not None and op:
        ctx[0].finish(op, status, [r for r in receipts if r])


def _log_send(identifier, text, event_id, verb, files=None):
    """Archive this send from the CLI itself (plain shell, launchd, pi-2...). Never raises."""
    C = _comms_module("commslib")
    if C is None or not event_id:
        return
    C.log_event("whatsapp", "send", identifier, text=text, message_id=event_id, verb=verb,
                files=files, command=" ".join(sys.argv))


def _log_edit(identifier, text):
    """Archive an edit from the CLI. No message id: the hook learns receipts only from
    `Sent. event_id=`, which an edit never prints, so an id here would mean two rows in a hook
    runtime. With no id, log_event leaves a hook runtime to the hook. Never raises."""
    C = _comms_module("commslib")
    if C is not None:
        C.log_event("whatsapp", "send", identifier, text=text, verb="edit",
                    command=" ".join(sys.argv))


def _put_message(token, portal_id, content, ctx, op):
    """The text PUT, with the gate reservation closed on every outcome."""
    txn = f"wamx{int(time.time() * 1000)}"
    try:
        resp = _request(
            "PUT",
            f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/send/m.room.message/{txn}",
            token=token,
            body=content,
        )
    except MatrixError:
        _gate_finish(ctx, op, "failed_before_send")      # the server answered: not stored
        raise
    except BaseException:
        _gate_finish(ctx, op, "uncertain")               # it may have landed
        raise
    event_id = (resp or {}).get("event_id")
    _gate_finish(ctx, op, "sent" if event_id else "uncertain", [event_id])
    return resp


# --- full read: Matrix events -> msgschema ------------------------------------------

def _is_real_message(event):
    """An m.room.message that is a message of its own (not an m.replace edit)."""
    if event.get("type") != "m.room.message":
        return False
    rel = (event.get("content") or {}).get("m.relates_to") or {}
    return rel.get("rel_type") != "m.replace"


def _redacted_ids(events):
    out = set()
    for e in events:
        if e.get("type") == "m.room.redaction":
            rid = e.get("redacts") or (e.get("content") or {}).get("redacts")
            if rid:
                out.add(rid)
        if (e.get("unsigned") or {}).get("redacted_because"):
            out.add(e.get("event_id"))
    return out


def _walk_back(token, portal_id, enough, before=None, max_pages=None):
    """Page /messages backwards until enough(events) is True or history ends.
    Returns (events newest-first, end_reached). `before` anchors the start on an event id."""
    events, since = [], None
    if before:
        ctx = _request("GET", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/context/"
                              f"{urllib.parse.quote(str(before), safe='')}",
                       token=token, params={"limit": 0})
        since = ctx.get("start")
        if not since:
            return [], True
    for _ in range(FULL_MAX_PAGES if max_pages is None else max_pages):
        params = {"dir": "b", "limit": FULL_PAGE}
        if since:
            params["from"] = since
        data = _request("GET", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/messages",
                        token=token, params=params)
        chunk = data.get("chunk") or []
        events.extend(chunk)
        nxt = data.get("end")
        if not chunk or not nxt or nxt == since:
            return events, True
        since = nxt
        if enough(events):
            return events, False
    return events, False


def _fetch_event(token, portal_id, event_id):
    try:
        return _request("GET", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/event/"
                               f"{urllib.parse.quote(str(event_id), safe='')}", token=token)
    except (MatrixError, ConnectionError, OSError):
        return None


def _to_messages(token, portal_id, selected, window, *, transcribe, self_number,
                 download_dir=None):
    """Fold relations from `window` (every fetched event) onto `selected` (real messages,
    newest-first) and return msgschema Messages, oldest first."""
    S = _comms_module("msgschema")
    if S is None:
        raise MatrixError(0, "msgschema.py could not load from ~/.claude/communications")
    by_id = {e.get("event_id"): e for e in window if e.get("event_id")}
    redacted = _redacted_ids(window)
    wanted = {e.get("event_id") for e in selected}
    edits, reactions = {}, {}
    for e in window:
        content = e.get("content") or {}
        rel = content.get("m.relates_to") or {}
        if e.get("event_id") in redacted or not content:
            continue                                  # a redacted edit/reaction no longer counts
        if e.get("type") == "m.room.message" and rel.get("rel_type") == "m.replace":
            target = rel.get("event_id")
            orig = by_id.get(target)
            if orig and orig.get("sender") == e.get("sender"):
                edits.setdefault(target, []).append(e)  # only the author can edit (forgery guard)
        elif e.get("type") == "m.reaction" and rel.get("rel_type") == "m.annotation":
            if rel.get("event_id") in wanted:
                reactions.setdefault(rel.get("event_id"), []).append(e)
    fetched = [0]

    def parent(eid):
        e = by_id.get(eid)
        if e is None and fetched[0] < FULL_MAX_PARENTS:
            fetched[0] += 1
            e = _fetch_event(token, portal_id, eid)
            if e:
                by_id[eid] = e
        if e is None:
            return S.ReplyRef(id=eid, incomplete=True)
        gone = eid in redacted or not (e.get("content") or {}) or \
            bool((e.get("unsigned") or {}).get("redacted_because"))
        newest = sorted(edits.get(eid, []), key=lambda x: x.get("origin_server_ts") or 0)
        text = (((newest[-1].get("content") or {}).get("m.new_content") or {}).get("body", "")
                if newest else (e.get("content") or {}).get("body", ""))
        return S.ReplyRef(id=eid, text="" if gone else text, deleted=gone)

    out = []
    for e in reversed(selected):
        eid = e.get("event_id")
        content = e.get("content") or {}
        deleted = eid in redacted or not content
        sender = e.get("sender", "?")
        msg = S.Message(id=eid, chat_id=portal_id, channel="whatsapp",
                        sender=_sender_label(sender), sender_id=sender,
                        is_from_me=_is_owner(sender, self_number),
                        ts=(e.get("origin_server_ts") or 0) / 1000.0, deleted=deleted)
        if not deleted:
            body = content.get("body", "") or ""
            msgtype = content.get("msgtype")
            if msgtype in MEDIA_MSGTYPES:
                info = content.get("info") or {}
                filename = content.get("filename")
                att = S.Attachment(id=content.get("url"), name=filename or body,
                                   mime=info.get("mimetype"), size=info.get("size"))
                msg.text = body if filename and filename != body else ""
                if msgtype == "m.audio" and content.get("url"):
                    if not transcribe:
                        att.transcript_status = "off"
                    else:
                        try:
                            att.transcript = _transcribe_event(token, e) or None
                            att.transcript_status = "ok" if att.transcript else "unavailable"
                        except (MatrixError, ConnectionError, OSError):
                            att.transcript_status = "unavailable"
                if download_dir and content.get("url"):
                    try:
                        os.makedirs(download_dir, exist_ok=True)
                        path = os.path.join(download_dir, _media_filename(e, len(out) + 1))
                        with open(path, "wb") as fh:
                            fh.write(_download_media(token, content["url"]))
                        att.local_path = os.path.abspath(path)
                    except (MatrixError, ConnectionError, OSError) as err:
                        msg.extra["download_error"] = str(err)[:120]
                msg.attachments.append(att)
            else:
                msg.text = body
            if msgtype and msgtype != "m.text":
                msg.extra["msgtype"] = msgtype
            history = sorted(edits.get(eid, []), key=lambda x: x.get("origin_server_ts") or 0)
            if history:
                msg.edits = [S.Edit(ts=msg.ts, text=msg.text)] + [
                    S.Edit(ts=(x.get("origin_server_ts") or 0) / 1000.0,
                           text=((x.get("content") or {}).get("m.new_content") or {}).get("body", ""))
                    for x in history]
                msg.text = msg.edits[-1].text
            reply = (content.get("m.relates_to") or {}).get("m.in_reply_to") or {}
            if reply.get("event_id"):
                msg.reply_to = parent(reply["event_id"])
        else:
            msg.extra["deleted_by"] = "redaction"
        for r in sorted(reactions.get(eid, []), key=lambda x: x.get("origin_server_ts") or 0):
            key = ((r.get("content") or {}).get("m.relates_to") or {}).get("key") or ""
            msg.reactions.append(S.Reaction(key=key, sender=_sender_label(r.get("sender", "?"))))
        out.append(msg)
    return out


def _history_full(token, mgmt_room, chat, args):
    S = _comms_module("msgschema")
    if S is None:
        print("msgschema.py could not load from ~/.claude/communications; --full needs it.")
        sys.exit(1)
    portal_id = chat["portal_id"]
    n = max(1, int(args.number))
    before = getattr(args, "before", None)
    window, end = _walk_back(
        token, portal_id, lambda evs: sum(1 for e in evs if _is_real_message(e)) >= n, before=before)
    reals = [e for e in window if _is_real_message(e)]
    selected = reals[:n]
    messages = _to_messages(token, portal_id, selected, window,
                            transcribe=not args.no_transcribe,
                            self_number=_self_number(token, mgmt_room),
                            download_dir=getattr(args, "download", None))
    oldest = selected[-1].get("event_id") if selected else None
    truncated = len(selected) < n and not end
    more = f"--before {oldest}" if (oldest and not (end and len(reals) <= n)) else None
    extra = {}
    if before:
        # Edits and reactions are newer than their message. A window anchored with --before
        # only folds the relations it fetched; it says so instead of looking complete.
        extra["relations_complete"] = False
    env = S.envelope("whatsapp", {"id": portal_id, "label": chat["label"], "is_group": chat["is_group"]},
                     messages, complete=not truncated, truncated=truncated, more=more,
                     requested=n, **extra)
    print(S.dumps(env))


def _unread_full(token, mgmt_room, unread, names, transcribe):
    """Every unread room with its unread messages in full (see `unread --full`)."""
    S = _comms_module("msgschema")
    if S is None:
        print("msgschema.py could not load from ~/.claude/communications; --full needs it.")
        sys.exit(1)
    self_number = _self_number(token, mgmt_room)
    rooms = []
    for (room_id, _room_data, count), name in zip(unread, names):
        label = name or room_id
        if "Admin Room" in label:
            continue
        want = min(count, UNREAD_MAX_PER_ROOM)
        chat = {"id": room_id, "label": label}
        try:
            def enough(evs):
                theirs = 0
                for e in evs:
                    if not _is_real_message(e):
                        continue
                    if _is_owner(e.get("sender", ""), self_number):
                        return True
                    theirs += 1
                    if theirs >= want:
                        return True
                return False

            window, end = _walk_back(token, room_id, enough)
            picked, stop = [], "history_end" if end else "page_cap"
            for e in window:
                if not _is_real_message(e):
                    continue
                if _is_owner(e.get("sender", ""), self_number):
                    stop = "owner_message"
                    break
                picked.append(e)
                if len(picked) >= want:
                    stop = "count"
                    break
            messages = _to_messages(token, room_id, picked, window, transcribe=transcribe,
                                    self_number=self_number)
            truncated = count > UNREAD_MAX_PER_ROOM or stop == "page_cap"
            oldest = picked[-1].get("event_id") if picked else None
            rooms.append(S.room_entry(
                chat, messages, complete=stop in ("owner_message", "history_end"),
                truncated=truncated, more=(f"history {room_id} --full --before {oldest}"
                                           if truncated and oldest else None),
                unread_count=count, basis="notification_count", stopped_at=stop))
        except (MatrixError, ConnectionError, OSError) as err:
            rooms.append(S.room_entry(chat, [], complete=False, error=str(err)[:200],
                                      unread_count=count, basis="notification_count"))
    print(S.dumps(S.rooms_envelope("whatsapp", rooms)))


# --- Commands -----------------------------------------------------------------

def cmd_login(args):
    """Password-login as the configured Matrix user and cache {token, mgmt_room} (chmod 600)."""
    print(f"Logging in to {_homeserver()} as {_login_user()}...")
    try:
        resp = _request(
            "POST",
            "/_matrix/client/v3/login",
            body={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": _login_user()},
                "password": args.password or os.environ.get("MATRIX_PASSWORD", ""),
            },
        )
    except MatrixError as e:
        print(f"Login failed: {e}")
        sys.exit(1)
    except ConnectionError as e:
        print(e)
        sys.exit(1)

    token = resp.get("access_token")
    if not token:
        print(f"Login returned no access_token: {json.dumps(resp)}")
        sys.exit(1)
    print(f"Logged in as {resp.get('user_id')} (device {resp.get('device_id')}).")

    # Find or create the management room: a DM with the bridge bot.
    mgmt_room = _find_or_create_mgmt_room(token)
    print(f"Management room: {mgmt_room}")

    _write_session(SESSION_FILE, {"token": token, "mgmt_room": mgmt_room})
    print(f"Session cached to {SESSION_FILE} (chmod 600).")


def _write_session(path, data):
    """The Matrix token never sits in a file other users can read, not even for a moment."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)  # os.open keeps the old mode of a file that already exists
        json.dump(data, f)


def _find_or_create_mgmt_room(token):
    """Return a management room (DM with the bot), creating one if needed.

    Reuses a cached room if one is already known, otherwise scans joined rooms
    for a 2-member DM that includes the bot, and finally creates a fresh DM.
    """
    _, cached_room = _load_session()
    if cached_room:
        return cached_room

    # Scan joined rooms for an existing DM with the bot.
    try:
        joined = _request(
            "GET", "/_matrix/client/v3/joined_rooms", token=token
        ).get("joined_rooms", [])
    except MatrixError:
        joined = []
    for room_id in joined:
        try:
            members = _request(
                "GET",
                f"/_matrix/client/v3/rooms/{_encode_room(room_id)}/joined_members",
                token=token,
            ).get("joined", {})
        except MatrixError:
            continue
        if _bot_user() in members and len(members) <= 2:
            name = _room_name(token, room_id)
            if "Admin Room" not in name:
                return room_id

    # Create a fresh DM with the bot.
    resp = _request(
        "POST",
        "/_matrix/client/v3/createRoom",
        token=token,
        body={
            "invite": [_bot_user()],
            "is_direct": True,
            "preset": "trusted_private_chat",
        },
    )
    return resp.get("room_id")


def cmd_status(args):
    """Reachability check + WhatsApp login state (via `list-logins`)."""
    # 1. Reachability (no auth required).
    try:
        versions = _request("GET", "/_matrix/client/versions")
        n = len(versions.get("versions", []))
        print(f"Homeserver:      {_homeserver()} (reachable, {n} spec versions)")
    except ConnectionError as e:
        print(f"Homeserver:      UNREACHABLE — {e}")
        sys.exit(1)

    # 2. WhatsApp login state via the bridge bot.
    token, room = _require_session()
    if not room:
        print("WhatsApp:        (no management room — run `login`)")
        return

    print(f"Management room: {room}")
    marker = _latest_event_id(token, room)
    _send_mgmt_command(token, room, "list-logins")
    reply = _await_bot_reply(token, room, marker)
    if reply is None:
        print("WhatsApp:        (no reply from bridge bot within timeout)")
        return
    _write_self_number_cache(token, room, _parse_self_number(reply))
    print("WhatsApp logins:")
    printed = False
    for line in reply.splitlines():
        line = line.strip()
        if line.startswith("*"):
            print(f"  {line.lstrip('* ').strip()}")
            printed = True
    if not printed:
        print(f"  {reply.strip()}")


def cmd_search(args):
    """Search WhatsApp contacts via the bridge bot's `search` command."""
    saved = _saved_contacts(args.query)
    if saved:
        print(f"Saved contacts matching '{args.query}' ({len(saved)}):\n")
        for number, display in saved:
            print(f"  {number}  {display}")
        print()

    token, room = _require_session()
    if not room:
        print("No management room — run `login`.")
        sys.exit(1)

    marker = _latest_event_id(token, room)
    _send_mgmt_command(token, room, f"search {args.query}")
    reply = _await_bot_reply(token, room, marker)
    if reply is None:
        print("No reply from bridge bot within timeout.")
        sys.exit(1)

    results = _parse_search_results(reply)
    if not results:
        print(f"No contacts found matching '{args.query}'.")
        return

    print(f"Search results for '{args.query}' ({len(results)}):\n")
    for number, display in results:
        print(f"  {number}  {display}")


def cmd_history(args):
    """Resolve a chat (DM or GROUP) and print recent messages.

    SAFETY: resolution goes through `_resolve_chat` — bot commands hit the
    MANAGEMENT room (never a portal); we then JOIN and READ the portal — no message
    is ever posted into it. Works for a DM number/name OR a GROUP room_id/name/JID.
    By DEFAULT, m.audio voice notes are transcribed inline via the local whisper
    service (read-only POST to box-localhost, never to a portal) and shown as
    `[voice: <transcript>]`. Pass --no-transcribe to skip that (faster; voice
    notes show as `[voice note]`). If whisper is unreachable, a voice note shows
    `[voice note — transcription unavailable]` and never crashes the command.
    """
    token, room = _require_session()
    if not room:
        print("No management room — run `login`.")
        sys.exit(1)

    # 1. Resolve + join the portal (DM or GROUP; management room / join — SAFE).
    chat = _resolve_chat(token, room, args.identifier)
    portal_id = chat["portal_id"]
    if getattr(args, "full", False):
        return _history_full(token, room, chat, args)
    if getattr(args, "before", None) or getattr(args, "download", None):
        print("--before and --download need --full.")
        sys.exit(2)

    # 2. Read messages (GET only — never POST into a portal).
    data = _request(
        "GET",
        f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/messages",
        token=token,
        params={"dir": "b", "limit": args.number},
    )
    events = [e for e in data.get("chunk", []) if e.get("type") == "m.room.message"]
    if not events:
        print(f"No messages in the portal for '{args.identifier}' "
              f"(backfill depth is limited by WhatsApp).")
        return

    # chunk is newest -> oldest; print oldest -> newest.
    events.reverse()
    kind = "GROUP" if chat["is_group"] else "chat"
    # The room id goes AFTER "(N messages)": `comms` and the KB exporter parse the
    # label as everything up to that parenthesis.
    print(f"History for {kind} {chat['label']} ({len(events)} messages) "
          f"[room {portal_id}]:\n")
    # Voice notes are transcribed inline BY DEFAULT (read-only); --no-transcribe
    # opts out for speed. Failures degrade to a placeholder, never crash.
    transcribe = not args.no_transcribe
    for event in events:
        ts = _format_ts(event.get("origin_server_ts", 0))
        sender = _sender_label(event.get("sender", "?"))
        content = event.get("content", {})
        body = content.get("body", "")
        line = f"  [{ts}] {sender}: {body}"
        line += _voice_note_suffix(token, event, transcribe=transcribe)
        # --ids is opt-in: `comms` and the KB exporter parse the default line.
        if getattr(args, "ids", False):
            line += f"  [{event.get('event_id')}]"
        print(line)


def cmd_chats(args):
    """List materialized portal rooms (joined rooms minus mgmt + Admin Room).

    Each row is tagged `[group]` or `[dm]` via `_is_group` (READ-ONLY classify).
    """
    token, mgmt_room = _require_session()
    self_number = _self_number(token, mgmt_room)

    joined = _request("GET", "/_matrix/client/v3/joined_rooms", token=token).get("joined_rooms", [])

    def _row(room_id):
        name = _room_name(token, room_id)
        if "Admin Room" in name:
            return None
        is_group = _is_group(token, room_id, self_number=self_number)
        # A name-less group gets a member-derived label so it's still recognizable.
        if is_group and not name:
            label = _group_label(token, room_id, self_number)
        else:
            label = name or room_id
        body, ts = _last_message(token, room_id)
        return (label, is_group, ts, body)

    # self_number is resolved above, so the pool only issues per-room GETs.
    rows = _map_parallel(_row, [room_id for room_id in joined if room_id != mgmt_room])
    portals = [row for row in rows if row]

    if not portals:
        print("No portal rooms yet.")
        return

    # Newest activity first.
    portals.sort(key=lambda p: p[2], reverse=True)
    portals = portals[: args.number]

    print(f"Portal chats ({len(portals)}):\n")
    for label, is_group, ts, body in portals:
        when = _format_ts(ts) if ts else "—"
        tag = "[group]" if is_group else "[dm]"
        print(f"  {tag} {label} (last: {when})")
        preview = (body or "")[:60]
        if preview:
            print(f"    > {preview}")


def cmd_groups(args):
    """List GROUP chats with each one's newest message — READ-ONLY.

    Production view of every group you're in, newest-activity first. For each group
    it shows the label (name, or a member-derived label when the m.room.name hasn't
    synced), last-activity time, and a preview of the latest message (sender + body;
    voice notes transcribed inline unless --no-transcribe). `-n` limits the count.

    AUTONOMY: first auto-accepts any pending portal invites (`_accept_portal_invites`)
    so groups that became active since last run appear on their own — no manual
    `create-portal`. GET/JOIN-only: joined_rooms + per-room state/last-message +
    invite-joins; it NEVER posts content into a portal.
    """
    token, mgmt_room = _require_session()
    transcribe = not getattr(args, "no_transcribe", False)

    # AUTONOMY: pull in any chats the bridge has newly invited us to.
    newly = _accept_portal_invites(token, mgmt_room)
    if newly:
        print(f"(joined {len(newly)} newly-invited chat(s))\n")

    # FAST: one /sync for name + classification + recent timeline of every room.
    portals = _sync_portals(token)

    groups = []
    for room_id, p in portals.items():
        jid = p["jid"]
        if not jid.endswith("@g.us"):
            continue  # DM or non-portal room (mgmt/admin have no @g.us bridge JID)
        label = p["name"] or f"(unnamed group {jid})"
        # Newest message from the room's recent timeline (events are oldest->newest).
        ts, sender, body = 0, "", ""
        for event in reversed(p["timeline"]):
            if event.get("type") == "m.room.message":
                ts = event.get("origin_server_ts", 0)
                sender = _sender_label(event.get("sender", "?"))
                body = (event.get("content", {}).get("body", "") or "").replace("\n", " ").strip()
                body += _voice_note_suffix(token, event, transcribe=transcribe)
                break
        groups.append((label, room_id, ts, sender, body))

    if not groups:
        print("No group chats yet. (A group appears once it has activity and its "
              "portal is materialized — send a message in it, or open it on your phone.)")
        return

    # Newest activity first; apply -n if given.
    groups.sort(key=lambda g: g[2], reverse=True)
    total = len(groups)
    limit = getattr(args, "number", None)
    if limit:
        groups = groups[:limit]

    shown = f" (showing {len(groups)} of {total})" if limit and len(groups) < total else ""
    print(f"Group chats ({total}){shown}:\n")
    for label, room_id, ts, sender, body in groups:
        when = _format_ts(ts) if ts else "—"
        print(f"  {label}  (last: {when})")
        if body:
            preview = body if len(body) <= 80 else body[:80] + "…"
            who = f"{sender}: " if sender else ""
            print(f"    > {who}{preview}")


def cmd_unread(args):
    """List rooms with unread messages via a filtered /sync.

    The last-line preview transcribes an unread voice note inline BY DEFAULT
    (read-only) so an incoming m.audio shows its text rather than a blank/
    filename placeholder. --no-transcribe opts out (faster; shows `[voice note]`),
    and an unreachable whisper degrades to a placeholder, never crashing.

    AUTONOMY: first auto-accepts any pending portal invites so unread messages in a
    brand-new chat/group are caught without a manual `create-portal` (JOIN-only).
    """
    token, mgmt_room = _require_session()

    # AUTONOMY: join any newly-invited portals so their unreads are visible below.
    _accept_portal_invites(token, mgmt_room)

    # Minimal sync: room timeline of 1 event, no presence/account data noise.
    sync_filter = json.dumps({
        "room": {"timeline": {"limit": 1}},
        "presence": {"types": []},
        "account_data": {"types": []},
    })
    data = _request(
        "GET",
        "/_matrix/client/v3/sync",
        token=token,
        params={"filter": sync_filter, "timeout": "0"},
    )

    transcribe = not args.no_transcribe
    joined = data.get("rooms", {}).get("join", {})
    unread = []
    for room_id, room_data in joined.items():
        if room_id == mgmt_room:
            continue
        count = room_data.get("unread_notifications", {}).get("notification_count", 0)
        if count > 0:
            unread.append((room_id, room_data, count))
    # Names in parallel (one GET per room); voice transcription below stays serial.
    names = _map_parallel(lambda item: _room_name(token, item[0]), unread)
    if getattr(args, "full", False):
        return _unread_full(token, mgmt_room, unread, names, transcribe)

    found = []
    for (room_id, room_data, count), name in zip(unread, names):
        name = name or room_id
        if "Admin Room" in name:
            continue
        # Latest message from the sync timeline; fall back to a fetch.
        body, ts, preview = "", 0, ""
        for event in reversed(room_data.get("timeline", {}).get("events", [])):
            if event.get("type") == "m.room.message":
                body = event.get("content", {}).get("body", "")
                ts = event.get("origin_server_ts", 0)
                # Voice notes: transcribe inline (default) so the preview isn't
                # a blank/filename. Falls back to a placeholder, never crashes.
                preview = _voice_note_suffix(token, event, transcribe=transcribe).strip()
                break
        found.append((name, count, ts, preview or (body or "")[:60]))

    if not found:
        print("No unread messages.")
        return

    found.sort(key=lambda r: r[1], reverse=True)
    print(f"Unread ({len(found)} chats):\n")
    for name, count, ts, preview in found:
        when = _format_ts(ts) if ts else "—"
        print(f"  {name} ({count} unread, last: {when})")
        if preview:
            print(f"    > {preview}")


def _resolve_number(token, room, identifier):
    """Resolve a send identifier to a single WhatsApp number, or exit non-zero.

    - All-digit identifier -> used as the number directly (no guessing).
    - Otherwise treated as a NAME and resolved via the bot's `search` command
      (MANAGEMENT room — SAFE). Returns (number, display) ONLY on an EXACTLY-one
      match. Zero or multiple matches print the candidates and exit non-zero — we
      NEVER guess among several contacts.
    """
    if identifier.isdigit():
        return identifier, ""

    marker = _latest_event_id(token, room)
    _send_mgmt_command(token, room, f"search {identifier}")
    reply = _await_bot_reply(token, room, marker)
    if reply is None:
        print("No reply from bridge bot within timeout.")
        sys.exit(1)

    results = _parse_search_results(reply)
    if not results:
        print(f"No contact matches '{identifier}' — refusing to send.")
        sys.exit(1)
    if len(results) > 1:
        print(f"'{identifier}' is ambiguous ({len(results)} matches) — refusing to send.")
        print("Disambiguate by passing the exact number:")
        for number, display in results:
            print(f"  {number}  {display}")
        sys.exit(1)

    number, display = results[0]
    return number, display


# --- @mention resolution (READ-ONLY GET of the target room's own members) ------
#
# A real WhatsApp mention rides ENTIRELY on the HTML pill in `formatted_body`
# (source-confirmed against mautrix-whatsapp `main`, pkg/msgconv): if the event
# has no `org.matrix.custom.html` format, NO ping is produced no matter what
# `m.mentions` says. `m.mentions.user_ids` is only an allowlist FILTER over the
# pills, so we set it to the EXACT same mxids the pills carry. The mxid's server
# domain and its LID-vs-phone form are load-bearing, so we NEVER hardcode them —
# every mention is resolved against a real `@whatsapp_*` ghost that is already a
# member of THIS target room, which simultaneously proves the ghost exists, is a
# genuine participant WhatsApp will ping, and carries the room's real domain.

def _ghost_members(token, portal_id):
    """Return [(mxid, localpart, display_name)] for @whatsapp_ ghosts in a room.

    Reads joined_members (GET only). Excludes the bridge bot (@whatsappbot...).
    `localpart` is the WhatsApp id between '@whatsapp_' and ':server' — a phone
    number or a 'lid-<id>'. `display_name` comes from each member's profile in the
    joined_members response ('' when absent).
    """
    members = _portal_members(token, portal_id)
    ghosts = []
    for mxid, profile in members.items():
        if not mxid.startswith("@whatsapp_") or mxid.startswith("@whatsappbot"):
            continue
        localpart = mxid[len("@whatsapp_"):].split(":", 1)[0]
        display_name = (profile or {}).get("display_name", "") or ""
        ghosts.append((mxid, localpart, display_name))
    return ghosts


def _print_mention_candidates(value, candidates, portal_id, why):
    """Print the candidate members for a failed --mention and refuse to guess."""
    print(f"--mention '{value}' {why} in room {portal_id} — refusing to guess.")
    print("Candidates (id  display-name  [mxid]):")
    for mxid, localpart, name in candidates:
        print(f"  {localpart}  {name or '(no name)'}  [{mxid}]")


def _resolve_one_mention(value, ghosts, portal_id):
    """Resolve ONE --mention value to a single ghost member, or exit non-zero.

    Match order (case-insensitive), mirroring the tool's refuse-to-guess style:
      (i)  exact ghost localpart / id (e.g. 'lid-14281202495595' or a number);
      (ii) else display-name substring.
    Exactly one match wins; zero or several exits non-zero with the candidates.
    """
    v = value.casefold()
    exact = [g for g in ghosts if g[1].casefold() == v]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        _print_mention_candidates(value, exact, portal_id, "matched several members by id")
        sys.exit(1)
    subs = [g for g in ghosts if v in (g[2] or "").casefold()]
    if len(subs) == 1:
        return subs[0]
    if not subs:
        _print_mention_candidates(value, ghosts, portal_id, "matched no member")
        sys.exit(1)
    _print_mention_candidates(value, subs, portal_id, f"matched {len(subs)} members by name")
    sys.exit(1)


def _apply_mentions(message, resolutions):
    """Build the HTML `formatted_body` from the plain message + resolved mentions.

    For each resolution, replace the FIRST case-insensitive occurrence of its
    `value` in the still-plain text with a matrix.to pill whose anchor text is the
    matched substring AS IT APPEARS in the message (so 'Alex' stays 'Alex').
    If `value` is NOT present in the message text, PREPEND the pill (anchor = the
    member's display name, or the raw value when it has no name) followed by a
    space. Text is HTML-escaped per segment; a segment already turned into a pill
    is never re-searched, so overlapping or duplicate values cannot nest. Each
    resolution dict is annotated with `anchor` (the rendered @Name) for the preview.
    """
    # Segments are uniform 3-tuples: ("text", raw, "") is searchable, ("pill",
    # mxid, anchor) is frozen. Uniform arity keeps the render unpack total.
    segments = [("text", message, "")]
    for res in resolutions:
        value = res["value"]
        mxid = res["mxid"]
        low = value.casefold()
        placed = False
        for i, seg in enumerate(segments):
            if seg[0] != "text":
                continue
            raw = seg[1]
            idx = raw.casefold().find(low)
            if idx == -1:
                continue
            before = raw[:idx]
            matched = raw[idx:idx + len(value)]  # matched text AS IT APPEARS
            after = raw[idx + len(value):]
            res["anchor"] = matched
            new_segs = []
            if before:
                new_segs.append(("text", before, ""))
            new_segs.append(("pill", mxid, matched))
            if after:
                new_segs.append(("text", after, ""))
            segments[i:i + 1] = new_segs
            placed = True
            break
        if not placed:
            res["anchor"] = res["display_name"] or value
            segments[0:0] = [("pill", mxid, res["anchor"]), ("text", " ", "")]
    out = []
    for kind, first, second in segments:
        if kind == "text":
            out.append(html.escape(first))
        else:
            url = "https://matrix.to/#/" + first  # first = mxid
            out.append(f'<a href="{html.escape(url)}">{html.escape(second)}</a>')  # second = anchor
    return "".join(out)


def _send_content_with_mentions(token, portal_id, message, mention_values):
    """Return (content_dict, resolutions) for a mention send, or exit non-zero.

    `content_dict` is the m.room.message this command PUTs — it carries the pills
    in `formatted_body`, `format: org.matrix.custom.html`, the plain `body`
    fallback, and `m.mentions.user_ids` set to the EXACT list of pill mxids.
    """
    ghosts = _ghost_members(token, portal_id)
    if not ghosts:
        print(f"No WhatsApp members found in room {portal_id} — cannot resolve --mention.")
        sys.exit(1)
    resolutions = []
    user_ids = []
    for value in mention_values:
        mxid, localpart, display_name = _resolve_one_mention(value, ghosts, portal_id)
        resolutions.append({
            "value": value, "mxid": mxid,
            "localpart": localpart, "display_name": display_name,
        })
        if mxid not in user_ids:
            user_ids.append(mxid)
    formatted_body = _apply_mentions(message, resolutions)
    content = {
        "msgtype": "m.text",
        "body": message,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_body,
        "m.mentions": {"user_ids": user_ids},
    }
    return content, resolutions


# `send --since`: the chat moved after the message the caller read, or it could
# not be re-read. Distinct from 1 (bad input) and 4 (delivery unconfirmed): with
# this code NOTHING was sent, so the caller may redraft and ask again.
STALE_EXIT = 5


def _parse_since(value):
    """--since: epoch seconds (what `send`'s own preview prints and /inbox passes),
    or epoch milliseconds, or `empty` (0.0: you read the chat and it had no
    message; refused if one is there now). A minute-precision clock time is
    refused: a message in the same minute would hide behind it."""
    if str(value).strip().lower() == "empty":
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"--since needs epoch seconds (the preview prints it), got {value!r}") from None
    if v > 1e12:
        v /= 1000.0
    if v <= 0:
        raise argparse.ArgumentTypeError("--since must be a real time, not 0")
    return v


def _newer_than(events, since_s, tolerance=1.0):
    """The m.room.message events newer than `since_s` (epoch seconds), oldest
    first. Pure. Any sender counts: a reply from the owner's phone arrives as their
    `lid-` or number puppet, a send from another session as the CLI user, and both must
    stop a second answer just as a new message from them must."""
    cut = (since_s + tolerance) * 1000.0
    out = [e for e in events or []
           if e.get("type") == "m.room.message" and (e.get("origin_server_ts") or 0) > cut]
    return sorted(out, key=lambda e: e.get("origin_server_ts") or 0)


def _one_line(text, cap):
    text = " ".join(str(text or "").split())
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _chat_unchanged_guard(token, portal_id, since):
    """Re-read the chat LIVE right before a send (Matrix gets bridged messages in
    seconds; the comms KB copy /inbox scans runs 20-30 minutes behind).

    Always prints the newest message and the `--since` value that pins it, so a
    dry run doubles as the read. With `since`, refuses (STALE_EXIT) when anything
    is newer, and also when the chat cannot be read or shows no message at all:
    a re-read that sees nothing must never count as "nothing new"."""
    try:
        data = _request(
            "GET",
            f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/messages",
            token=token,
            params={"dir": "b", "limit": 30},
        )
    except (MatrixError, ConnectionError, OSError) as e:
        if since is None:
            print(f"  (could not read the newest message: {e})")
            return
        print(f"NOT SENT: could not re-read the chat before sending ({e}).")
        sys.exit(STALE_EXIT)
    msgs = [e for e in data.get("chunk", []) if e.get("type") == "m.room.message"]
    if msgs:
        top = max(msgs, key=lambda e: e.get("origin_server_ts") or 0)
        ts = top.get("origin_server_ts") or 0
        print(f"  newest message here: [{_format_ts(ts)}] {_sender_label(top.get('sender', '?'))}: "
              f"{_one_line(top.get('content', {}).get('body'), 80)}  (--since {ts / 1000:.3f})")
    else:
        print("  no message visible in this chat  (--since empty)")
    if since is None:
        return
    if not msgs:
        if since == 0.0:
            return                      # `--since empty`, and it still is
        print("NOT SENT: the re-read shows no message in this chat, so --since "
              "cannot be checked.")
        sys.exit(STALE_EXIT)
    newer = _newer_than(msgs, since)
    if newer:
        print(f"NOT SENT: {len(newer)} newer message(s) in this chat since the one you read:")
        for e in newer:
            print(f"  [{_format_ts(e.get('origin_server_ts'))}] {_sender_label(e.get('sender', '?'))}: "
                  f"{_one_line(e.get('content', {}).get('body'), 200)}")
        print("Re-read the chat, redraft, and get a fresh yes before sending.")
        sys.exit(STALE_EXIT)


def _print_next_since(token, portal_id, resp):
    """After a send, print the sent message's own server time as `next --since`,
    so the NEXT bubble of the same reply is checked against it (anything that
    lands between bubbles stops the rest). One GET; on failure say so, and the
    caller must not send the next bubble unchecked."""
    event_id = (resp or {}).get("event_id")
    try:
        ev = _request("GET", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/event/"
                             f"{urllib.parse.quote(str(event_id), safe='')}", token=token)
        print(f"next --since {int(ev['origin_server_ts']) / 1000:.3f}")
    except (MatrixError, ConnectionError, OSError, KeyError, TypeError, ValueError) as e:
        print(f"next --since unknown (could not read the sent message's time: {e})")


def _report_sent(resp):
    """Print the send receipt. Under OUTBOUND_GUARDS=1, refuse to claim 'Sent' without an
    event_id (Guard 3, delivery): a missing receipt means the send is unconfirmed, not done.
    Off by default, so the historical `Sent. event_id=...` line is unchanged."""
    event_id = resp.get("event_id")
    if os.environ.get("OUTBOUND_GUARDS") == "1" and not event_id:
        print("NOT SENT: no event_id in the bridge response — delivery unconfirmed.")
        sys.exit(4)
    print(f"Sent. event_id={event_id}")


def _apply_recipient_guard(token, chat, portal_id, args):
    """Guard 2 (recipient/audience): refuse an excluded recipient or an unintended group send.

    Active only when OUTBOUND_GUARDS=1 (the caller checks). Fail-closed by design: if the guard
    module is missing, or a group's membership cannot be read, it REFUSES rather than send blind —
    the operator opted in with the flag, so a guard that cannot run must not silently pass a send.
    Pure logic lives in outbound_guards.py, in the folder `whatsapp.recipient_guard_dir` (one source of truth with the Slack
    CLI; mirrored by test_outbound_guards.py).
    """
    try:
        _guards_dir = os.path.expanduser(str(kitconfig.require("whatsapp.recipient_guard_dir")))
        if _guards_dir not in sys.path:
            sys.path.insert(0, _guards_dir)
        from outbound_guards import evaluate_recipient, describe_audience
    except Exception as exc:  # noqa: BLE001 — any import failure must fail closed, not send
        print(f"Refusing to send: recipient guard unavailable ({exc}); OUTBOUND_GUARDS=1 requires it.")
        sys.exit(3)

    if chat.get("is_group"):
        # Real group membership (GET only); display names, bridge bot already excluded.
        audience = [name or localpart for (_mxid, localpart, name) in _ghost_members(token, portal_id)]
    else:
        audience = [chat.get("label") or getattr(args, "identifier", "")]

    result = evaluate_recipient(
        audience,
        group_intended=getattr(args, "group_ok", False),
        exclude=getattr(args, "exclude", None) or [],
    )
    if not result["allow"]:
        print(f"Refusing to send: {result['reason']}  [{describe_audience(audience)}]")
        sys.exit(2)


def cmd_send(args):
    """Send a WhatsApp message — a guarded portal-write path (DM or GROUP).

    *** CONTENT PORTAL WRITE SITE #1 of 2 (the other is
    `_upload_and_send_media`) ***
    These are the only two places in the whole file that PUT relayed-content
    m.room.message events into portal rooms. `_send_mgmt_command` stays strictly
    for management-room bot commands; do not repurpose it for portal writes.
    The guard is unchanged: resolve one unambiguous target, always preview, and
    PUT only with --yes. The flow is:

      1. Resolve <identifier> to ONE target via `_resolve_chat` (digits/name -> a
         DM; a `!roomid`/`@g.us`/group-name -> that GROUP). It aborts on a zero/
         multiple/ambiguous name match — never guesses.
      2. For a DM the resolver already start-chats + joins the portal; for a GROUP
         it joins the existing portal directly (there is NO number — we skip number
         resolution and write straight to the portal room).
      3. PRINT a preview line ALWAYS — a GROUP preview is unmistakably labelled.
      4. Without --yes: stop (dry-run), send nothing. With --yes: PUT the message
         into THAT portal room and print the event_id.
    """
    token, room = _require_session()
    if not room:
        print("No management room — run `login`.")
        sys.exit(1)

    message = " ".join(args.message)
    if not message.strip():
        print("Refusing to send an empty message.")
        sys.exit(1)

    # 1./2. Resolve to ONE target (DM or GROUP) + join its portal (or exit).
    #    For a GROUP there is no phone number; the resolver returns the portal
    #    directly and we write straight to it. For a DM the resolver uses the full
    #    `<number>@s.whatsapp.net` JID (avoids the usync lookup that fails for your
    #    own number). Either way the target is unchanged — this only locates it.
    chat = _resolve_chat(token, room, args.identifier)
    portal_id = chat["portal_id"]

    # Guard 2 (flag-gated): recipient/audience check. Active only when OUTBOUND_GUARDS=1; it can
    # only ever REFUSE a send, never cause one. Off by default, so normal behavior is unchanged.
    if os.environ.get("OUTBOUND_GUARDS") == "1":
        _apply_recipient_guard(token, chat, portal_id, args)

    # 3. ALWAYS show the preview before any send — group vs DM is unmistakable.
    if chat["is_group"]:
        print(f'About to send to GROUP "{chat["label"]}" [room {portal_id}]: "{message}"')
    else:
        print(f'About to send to {chat["label"]} [room {portal_id}]: "{message}"')

    # Live re-read, AFTER the preview line (/inbox parses the first "About to
    # send to" line, and a quoted message body must never be read as it) and
    # BEFORE the --yes gate, so a dry run with --since shows the refusal too.
    _chat_unchanged_guard(token, portal_id, getattr(args, "since", None))

    # Build the event content. With NO --mention this is byte-identical to the
    # historical 2-key text event; a mention adds the HTML pill + m.mentions that
    # mautrix-whatsapp requires to actually ping a WhatsApp participant. Resolution
    # runs BEFORE the --yes gate so the dry-run preview shows exactly what --yes
    # would relay (and can still abort non-zero on an unresolvable mention).
    mentions = getattr(args, "mention", None)
    if mentions:
        content, resolutions = _send_content_with_mentions(token, portal_id, message, mentions)
        for res in resolutions:
            print(f'  mention "{res["value"]}" -> {res["mxid"]}  renders as @{res["anchor"]}')
        print(f"  content: {json.dumps(content, ensure_ascii=False)}")
    else:
        content = {"msgtype": "m.text", "body": message}

    # Outbound gate on the FINAL text (P0). Report mode never changes this output or exit code.
    gate = _gate_check(args, target=portal_id, texts=[content.get("body", message)],
                       recipients=_gate_recipients(token, portal_id, chat, args.identifier),
                       dry_run=not args.yes, source="whatsapp send")

    if not args.yes:
        print("Dry run — pass --yes to actually send.")
        return
    if getattr(args, "since", None) is None:
        # Required since 2026-09-23 (owner's call): a send with no anchor cannot tell
        # whether he already answered from his phone, so it does not go out.
        print("NOT SENT: --yes needs --since. Run without --yes first; the preview "
              "prints the value for the chat as you read it.")
        sys.exit(2)

    # 4. CONTENT PORTAL WRITE #1 of 2: PUT m.room.message into THIS portal room.
    op = _gate_begin(gate)
    resp = _put_message(token, portal_id, content, gate, op)
    _report_sent(resp)
    _log_send(args.identifier, content.get("body", message), (resp or {}).get("event_id"), "send")
    _print_next_since(token, portal_id, resp)


# ---------------------------------------------------------------- reactions
# WhatsApp keeps ONE reaction per person per message and shows only an emoji, so
# a word or two emojis as the key is dropped or shown wrong. The owner's own habits
# (measured 2026-10-28 over 176 chats): ❤️ 75%, 👍 12%, then 😂 and 🙏.
OWNER_PHONE_LID = str(_SETTINGS.get("owner_lid", ""))  # the phone puppet id; "" = none
MONEY_LOCK_EXIT = 6
REACTION_ALIASES = {
    "heart": "❤️", "love": "❤️",
    "thumbsup": "👍", "thumbs-up": "👍", "thumbs_up": "👍", "like": "👍", "+1": "👍",
    "laugh": "😂", "haha": "😂", "lol": "😂", "joy": "😂",
    "pray": "🙏", "thanks": "🙏",
    "fire": "🔥", "wow": "😮", "sad": "😢", "check": "✅", "clap": "👏",
    "raised_hands": "🙌", "eyes": "👀", "100": "💯", "party": "🎉", "tada": "🎉",
}
_VARIATION_SELECTORS = {0xFE0E, 0xFE0F}


def _is_skin_tone(cp):
    return 0x1F3FB <= cp <= 0x1F3FF


def _is_regional_indicator(cp):
    return 0x1F1E6 <= cp <= 0x1F1FF


def _is_emoji_base(cp):
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2300 <= cp <= 0x23FF
            or 0x2B00 <= cp <= 0x2BFF or 0x2190 <= cp <= 0x21FF or 0x25A0 <= cp <= 0x25FF
            or 0x2900 <= cp <= 0x297F
            or cp in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x24C2,
                      0x3030, 0x303D, 0x3297, 0x3299))


def _emoji_units(text):
    """How many separate emojis `text` holds, or None if anything else is in it.
    A ZWJ family, a skin tone, a variation selector and a two-letter flag all
    count as part of one emoji (stdlib only, so no grapheme library)."""
    units, joined, flag_open = 0, False, False
    for ch in text:
        cp = ord(ch)
        if cp in _VARIATION_SELECTORS or _is_skin_tone(cp) or 0xE0020 <= cp <= 0xE007F \
                or cp == 0x20E3:
            if not units:
                return None
            continue
        if cp == 0x200D:                         # zero-width joiner
            if not units:
                return None
            joined = True
            continue
        if _is_regional_indicator(cp):
            if flag_open:                        # second letter of a flag
                flag_open = False
                continue
            flag_open = True
        elif _is_emoji_base(cp):
            flag_open = False
        else:
            return None                          # a letter, a digit, a space, ':'
        if not joined:
            units += 1
        joined = False
    return units


def _reaction_key(value):
    """An alias (heart, thumbsup, laugh, ...) or ONE raw emoji -> the reaction key.
    Raises ValueError on a word, two emojis or an empty value."""
    text = str(value or "").strip()
    alias = REACTION_ALIASES.get(text.lower())
    if alias:
        return alias
    if _emoji_units(text) != 1:
        raise ValueError(f"a reaction is ONE emoji or a name like heart, thumbsup, laugh, "
                         f"pray (got {value!r})")
    return text


def _norm_key(key):
    """Compare keys without variation selectors or skin tones: the owner's phone
    sends 👍️ (with U+FE0F), and that is the same reaction as 👍."""
    return "".join(ch for ch in str(key or "")
                   if ord(ch) not in _VARIATION_SELECTORS and not _is_skin_tone(ord(ch)))


def _is_owner(sender, self_number=""):
    """The owner through this CLI (@<matrix user>...), on their phone (the LID puppet,
    `whatsapp.owner_lid`), or their number puppet in a chat from before the LID migration."""
    sender = str(sender or "")
    if sender.startswith(_owner_prefix()):
        return True
    if OWNER_PHONE_LID and sender.startswith(f"@whatsapp_lid-{OWNER_PHONE_LID}:"):
        return True
    return bool(self_number) and sender.startswith(f"@whatsapp_{self_number}:")


def _react_target(chunk, self_number=""):
    """The newest real message from someone other than the owner (chunk is newest
    first, as /messages dir=b returns it), or None. Skips the owner on both paths,
    the bridge bot, notices, reactions and redacted messages."""
    for e in sorted(chunk or [], key=lambda e: -(e.get("origin_server_ts") or 0)):
        if not _is_real_message(e) or not e.get("content"):
            continue
        sender = e.get("sender", "")
        if _is_owner(sender, self_number) or sender.startswith("@whatsappbot"):
            continue
        if e["content"].get("msgtype") == "m.notice":
            continue
        return e
    return None


def _owner_reactions(chunk, self_number=""):
    """The owner's reactions among an m.annotation relations chunk ->
    [{event_id, key, via}] with via = "tool" (this CLI, removable) or "phone"."""
    out = []
    for e in chunk or []:
        if e.get("type") != "m.reaction":
            continue
        sender = e.get("sender", "")
        if not _is_owner(sender, self_number):
            continue
        key = ((e.get("content") or {}).get("m.relates_to") or {}).get("key")
        out.append({"event_id": e.get("event_id"), "key": key,
                    "via": "tool" if sender.startswith(_owner_prefix()) else "phone"})
    return out


def _load_draft_check():
    """The skill's draft_check.py, loaded from an exact path: next to this file
    (the skill), else WHATSAPP_SKILL_DIR (the mirror
    in another repo ships without it). None when neither exists. A
    path, not `import`, so a draft_check cached from somewhere else never counts."""
    import importlib.util  # noqa: PLC0415
    for folder in (os.path.dirname(os.path.abspath(__file__)),  # kit: code path
                   os.environ.get("WHATSAPP_SKILL_DIR", "")):
        if not folder:
            continue
        path = os.path.join(folder, "draft_check.py")
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location("_wa_draft_check", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None


def _money_hits(text):
    """draft_check.py's MONEY terms in the message being reacted to, or None when
    the check cannot run. A heart or a thumbs up on a price or a deadline reads
    as a yes, so the caller locks on both a hit and None."""
    try:
        draft_check = _load_draft_check()
        if draft_check is None:
            return None
        flat = draft_check.normalize_for_matching(str(text or ""))
        return [m.group(0).strip() for m in draft_check.MONEY.finditer(flat)]
    except Exception:  # noqa: BLE001 — a check that cannot run must lock, not pass
        return None


def _bridge_refusal(token, portal_id, event_id, wait=4.0):
    """After a reaction or a redaction, give the bridge `wait` seconds, then
    look for its refusal: a whatsappbot "not bridged" / "failed" notice, or a
    com.beeper.message_send_status FAIL for our event. Returns the reason or
    None. None is "no refusal seen", not a delivery receipt."""
    time.sleep(wait)
    try:
        ours = _request("GET", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/event/"
                               f"{urllib.parse.quote(str(event_id), safe='')}", token=token)
        since_ms = int(ours.get("origin_server_ts") or 0)
        data = _request("GET", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/messages",
                        token=token, params={"dir": "b", "limit": 20})
    except (MatrixError, ConnectionError, OSError, TypeError, ValueError) as e:
        return f"could not re-read the chat to check the bridge ({e})"
    for e in data.get("chunk", []):
        content = e.get("content") or {}
        if e.get("type") == "com.beeper.message_send_status":
            rel = content.get("m.relates_to") or {}
            if rel.get("event_id") == event_id and str(content.get("status", "")).startswith("FAIL"):
                return content.get("message") or content.get("reason") or content.get("status")
            continue
        if (e.get("sender", "").startswith("@whatsappbot")
                and (e.get("origin_server_ts") or 0) >= since_ms
                and re.search(r"not bridged|failed", str(content.get("body", "")), re.I)):
            return content.get("body")
    return None


def cmd_react(args):
    """React to ONE WhatsApp message with an emoji, or remove this tool's reaction.

    *** PORTAL WRITE SITE (reactions): an m.reaction PUT, or a redaction of one.
    It relays no text. Same guard shape as `send`: always a preview first, and
    `--yes` must name the previewed message with `--event`, so a message that
    arrives in between can never become the target. ***
    """
    token, room = _require_session()
    if not room:
        print("No management room — run `login`.")
        sys.exit(1)
    key = None
    if not args.remove:
        try:
            key = _reaction_key(args.emoji)
        except ValueError as e:
            print(f"NOT SENT: {e}")
            sys.exit(1)

    chat = _resolve_chat(token, room, args.identifier)
    portal_id = chat["portal_id"]
    if os.environ.get("OUTBOUND_GUARDS") == "1":
        _apply_recipient_guard(token, chat, portal_id, args)
    self_number = _read_self_number_cache(token, room)
    enc = _encode_room(portal_id)

    recent = _request("GET", f"/_matrix/client/v3/rooms/{enc}/messages", token=token,
                      params={"dir": "b", "limit": 50}).get("chunk", [])
    if args.event:
        try:
            target = _request("GET", f"/_matrix/client/v3/rooms/{enc}/event/"
                                     f"{urllib.parse.quote(args.event, safe='')}", token=token)
        except MatrixError as e:
            print(f"NOT SENT: no message {args.event} in this chat ({e}).")
            sys.exit(1)
        if target.get("type") != "m.room.message" or not target.get("content"):
            print(f"NOT SENT: {args.event} is not a message (it is {target.get('type')}).")
            sys.exit(1)
    else:
        target = _react_target(recent, self_number)
        if not target:
            print("NOT SENT: no message from them in the last 50 to react to. Pass --event "
                  "with the exact id (`history <chat> --ids` prints it).")
            sys.exit(1)
    event_id = target["event_id"]
    content = target.get("content") or {}
    body = content.get("body") or f"[{content.get('msgtype', 'message')}]"
    who = "your" if _is_owner(target.get("sender"), self_number) \
        else f"{_sender_label(target.get('sender', '?'))}'s"
    where = f'GROUP "{chat["label"]}"' if chat["is_group"] else chat["label"]
    action = "remove your reaction from" if args.remove else f"react {key} to"
    print(f'About to {action} {who} message in {where} [room {portal_id}]: '
          f'"{_one_line(body, 120)}"  [event {event_id}]')

    newer = [e for e in recent if e.get("type") == "m.room.message"
             and (e.get("origin_server_ts") or 0) > (target.get("origin_server_ts") or 0)]
    if newer:
        top = max(newer, key=lambda e: e.get("origin_server_ts") or 0)
        print(f"  note: {len(newer)} newer message(s) after this one. Newest: "
              f"{_sender_label(top.get('sender', '?'))}: "
              f"{_one_line((top.get('content') or {}).get('body'), 80)}")

    try:
        relations = _request(
            "GET", f"/_matrix/client/v1/rooms/{enc}/relations/"
                   f"{urllib.parse.quote(event_id, safe='')}/m.annotation",
            token=token, params={"limit": 50}).get("chunk", [])
    except (MatrixError, ConnectionError, OSError) as e:
        relations = None
        print(f"  (could not read the reactions already on it: {e})")
    mine = _owner_reactions(relations or [], self_number)
    if relations:
        counts = {}
        for r in relations:
            k = ((r.get("content") or {}).get("m.relates_to") or {}).get("key")
            counts[k] = counts.get(k, 0) + 1
        print("  reactions there now: " + ", ".join(f"{k} x{n}" for k, n in counts.items()))

    if args.remove:
        removable = [r for r in mine if r["via"] == "tool"]
        if relations is None:
            print("NOT SENT: cannot find your reaction without reading the reactions on it.")
            sys.exit(1)
        if not mine:
            print("NOT SENT: you have no reaction on this message.")
            return
        if not removable:
            print(f"NOT SENT: your {mine[0]['key']} there came from your phone. This tool can "
                  "only remove reactions it sent. Tap it on your phone to remove it.")
            sys.exit(1)
        if not args.yes:
            print(f"Dry run — to remove {removable[0]['key']}, re-run with --remove --yes "
                  f"--event {event_id}")
            return
        if not args.event:
            print("NOT SENT: --yes needs --event (the preview prints it).")
            sys.exit(2)
        for r in removable:
            txn = f"wamx{int(time.time() * 1000)}"
            resp = _request("PUT", f"/_matrix/client/v3/rooms/{enc}/redact/"
                                   f"{urllib.parse.quote(r['event_id'], safe='')}/{txn}",
                            token=token, body={})
            print(f"Removed {r['key']}. event_id={resp.get('event_id')}")
            refusal = _bridge_refusal(token, portal_id, resp.get("event_id"))
            if refusal:
                print(f"NOT SENT: the bridge did not pass the removal to WhatsApp: {refusal}")
                sys.exit(4)
        print("No bridge error seen in 4s.")
        return

    same = [r for r in mine if _norm_key(r["key"]) == _norm_key(key)]
    if same:
        print(f"NOT SENT: you already reacted {same[0]['key']} to this message "
              f"(from {'this tool' if same[0]['via'] == 'tool' else 'your phone'}). Nothing to do.")
        return
    if mine:
        print(f"  note: WhatsApp keeps one reaction per person. This replaces your {mine[0]['key']}.")
    hits = _money_hits(body)
    locked = hits is None or bool(hits)
    if hits is None:
        print("  MONEY LOCK: the money check (draft_check.py) could not run, so treat this "
              "message as money. Show the owner the message, get a fresh yes, then add --money-ok.")
    elif hits:
        print(f"  MONEY LOCK: the message mentions {', '.join(sorted(set(hits)))}. A reaction "
              "can read as a yes. Show the owner the message, get a fresh yes, then add --money-ok.")
    if "?" in body:
        print("  note: this message asks a question. A reaction does not answer it.")

    if not args.yes:
        print(f"Dry run — to react, re-run with --yes --event {event_id}"
              + (" --money-ok" if locked else ""))
        return
    if not args.event:
        print("NOT SENT: --yes needs --event (the preview prints it), so the reaction lands "
              "on the message you previewed.")
        sys.exit(2)
    if locked and not args.money_ok:
        print("NOT SENT: money lock. Show the owner the message, get a fresh yes, then add --money-ok.")
        sys.exit(MONEY_LOCK_EXIT)

    txn = f"wamx{int(time.time() * 1000)}"
    resp = _request("PUT", f"/_matrix/client/v3/rooms/{enc}/send/m.reaction/{txn}", token=token,
                    body={"m.relates_to": {"rel_type": "m.annotation",
                                           "event_id": event_id, "key": key}})
    sent_id = resp.get("event_id")
    if not sent_id:
        print("NOT SENT: no event_id in the bridge response — reaction unconfirmed.")
        sys.exit(4)
    print(f"Reacted {key}. event_id={sent_id}")
    refusal = _bridge_refusal(token, portal_id, sent_id)
    if refusal:
        print(f"NOT SENT: the bridge did not pass the reaction to WhatsApp: {refusal}")
        sys.exit(4)
    print("No bridge error seen in 4s.")


def cmd_download(args):
    """Download recent media from a chat to a local dir — READ-ONLY.

    Resolves the portal (DM or GROUP) via `_resolve_chat` (bot commands hit the
    MANAGEMENT room, then JOIN), scans the recent timeline for media events, and
    downloads the most recent (or last -n) via the AUTHENTICATED media endpoint.
    Never posts into the portal.
    """
    token, room = _require_session()
    if not room:
        print("No management room — run `login`.")
        sys.exit(1)

    portal_id = _resolve_chat(token, room, args.identifier)["portal_id"]

    # Scan a generous window so we still find media when interleaved with text.
    scan = max(args.number * 10, 50)
    events = _media_events(token, portal_id, scan)
    if not events:
        print(f"No media found in the recent timeline for '{args.identifier}'.")
        return

    # events are newest -> oldest; take the most recent N.
    events = events[: args.number]

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    print(f"Downloading {len(events)} media item(s) to {out_dir}:\n")
    for i, event in enumerate(events):
        content = event.get("content", {})
        msgtype = content.get("msgtype", "?")
        info = content.get("info", {}) or {}
        mimetype = info.get("mimetype", "")
        try:
            raw = _download_media(token, content.get("url"))
        except (MatrixError, ConnectionError) as e:
            print(f"  [{msgtype}] download failed: {e}")
            continue
        name = _media_filename(event, i)
        local_path = os.path.join(out_dir, name)
        # Avoid clobbering when several items share a body/name.
        if os.path.exists(local_path):
            stem, ext = os.path.splitext(name)
            local_path = os.path.join(out_dir, f"{stem}_{i}{ext}")
        with open(local_path, "wb") as f:
            f.write(raw)
        print(f"  {local_path}  ({msgtype}, {len(raw)} bytes, {mimetype or 'unknown'})")


def cmd_send_media(args):
    """Send a media file to a WhatsApp chat through the guarded media write path.

    This command and `cmd_send_voice_confirm` both reach the single
    `_upload_and_send_media` content-write helper. Each has a distinct dry-run
    path and reaches the helper only when `--yes` is explicit.

    The flow:
      1. Resolve <identifier> to one DM or GROUP, aborting on ambiguity.
      2. Read the local file and infer its Matrix media type.
      3. Print the exact target, room, file, size, and caption.
      4. Without --yes, stop before upload. With --yes, upload and send.
    """
    token, room = _require_session()
    if not room:
        print("No management room — run `login`.")
        sys.exit(1)

    filepath = os.path.abspath(args.filepath)
    if not os.path.isfile(filepath):
        print(f"File not found: {filepath}")
        sys.exit(1)
    size = os.path.getsize(filepath)
    if size == 0:
        print("Refusing to send an empty file.")
        sys.exit(1)

    # 1. Resolve to ONE target (DM or GROUP) + join its portal (or exit non-zero).
    chat = _resolve_chat(token, room, args.identifier)
    portal_id = chat["portal_id"]

    # Guard 2 (flag-gated): recipient/audience check on the media path too. Off by default.
    if os.environ.get("OUTBOUND_GUARDS") == "1":
        _apply_recipient_guard(token, chat, portal_id, args)

    # 2. Guess mimetype -> msgtype.
    filename = os.path.basename(filepath)
    mimetype = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
    if mimetype.startswith("image/"):
        msgtype = "m.image"
    elif mimetype.startswith("audio/"):
        msgtype = "m.audio"
    elif mimetype.startswith("video/"):
        msgtype = "m.video"
    else:
        msgtype = "m.file"

    # A real caption needs the Matrix `filename` field (spec v1.10 / MSC2530):
    # when `filename` is set AND differs from `body`, clients render `body` as the
    # caption. With ONLY `body` set, WhatsApp shows that text as the file's NAME,
    # not as a caption. Sending `--caption` without a `filename` field was the bug
    # that renamed the PDF instead of captioning it.
    has_caption = args.caption is not None and args.caption.strip() != ""
    caption = args.caption if has_caption else None

    # 3./4. ALWAYS show the preview before any send/upload — group vs DM unmistakable.
    if chat["is_group"]:
        target = f'GROUP "{chat["label"]}"'
    else:
        target = chat["label"]
    caption_note = f' caption="{caption}"' if has_caption else " (no caption)"
    print(f'About to send {msgtype} {filename} ({size} bytes) to {target} '
          f'[room {portal_id}]{caption_note}')

    # Live re-read, AFTER the preview line and BEFORE the --yes gate (same as
    # `send`), so a dry run with --since shows the refusal and nothing uploads.
    _chat_unchanged_guard(token, portal_id, getattr(args, "since", None))

    # Outbound gate (P0): the caption, or the filename WhatsApp shows as the body, plus the
    # words of a small text file. A binary file is listed as not read.
    with open(filepath, "rb") as media_file:
        media_bytes = media_file.read()
    G = _comms_module("outbound_gate")
    file_text = G.readable_bytes(media_bytes) if G else None
    gate = _gate_check(args, target=portal_id, texts=[caption] if has_caption else [],
                       recipients=_gate_recipients(token, portal_id, chat, args.identifier),
                       filenames=[filename],
                       attachment_texts=[(filename, file_text)] if file_text else [],
                       unread=[] if file_text else [filename],
                       hashes=[hashlib.sha256(media_bytes).hexdigest()],
                       dry_run=not args.yes, source="whatsapp send-media")

    if not args.yes:
        # Dry-run: do NOT upload, do NOT post the room event.
        print("Dry run — pass --yes to actually send.")
        return
    if getattr(args, "since", None) is None:
        print("NOT SENT: --yes needs --since. Run without --yes first; the preview "
              "prints the value for the chat as you read it.")
        sys.exit(2)

    content = {
        "msgtype": msgtype,
        # No caption: `body` is the filename (legacy). With a caption: `body` holds
        # the caption text and `filename` carries the real name, so clients render
        # the text as a caption instead of renaming the file.
        "body": caption if has_caption else filename,
        "info": {"mimetype": mimetype, "size": size},
    }
    if has_caption:
        content["filename"] = filename

    # 5. The shared helper owns the ONLY relayed-media portal PUT.
    op = _gate_begin(gate)
    try:
        resp = _upload_and_send_media(token, portal_id, filepath, mimetype, content, raw_bytes=media_bytes)
    except MatrixError:
        _gate_finish(gate, op, "failed_before_send")
        raise
    except BaseException:
        _gate_finish(gate, op, "uncertain")
        raise
    if resp is None:
        _gate_finish(gate, op, "failed_before_send")
        print("Upload returned no content_uri — aborting.")
        sys.exit(1)
    event_id = resp.get("event_id")
    _gate_finish(gate, op, "sent" if event_id else "uncertain", [event_id])
    _report_sent(resp)
    _log_send(args.identifier, caption, event_id, "send-media", files=[filename])
    _print_next_since(token, portal_id, resp)

def cmd_send_voice(args):
    """Prepare exact cloned-voice files for review; never send to WhatsApp."""
    token, room = _require_session()
    if not room:
        print("No management room — run `login`.")
        sys.exit(1)
    provider = _voice_provider()

    message = " ".join(args.message).strip()
    if not message:
        print("Refusing to generate an empty voice memo.")
        sys.exit(1)

    expressive = bool(getattr(args, "expressive", False))
    audio_tags = VOICE_AUDIO_TAG_PATTERN.findall(message)
    if audio_tags and not expressive:
        raise VoiceMemoError(
            "Performance tags such as [laughs] require --expressive."
        )
    if expressive:
        if not audio_tags:
            raise VoiceMemoError(
                "--expressive requires [laughs], [giggles], or [chuckles]."
            )
        _hybrid_voice_segments(message)
        model = VOICE_HYBRID_MODEL
    else:
        model = None

    chat = _verified_voice_target(
        token, _resolve_chat(token, room, args.identifier)
    )
    target = f'GROUP "{chat["label"]}"' if chat["is_group"] else chat["label"]
    print(
        f"Preparing a cloned-voice preview for {target} "
        f"[room {chat['portal_id']}]: {json.dumps(message, ensure_ascii=False)}"
    )
    mode = (
        f"{APPROVED_SPEECH_MODEL} speech with Eleven v3 reactions"
        if expressive
        else f"{APPROVED_SPEECH_MODEL} speech"
    )
    print(
        f"This uploads the script to {provider} to generate {mode} in the owner's "
        f"professional voice clone, then adds "
        f"subtle {VOICE_BACKGROUND_LUFS} LUFS fan/room tone. It cannot send "
        "anything to WhatsApp."
    )
    manifest = _prepare_voice_preview(
        args.identifier, message, chat, model=model
    )
    print()
    _print_voice_preview(manifest)
    print("\nNothing was sent.")
    print(
        "Listen to every Audio file above, then inspect the send preview with:\n"  # kit: code path
        f"  python3 {os.path.abspath(__file__)} "
        f"send-voice-confirm {manifest['preview_id']}"
    )


def cmd_send_voice_confirm(args):
    """Show or send a prepared voice preview without regenerating any audio."""
    manifest = _load_voice_preview_manifest(args.preview_id)
    _print_voice_preview(manifest)

    pending = [part for part in manifest["parts"] if not part.get("event_id")]
    if not pending:
        if manifest.get("status") != "sent":
            manifest["status"] = "sent"
            manifest["sent_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            _write_voice_preview_manifest(manifest)
        print("\nThis preview was already sent. Nothing else was sent.")
        return
    if not args.yes:
        _gate_check(args, target=manifest["target"]["portal_id"],
                    texts=[part.get("text", "") for part in pending],
                    recipients=[], dry_run=True, source="whatsapp send-voice-confirm preview")
        print(
            "\nDry run — nothing sent. After listening and explicit approval, "
            "rerun this command with --yes."
        )
        return

    token, room = _require_session()
    if not room:
        raise VoiceMemoError("No management room — run `login`.")
    current_target = _verified_voice_target(
        token,
        _resolve_chat(token, room, manifest["recipient_input"]),
    )
    approved_target = manifest["target"]
    target_keys = ("portal_id", "is_group", "channel_jid")
    if any(
        current_target[key] != approved_target[key]
        for key in target_keys
    ):
        raise VoiceMemoError(
            "Voice preview target changed since approval; generate a new preview."
        )

    validated = _validate_voice_preview_parts(manifest)
    portal_id = approved_target["portal_id"]
    gate = _gate_check(args, target=portal_id,
                       texts=[part.get("text", "") for part in pending],
                       recipients=_gate_recipients(token, portal_id, current_target,
                                                   manifest["recipient_input"]),
                       dry_run=False, source="whatsapp send-voice-confirm")
    print(
        "\nSending the approved cached audio exactly as previewed. "
        "No voice is being regenerated."
    )
    op = _gate_begin(gate)
    sent_ids = []
    try:
        _send_voice_parts(token, portal_id, validated, manifest, approved_target, sent_ids)
    except BaseException:
        _gate_finish(gate, op, "uncertain" if sent_ids else "failed_before_send", sent_ids)
        raise
    _gate_finish(gate, op, "sent", sent_ids)
    manifest["status"] = "sent"
    manifest["sent_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_voice_preview_manifest(manifest)
    print("All approved voice memo parts were sent.")


def _send_voice_parts(token, portal_id, validated, manifest, approved_target, sent_ids):
    """The voice part loop (moved out of cmd_send_voice_confirm unchanged, P0)."""
    for part, filepath, audio_bytes in validated:
        resp = _upload_and_send_media(
            token,
            portal_id,
            filepath,
            VOICE_NOTE_MIMETYPE,
            _voice_event_content(part),
            raw_bytes=audio_bytes,
            transaction_id=part["transaction_id"],
        )
        event_id = resp.get("event_id") if resp else None
        if not event_id:
            raise VoiceMemoError(
                f"Part {part['index']} upload/send returned no event ID."
            )
        part["event_id"] = event_id
        sent_ids.append(event_id)
        _log_send(manifest.get("recipient_input") or approved_target["portal_id"],
                  part.get("text", ""), event_id, "send-voice-confirm")
        part["sent_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        manifest["status"] = "partially_sent"
        _write_voice_preview_manifest(manifest)
        print(
            f"Sent part {part['index']}/{len(manifest['parts'])} "
            f"({part['duration_ms'] / 1000:.1f}s). event_id={event_id}"
        )


def cmd_transcribe(args):
    """Transcribe a voice/audio message from a chat — READ-ONLY.

    Resolves + joins the portal (DM or GROUP) via `_resolve_chat` (SAFE), finds the
    target audio event (--message EVENT_ID, else the most recent m.audio), downloads
    it via the authenticated media endpoint, and POSTs it to the local whisper
    service. Never posts into a portal. Prints the transcript + language + elapsed.
    """
    token, room = _require_session()
    if not room:
        print("No management room — run `login`.")
        sys.exit(1)

    portal_id = _resolve_chat(token, room, args.identifier)["portal_id"]

    # Find the target audio event.
    target = None
    if args.message:
        try:
            data = _request(
                "GET",
                f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/event/"
                f"{urllib.parse.quote(args.message, safe='')}",
                token=token,
            )
        except MatrixError as e:
            print(f"Could not fetch event {args.message}: {e}")
            sys.exit(1)
        if data.get("content", {}).get("msgtype") != "m.audio":
            print(f"Event {args.message} is not an m.audio message.")
            sys.exit(1)
        target = data
    else:
        for event in _media_events(token, portal_id, max(args.scan, 50)):
            if event.get("content", {}).get("msgtype") == "m.audio":
                target = event
                break

    if target is None:
        print(f"No voice/audio message found for '{args.identifier}'.")
        return

    content = target.get("content", {})
    info = content.get("info", {}) or {}
    ext = _ext_for_mimetype(info.get("mimetype", ""), fallback=".ogg")
    audio = _download_media(token, content.get("url"))
    fd, tmp_path = tempfile.mkstemp(prefix="wamx_voice_", suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        result = _whisper_transcribe(audio, os.path.basename(tmp_path),
                                     language=args.language)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    text = result.get("text", "").strip()
    lang = result.get("language", "?")
    elapsed = result.get("elapsed", "?")
    when = _format_ts(target.get("origin_server_ts", 0))
    print(f"Transcript ({lang}, {elapsed}s, message from {when}):\n")
    print(f"  {text}" if text else "  (empty transcript)")


# Fixed bridge command the only place sync ever writes into a portal. NOT relayed
# content — the `!wa ` prefix makes the bridge run it as a COMMAND (validated live;
# see the file header). This literal is NEVER built from user input.
_SYNC_COMMAND = "!wa sync-portal"


def cmd_sync(args):
    """Refresh a portal's metadata via the bridge `sync-portal` command.

    *** THIRD portal-write — COMMAND-ONLY, categorically different from send ***
    This PUTs the FIXED literal `!wa sync-portal` into the portal. The `!wa ` prefix
    is intercepted by the bridge and run as a bot COMMAND (it re-fetches the chat's
    name/avatar/members) — it is NOT delivered to the WhatsApp contact(s). This was
    VALIDATED live in the self-DM before shipping: a `!wa id` was consumed as a
    command (the bot answered; no relay, no "not bridged" notice). Because the
    payload is a fixed, prefixed command (never arbitrary/user text), it is not a
    content relay like send/send-media — but it IS still a portal write, so it
    resolves to ONE unambiguous target first (via `_resolve_chat`, which aborts on
    ambiguity) and prints what it did. Use it to fix a group whose name never synced.
    """
    token, room = _require_session()
    if not room:
        print("No management room — run `login`.")
        sys.exit(1)

    # Resolve to ONE target (DM or GROUP) + join its portal (or exit non-zero).
    chat = _resolve_chat(token, room, args.identifier)
    portal_id = chat["portal_id"]
    kind = "GROUP" if chat["is_group"] else "chat"
    print(f'Syncing {kind} "{chat["label"]}" [room {portal_id}] '
          f'via the `{_SYNC_COMMAND}` bridge command...')

    # PORTAL WRITE #3 (command-only): PUT the FIXED `!wa sync-portal` bot command.
    # NOT relayed content — the `!wa ` prefix is intercepted as a command.
    txn = f"wamx{int(time.time() * 1000)}"
    _request(
        "PUT",
        f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/send/m.room.message/{txn}",
        token=token,
        body={"msgtype": "m.text", "body": _SYNC_COMMAND},
    )

    # Give the bridge a moment to act, then report the (possibly refreshed) name.
    time.sleep(REPLY_POLL_INTERVAL_S * 2)
    new_name = _room_name(token, portal_id)
    if new_name:
        print(f"Done. Portal name is now: {new_name}")
    else:
        print("Done. (Portal name still unsynced — the bridge may sync it shortly, "
              "or the chat may have no name set.)")


# ---------------------------------------------------------------- message + group admin
# Added 2026-10-31 (leaf-1.1). reply/edit/delete/mark-read/find act on ONE message; group-* and
# block/unblock act on a group or a contact. Every write is a dry run unless --yes. Only `reply`
# prints `About to send to ...` and `Sent. event_id=` (two parsers read those lines for real
# sends); every other verb uses its own words (`About to edit`, `About to delete`, ...).
UNCONFIRMED_EXIT = 4          # the bridge/homeserver did not do it (or we cannot tell)
UNSUPPORTED_EXIT = 5          # this bridge has no such command
EDIT_WINDOW_MIN = 15          # WhatsApp refuses an edit older than this
FIND_SCAN_EVENTS = 300        # `find --chat` substring scan depth when the server finds nothing
_REFUSAL_RE = re.compile(
    r"not bridged|fail|error|couldn.t|can.t|cannot|not (?:an )?admin|not allowed|"
    r"unsupported|not supported|denied|forbidden|invalid", re.I)


def _is_redacted(event):
    """A message the server blanked: no content, or a redacted_because marker."""
    return (not (event.get("content") or {})) or bool(
        (event.get("unsigned") or {}).get("redacted_because"))


def _admin_event(token, portal_id, event_id, refuse):
    """GET one event of THIS chat, or exit 1 with `<refuse>: ...`. Never writes."""
    try:
        ev = _request("GET", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/event/"
                             f"{urllib.parse.quote(str(event_id), safe='')}", token=token)
    except MatrixError as e:
        print(f"{refuse}: no message {event_id} in this chat ({e}).")
        sys.exit(1)
    if ev.get("type") != "m.room.message":
        print(f"{refuse}: {event_id} is not a message (it is {ev.get('type')}).")
        sys.exit(1)
    if ev.get("room_id") not in (None, portal_id):
        print(f"{refuse}: {event_id} belongs to another chat.")
        sys.exit(1)
    return ev


def _original_event(token, portal_id, ev, refuse):
    """If `ev` is an m.replace edit event, the ORIGINAL it edits (a second edit and a delete
    must point at the original, never at an earlier edit); else `ev`."""
    rel = (ev.get("content") or {}).get("m.relates_to") or {}
    if rel.get("rel_type") == "m.replace" and rel.get("event_id"):
        return _admin_event(token, portal_id, rel["event_id"], refuse)
    return ev


def _current_body(token, portal_id, original):
    """The text people see now: the newest edit by the same sender, else the original body."""
    body = (original.get("content") or {}).get("body") or ""
    try:
        chunk = _request(
            "GET", f"/_matrix/client/v1/rooms/{_encode_room(portal_id)}/relations/"
                   f"{urllib.parse.quote(original['event_id'], safe='')}/m.replace",
            token=token, params={"limit": 50}).get("chunk", [])
    except (MatrixError, ConnectionError, OSError):
        return body
    mine = [e for e in chunk if e.get("sender") == original.get("sender")]
    if not mine:
        return body
    newest = max(mine, key=lambda e: e.get("origin_server_ts") or 0)
    return ((newest.get("content") or {}).get("m.new_content") or {}).get("body") or body


def _own_tool_message(token, room, portal_id, event_id, refuse, verb):
    """The ORIGINAL event, once it is proven to be a message this tool sent as the CLI user.
    Exits 1 for someone else's message and for his phone's (the bridge acts only on what it
    relayed itself). A deleted message is returned as-is; the caller decides."""
    ev = _admin_event(token, portal_id, event_id, refuse)
    ev = _original_event(token, portal_id, ev, refuse)
    sender = ev.get("sender", "")
    if sender.startswith(_owner_prefix()):
        return ev
    if _is_owner(sender, _read_self_number_cache(token, room)):
        print(f"{refuse}: that message was sent from your phone. This tool can only {verb} "
              "messages it sent itself. Do it on your phone.")
    else:
        print(f"{refuse}: you did not send that message ({_sender_label(sender)} did). "
              f"Only your own messages can be {verb}.")
    sys.exit(1)


def _admin_money_lock(texts):
    """Print the MONEY LOCK line and return True when any text mentions money, or when the
    check cannot run (draft_check.py missing locks, same as `react`)."""
    hits, unknown = [], False
    for text in texts:
        found = _money_hits(text)
        if found is None:
            unknown = True
        else:
            hits += found
    if unknown:
        print("  MONEY LOCK: the money check (draft_check.py) could not run, so treat this as "
              "money. Show the owner the exact text, get a fresh yes, then add --money-ok.")
    elif hits:
        print(f"  MONEY LOCK: the text mentions {', '.join(sorted(set(hits)))}. Show the owner the "
              "exact text, get a fresh yes, then add --money-ok.")
    return unknown or bool(hits)


def _where(chat):
    return f'GROUP "{chat["label"]}"' if chat["is_group"] else chat["label"]


def _admin_refusal(token, portal_id, since_ms):
    """A bridge-bot notice newer than `since_ms` that reads like a refusal, or None.
    None is "no refusal seen", not a receipt."""
    try:
        data = _request("GET", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/messages",
                        token=token, params={"dir": "b", "limit": 20})
    except (MatrixError, ConnectionError, OSError):
        return None
    for e in data.get("chunk", []):
        if (e.get("sender", "").startswith("@whatsappbot")
                and (e.get("origin_server_ts") or 0) >= since_ms
                and _REFUSAL_RE.search(str((e.get("content") or {}).get("body", "")))):
            return (e.get("content") or {}).get("body")
    return None


def _await_portal_bot_reply(token, portal_id, since_ms, wait=8.0):
    """The newest @whatsappbot message in a PORTAL room newer than `since_ms`, or None."""
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(REPLY_POLL_INTERVAL_S)
        try:
            data = _request("GET", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/messages",
                            token=token, params={"dir": "b", "limit": 10})
        except (MatrixError, ConnectionError, OSError):
            continue
        for e in data.get("chunk", []):
            if (e.get("type") == "m.room.message" and e.get("sender", "").startswith("@whatsappbot")
                    and (e.get("origin_server_ts") or 0) >= since_ms):
                return (e.get("content") or {}).get("body", "")
    return None


def _admin_session():
    token, room = _require_session()
    if not room:
        print("No management room — run `login`.")
        sys.exit(1)
    return token, room


def cmd_reply(args):
    """Reply to ONE message: a normal send whose content carries m.in_reply_to.

    *** CONTENT PORTAL WRITE (same class as `cmd_send`). It puts text in front of a person, so
    it has the same guards: the recipient guard, the outbound gate, `--since`, a dry run unless
    --yes, the same `About to send to` / `Sent. event_id=` lines, and the archive row. ***
    """
    message = " ".join(args.message)
    if not message.strip():
        print("Refusing to send an empty message.")
        sys.exit(1)
    token, room = _admin_session()
    chat = _resolve_chat(token, room, args.identifier)
    portal_id = chat["portal_id"]
    if os.environ.get("OUTBOUND_GUARDS") == "1":
        _apply_recipient_guard(token, chat, portal_id, args)

    target = _admin_event(token, portal_id, args.event, "NOT SENT")
    target = _original_event(token, portal_id, target, "NOT SENT")
    if _is_redacted(target):
        print("NOT SENT: that message was deleted, so there is nothing to reply to.")
        sys.exit(1)

    if chat["is_group"]:
        print(f'About to send to GROUP "{chat["label"]}" [room {portal_id}]: "{message}"')
    else:
        print(f'About to send to {chat["label"]} [room {portal_id}]: "{message}"')
    print(f'  replying to {_sender_label(target.get("sender", "?"))}: '
          f'"{_one_line((target.get("content") or {}).get("body"), 80)}"  [event {target["event_id"]}]')
    _chat_unchanged_guard(token, portal_id, getattr(args, "since", None))

    content = {"msgtype": "m.text", "body": message,
               "m.relates_to": {"m.in_reply_to": {"event_id": target["event_id"]}}}
    gate = _gate_check(args, target=portal_id, texts=[message],
                       recipients=_gate_recipients(token, portal_id, chat, args.identifier),
                       dry_run=not args.yes, source="whatsapp reply")
    if not args.yes:
        print("Dry run — pass --yes to actually send.")
        return
    if getattr(args, "since", None) is None:
        print("NOT SENT: --yes needs --since. Run without --yes first; the preview "
              "prints the value for the chat as you read it.")
        sys.exit(2)
    op = _gate_begin(gate)
    resp = _put_message(token, portal_id, content, gate, op)
    _report_sent(resp)
    _log_send(args.identifier, message, (resp or {}).get("event_id"), "reply")
    _print_next_since(token, portal_id, resp)


def cmd_edit(args):
    """Edit one of YOUR OWN messages (m.replace). Not a send: the preview says `About to edit`.

    Refuses someone else's message and his phone's. Same guards as `send` for the new text
    (recipient guard, outbound gate, --since) plus a money lock on the old AND new text. A
    bridge refusal (WhatsApp allows edits for 15 minutes) is a failure, never "Edited".
    """
    new = " ".join(args.message)
    if not new.strip():
        print("NOT EDITED: refusing an empty message. To remove a message, use `delete`.")
        sys.exit(1)
    token, room = _admin_session()
    chat = _resolve_chat(token, room, args.identifier)
    portal_id = chat["portal_id"]
    if os.environ.get("OUTBOUND_GUARDS") == "1":
        _apply_recipient_guard(token, chat, portal_id, args)

    orig = _own_tool_message(token, room, portal_id, args.event, "NOT EDITED", "edited")
    if _is_redacted(orig):
        print("NOT EDITED: that message was deleted.")
        sys.exit(1)
    msgtype = (orig.get("content") or {}).get("msgtype")
    if msgtype != "m.text":
        print(f"NOT EDITED: only text messages can be edited (this one is {msgtype}).")
        sys.exit(1)
    old = _current_body(token, portal_id, orig)
    if old == new:
        print("NOT EDITED: the new text is the same as the current text. Nothing to do.")
        return
    orig_id = orig["event_id"]

    print(f'About to edit your message in {_where(chat)} [room {portal_id}]: '
          f'"{_one_line(old, 120)}" -> "{new}"  [event {orig_id}]')
    age_min = (time.time() * 1000.0 - (orig.get("origin_server_ts") or 0)) / 60000.0
    if age_min > EDIT_WINDOW_MIN:
        print(f"  warning: WhatsApp only allows an edit within {EDIT_WINDOW_MIN} minutes of "
              f"sending. This message is {int(age_min)} minutes old, so the bridge will "
              "probably refuse.")
    locked = _admin_money_lock([old, new])
    _chat_unchanged_guard(token, portal_id, getattr(args, "since", None))

    content = {"msgtype": "m.text", "body": "* " + new,
               "m.new_content": {"msgtype": "m.text", "body": new},
               "m.relates_to": {"rel_type": "m.replace", "event_id": orig_id}}
    gate = _gate_check(args, target=portal_id, texts=[new],
                       recipients=_gate_recipients(token, portal_id, chat, args.identifier),
                       dry_run=not args.yes, source="whatsapp edit")
    if not args.yes:
        print("Dry run — pass --yes (with --since from the line above) to edit.")
        return
    if getattr(args, "since", None) is None:
        print("NOT EDITED: --yes needs --since. Run without --yes first; the preview "
              "prints the value for the chat as you read it.")
        sys.exit(2)
    if locked and not args.money_ok:
        print("NOT EDITED: money lock. Show the owner the exact text, get a fresh yes, then add --money-ok.")
        sys.exit(MONEY_LOCK_EXIT)

    op = _gate_begin(gate)
    try:
        resp = _put_message(token, portal_id, content, gate, op)
    except MatrixError as e:
        print(f"NOT EDITED: the homeserver refused the edit ({e}).")
        sys.exit(1)
    sent_id = (resp or {}).get("event_id")
    if not sent_id:
        print("NOT EDITED: no event_id in the bridge response — edit unconfirmed.")
        sys.exit(UNCONFIRMED_EXIT)
    refusal = _bridge_refusal(token, portal_id, sent_id)
    if refusal:
        print(f"NOT EDITED: the bridge did not pass the edit to WhatsApp: {refusal}")
        sys.exit(UNCONFIRMED_EXIT)
    _log_edit(args.identifier, new)
    print(f"Edited. event_id={sent_id}")
    print("No bridge error seen in 4s.")


def cmd_delete(args):
    """Delete (redact) one of YOUR OWN messages. Not a send: the preview says `About to delete`.

    Same ownership rule as `edit`, the money lock on the message being removed, and three
    failure checks: an already-deleted message reports nothing as done, a redaction the bridge
    refuses is exit 4, and a redaction the server did not apply is exit 4.
    """
    token, room = _admin_session()
    chat = _resolve_chat(token, room, args.identifier)
    portal_id = chat["portal_id"]
    orig = _own_tool_message(token, room, portal_id, args.event, "NOT DELETED", "deleted")
    orig_id = orig["event_id"]
    if _is_redacted(orig):
        print("NOT DELETED: that message is already deleted. Nothing to do.")
        return
    body = (orig.get("content") or {}).get("body") or f"[{(orig.get('content') or {}).get('msgtype')}]"
    print(f'About to delete your message in {_where(chat)} [room {portal_id}]: '
          f'"{_one_line(body, 120)}"  [event {orig_id}]')
    locked = _admin_money_lock([body])
    if not args.yes:
        print(f"Dry run — to delete, re-run with --yes{' --money-ok' if locked else ''}.")
        return
    if locked and not args.money_ok:
        print("NOT DELETED: money lock. Show the owner the message, get a fresh yes, then add --money-ok.")
        sys.exit(MONEY_LOCK_EXIT)

    txn = f"wamx{int(time.time() * 1000)}"
    try:
        resp = _request("PUT", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/redact/"
                               f"{urllib.parse.quote(orig_id, safe='')}/{txn}", token=token, body={})
    except MatrixError as e:
        print(f"NOT DELETED: the homeserver refused the redaction ({e}).")
        sys.exit(1)
    sent_id = (resp or {}).get("event_id")
    if not sent_id:
        print("NOT DELETED: no event_id in the response — deletion unconfirmed.")
        sys.exit(UNCONFIRMED_EXIT)
    refusal = _bridge_refusal(token, portal_id, sent_id)
    if refusal:
        print(f"NOT DELETED: the bridge did not pass the deletion to WhatsApp: {refusal}")
        print("  (The message may be gone in Matrix but still on WhatsApp.)")
        sys.exit(UNCONFIRMED_EXIT)
    try:
        after = _admin_event(token, portal_id, orig_id, "NOT DELETED")
    except SystemExit:
        after = None
    if after is not None and not _is_redacted(after):
        print("NOT DELETED: the redaction was accepted but the message is still readable.")
        sys.exit(UNCONFIRMED_EXIT)
    print(f"Deleted. event_id={sent_id}")
    print("No bridge error seen in 4s.")


def _receipt_seen(token, portal_id, event_id):
    """True when the homeserver shows OUR m.read receipt on `event_id` (read back through a
    /sync filtered to this room's receipts)."""
    try:
        me = _request("GET", "/_matrix/client/v3/account/whoami", token=token).get("user_id")
        flt = {"room": {"rooms": [portal_id], "ephemeral": {"types": ["m.receipt"]},
                        "timeline": {"limit": 0}, "state": {"limit": 0}, "account_data": {"limit": 0}},
               "presence": {"limit": 0}, "account_data": {"limit": 0}}
        sync = _request("GET", "/_matrix/client/v3/sync", token=token,
                        params={"filter": json.dumps(flt), "timeout": 0})
    except (MatrixError, ConnectionError, OSError):
        return False
    events = (((sync.get("rooms") or {}).get("join") or {}).get(portal_id) or {}) \
        .get("ephemeral", {}).get("events", [])
    for e in events:
        if e.get("type") == "m.receipt":
            users = ((e.get("content") or {}).get(event_id) or {}).get("m.read") or {}
            if me in users:
                return True
    return False


def cmd_mark_read(args):
    """Send a read receipt up to one message (default: the newest). The other side can see it.

    The homeserver's acceptance is read back. Whether WhatsApp shows blue ticks cannot be
    confirmed: this homeserver does not pass ephemeral events to the bridge.
    """
    token, room = _admin_session()
    chat = _resolve_chat(token, room, args.identifier)
    portal_id = chat["portal_id"]
    if args.event:
        target = _admin_event(token, portal_id, args.event, "NOT MARKED")
    else:
        recent = _request("GET", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/messages",
                          token=token, params={"dir": "b", "limit": 20}).get("chunk", [])
        msgs = [e for e in recent if e.get("type") == "m.room.message"]
        if not msgs:
            print("NOT MARKED: no message in this chat to mark read.")
            sys.exit(1)
        target = max(msgs, key=lambda e: e.get("origin_server_ts") or 0)
    event_id = target["event_id"]
    body = (target.get("content") or {}).get("body") or ""
    print(f'About to mark read {_where(chat)} [room {portal_id}] up to: '
          f'"{_one_line(body, 80)}"  [event {event_id}]')
    if not args.yes:
        print("Dry run — pass --yes to send the read receipt.")
        return
    try:
        _request("POST", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/receipt/m.read/"
                         f"{urllib.parse.quote(event_id, safe='')}", token=token, body={})
    except MatrixError as e:
        print(f"NOT MARKED: the homeserver refused the receipt ({e}).")
        sys.exit(1)
    for attempt in range(3):
        if _receipt_seen(token, portal_id, event_id):
            print(f"Marked read up to {event_id}.")
            print("The homeserver stored it. WhatsApp blue ticks are not confirmable from here.")
            return
        time.sleep(1.0)
    print("NOT MARKED: the homeserver accepted the receipt but does not show it. Unconfirmed.")
    sys.exit(UNCONFIRMED_EXIT)


def _find_row(event, text_override=None):
    return {"ts": event.get("origin_server_ts") or 0, "sender": event.get("sender", "?"),
            "text": text_override if text_override is not None
            else ((event.get("content") or {}).get("body") or ""), "edited": False,
            "room": event.get("room_id")}


def cmd_find(args):
    """Search message text. READ-ONLY. Uses the homeserver's full-text search (continuwuity
    supports POST /search; it matches WHOLE WORDS only, so `invoic` finds nothing there). With
    --chat and no server hit, it scans the last FIND_SCAN_EVENTS events of that chat for the text
    as a substring and says so."""
    term = (args.text or "").strip()
    if not term:
        print("Nothing to search for.")
        sys.exit(1)
    token, room = _admin_session()
    limit = max(1, int(getattr(args, "number", 10) or 10))
    chat, flt = None, {"limit": limit * 4}
    if getattr(args, "chat", None):
        chat = _resolve_chat(token, room, args.chat)
        flt["rooms"] = [chat["portal_id"]]
    scope = _where(chat) if chat else "all your chats"
    try:
        data = _request("POST", "/_matrix/client/v3/search", token=token, body={
            "search_categories": {"room_events": {
                "search_term": term, "keys": ["content.body"], "order_by": "recent", "filter": flt}}})
        results = [r.get("result") or {} for r in
                   (data.get("search_categories", {}).get("room_events", {}).get("results") or [])]
    except MatrixError as e:
        if not chat:
            print(f"Search is unavailable on this homeserver ({e}). Pass --chat to scan one chat.")
            sys.exit(1)
        results = []

    rows = {}                                   # (room, original id) -> row
    for ev in results:
        if ev.get("sender", "").startswith("@whatsappbot") or ev.get("type") != "m.room.message":
            continue
        rel = (ev.get("content") or {}).get("m.relates_to") or {}
        if rel.get("rel_type") == "m.replace" and rel.get("event_id"):
            key = (ev.get("room_id"), rel["event_id"])
            new_body = ((ev.get("content") or {}).get("m.new_content") or {}).get("body")
            row = rows.setdefault(key, _find_row(ev, new_body))
            edit_ts = ev.get("origin_server_ts") or 0
            if new_body is not None and edit_ts >= row.get("edit_ts", 0):
                row.update(text=new_body, edit_ts=edit_ts)      # the NEWEST edit's text wins
            row.update(edited=True, id=rel["event_id"])
        else:
            key = (ev.get("room_id"), ev.get("event_id"))
            row = rows.get(key)
            if row is None:
                row = rows[key] = _find_row(ev)
            else:
                row["ts"], row["sender"] = ev.get("origin_server_ts") or row["ts"], ev.get("sender")
            row["id"] = ev.get("event_id")
    scanned = None
    if not rows and chat:
        events, _end = _walk_back(token, chat["portal_id"],
                                  lambda evs: len(evs) >= FIND_SCAN_EVENTS, max_pages=3)
        events = events[:FIND_SCAN_EVENTS]
        scanned = len(events)
        gone = _redacted_ids(events)
        needle = term.lower()
        for ev in events:
            if not _is_real_message(ev) or ev.get("event_id") in gone:
                continue
            body = (ev.get("content") or {}).get("body") or ""
            if needle in body.lower() and not ev.get("sender", "").startswith("@whatsappbot"):
                row = _find_row(ev)
                row["id"] = ev["event_id"]
                row["room"] = chat["portal_id"]
                rows[(row["room"], row["id"])] = row

    ordered = sorted(rows.values(), key=lambda r: -r["ts"])
    shown = ordered[:limit]
    method = (f"scanned the last {scanned} events of this chat for the text"
              if scanned is not None else "server search matches whole words only")
    print(f'find "{term}" in {scope}: {len(ordered)} message(s), limit {limit}; {method}.')
    if not ordered:
        print("No messages found."
              + ("" if chat else " Pass --chat to also scan one chat for part of a word."))
        return
    names = {}
    for r in shown:
        rid = r.get("room")
        if rid and rid not in names:
            names[rid] = _room_name(token, rid) or rid
        print(f'[{_format_ts(r["ts"])}] {names.get(rid, "?")} | {_sender_label(r["sender"])}: '
              f'{_one_line(r["text"], 160)}{" (edited)" if r["edited"] else ""}  [{r["id"]}]')
    if len(ordered) > limit:
        print(f"(showing {limit} of {len(ordered)}; raise -n)")


# ---- groups + block. Matrix events the bridge translates; every write read back where possible.

def _clean_digits(value, refuse):
    digits = re.sub(r"[\s().\-]", "", str(value or "")).lstrip("+")
    if not (digits.isdigit() and 7 <= len(digits) <= 15):
        print(f"{refuse}: {value!r} is not a phone number (digits with the country code, "
              "for example 12065550123).")
        sys.exit(1)
    return digits


def _group_chat(token, room, identifier, refuse):
    chat = _resolve_chat(token, room, identifier)
    if not chat["is_group"]:
        print(f"{refuse}: {identifier!r} is not a group (it resolved to {chat['label']} "
              f"[room {chat['portal_id']}]). Pass a group name, a !room id or a <id>@g.us id.")
        sys.exit(1)
    return chat


def _ghost_for_digits(token, room, digits):
    """The Matrix ghost for a phone number, found the way a DM finds it: start-chat (a lookup
    that sends no message) then the one non-self puppet in that DM. Exits 1 if it is not one."""
    portal_id = _resolve_portal(token, room, digits)
    others = _other_puppets(token, portal_id, _self_number(token, room))
    if len(others) != 1:
        print(f"Could not tell which WhatsApp account {digits} is ({len(others)} candidates).")
        sys.exit(1)
    return others[0]


def _poll(check, tries=4, pause=1.5):
    for i in range(tries):
        if check():
            return True
        if i < tries - 1:
            time.sleep(pause)
    return False


def cmd_group_members(args):
    """List a group's members with names, numbers and admins. READ-ONLY."""
    token, room = _admin_session()
    chat = _group_chat(token, room, args.group, "NOT A GROUP")
    portal_id = chat["portal_id"]
    ghosts = _ghost_members(token, portal_id)
    try:
        state = _request("GET", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/state", token=token)
    except (MatrixError, ConnectionError, OSError):
        state = []
    levels = {}
    for e in state or []:
        if e.get("type") == "m.room.power_levels":
            levels = (e.get("content") or {}).get("users") or {}
    me = _read_self_number_cache(token, room)
    print(f'GROUP "{chat["label"]}" [room {portal_id}]: {len(ghosts)} members')
    for mxid, localpart, name in sorted(ghosts, key=lambda g: (g[2] or g[1]).lower()):
        tags = []
        if _is_owner(mxid, me):
            tags.append("you")
        if (levels.get(mxid) or 0) >= 50:
            tags.append("admin")
        print(f"  {name or localpart}  {localpart}" + (f"  ({', '.join(tags)})" if tags else ""))


def cmd_group_add(args):
    """Add a person to a group: a Matrix invite of their WhatsApp ghost, which the bridge turns
    into a WhatsApp add. Dry run unless --yes; read back from the member list."""
    digits = _clean_digits(args.digits, "NOT ADDED")
    token, room = _admin_session()
    chat = _group_chat(token, room, args.group, "NOT ADDED")
    portal_id = chat["portal_id"]
    if digits == _self_number(token, room):
        print("NOT ADDED: that is your own number.")
        sys.exit(1)
    mxid = _ghost_for_digits(token, room, digits)
    members = _portal_members(token, portal_id)
    if mxid in members:
        print(f'{digits} is already in GROUP "{chat["label"]}". Nothing to do.')
        return
    print(f'About to add {digits} ({mxid}) to GROUP "{chat["label"]}" [room {portal_id}]  '
          f'({len([m for m in members if m.startswith("@whatsapp_")])} members now)')
    if not args.yes:
        print("Dry run — pass --yes to add them. They are told they were added.")
        return
    since_ms = int(time.time() * 1000) - 2000
    try:
        _request("POST", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/invite",
                 token=token, body={"user_id": mxid})
    except MatrixError as e:
        print(f"NOT ADDED: the homeserver refused the invite ({e}).")
        sys.exit(1)
    time.sleep(1.0)
    refusal = _admin_refusal(token, portal_id, since_ms)
    if refusal:
        print(f"NOT ADDED: the bridge did not add them on WhatsApp: {refusal}")
        sys.exit(UNCONFIRMED_EXIT)
    if not _poll(lambda: mxid in _portal_members(token, portal_id)):
        print(f"NOT ADDED: {digits} is not in the member list after the invite. Unconfirmed; "
              "check the group in WhatsApp.")
        sys.exit(UNCONFIRMED_EXIT)
    print(f'Added {digits} to GROUP "{chat["label"]}".')


def cmd_group_remove(args):
    """Remove a person from a group: a Matrix kick of their ghost, turned into a WhatsApp
    remove. Dry run unless --yes; read back from the member list."""
    digits = _clean_digits(args.digits, "NOT REMOVED")
    token, room = _admin_session()
    chat = _group_chat(token, room, args.group, "NOT REMOVED")
    portal_id = chat["portal_id"]
    if digits == _self_number(token, room):
        print("NOT REMOVED: that is your own number. Use `group-leave` to leave.")
        sys.exit(1)
    mxid = _ghost_for_digits(token, room, digits)
    members = _portal_members(token, portal_id)
    if mxid not in members:
        print(f'NOT REMOVED: {digits} is not in GROUP "{chat["label"]}".')
        sys.exit(1)
    print(f'About to remove {digits} ({mxid}) from GROUP "{chat["label"]}" [room {portal_id}]')
    if not args.yes:
        print("Dry run — pass --yes to remove them.")
        return
    since_ms = int(time.time() * 1000) - 2000
    try:
        _request("POST", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/kick",
                 token=token, body={"user_id": mxid})
    except MatrixError as e:
        print(f"NOT REMOVED: the homeserver refused the kick ({e}).")
        sys.exit(1)
    time.sleep(1.0)
    refusal = _admin_refusal(token, portal_id, since_ms)
    if refusal:
        print(f"NOT REMOVED: the bridge did not remove them on WhatsApp: {refusal}")
        sys.exit(UNCONFIRMED_EXIT)
    if not _poll(lambda: mxid not in _portal_members(token, portal_id)):
        print(f"NOT REMOVED: {digits} is still in the member list after the kick. Unconfirmed.")
        sys.exit(UNCONFIRMED_EXIT)
    print(f'Removed {digits} from GROUP "{chat["label"]}".')


def cmd_group_rename(args):
    """Rename a group: an m.room.name state event the bridge turns into a WhatsApp subject.
    Dry run unless --yes; read back after the bridge has had time to revert a refusal."""
    name = (args.name or "").strip()
    if not name or len(name) > 100:
        print("NOT RENAMED: a group name is 1 to 100 characters.")
        sys.exit(1)
    token, room = _admin_session()
    chat = _group_chat(token, room, args.group, "NOT RENAMED")
    portal_id = chat["portal_id"]
    current = _room_name(token, portal_id) or chat["label"]
    if current == name:
        print(f'NOT RENAMED: the group is already named "{name}". Nothing to do.')
        return
    print(f'About to rename GROUP "{current}" [room {portal_id}] to "{name}"')
    if not args.yes:
        print("Dry run — pass --yes to rename it. Every member sees the change.")
        return
    since_ms = int(time.time() * 1000) - 2000
    try:
        _request("PUT", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/state/m.room.name",
                 token=token, body={"name": name})
    except MatrixError as e:
        print(f"NOT RENAMED: the homeserver refused the rename ({e}).")
        sys.exit(1)
    time.sleep(3.0)
    refusal = _admin_refusal(token, portal_id, since_ms)
    if refusal:
        print(f"NOT RENAMED: the bridge did not rename it on WhatsApp: {refusal}")
        sys.exit(UNCONFIRMED_EXIT)
    now = _room_name(token, portal_id)
    if now != name:
        print(f'NOT RENAMED: the group name is "{now}" after the change; the bridge reverted it.')
        sys.exit(UNCONFIRMED_EXIT)
    print(f'Renamed to "{name}".')


def cmd_group_leave(args):
    """Leave a group: a Matrix leave, turned into a WhatsApp leave. Dry run unless --yes.
    Once you have left, the room can no longer be read, so the result cannot be confirmed."""
    token, room = _admin_session()
    chat = _group_chat(token, room, args.group, "NOT LEFT")
    portal_id = chat["portal_id"]
    print(f'About to leave GROUP "{chat["label"]}" [room {portal_id}]')
    if not args.yes:
        print("Dry run — pass --yes to leave. Members see that you left.")
        return
    try:
        _request("POST", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/leave",
                 token=token, body={})
    except MatrixError as e:
        print(f"NOT LEFT: the homeserver refused ({e}).")
        sys.exit(1)
    print(f'Left GROUP "{chat["label"]}" on Matrix. The result on WhatsApp cannot be read back '
          "from here: check the group in the WhatsApp app.")


def cmd_group_create(args):
    """Create a WhatsApp group through the bridge's own `create-group` command: make a private
    Matrix room, invite the members' ghosts and the bridge bot, run `!wa create-group` in it,
    and trust it only when the room is bridged to an @g.us JID. Dry run unless --yes."""
    name = (args.name or "").strip()
    refuse = "NOT CREATED"
    if not name or len(name) > 100:
        print(f"{refuse}: a group name is 1 to 100 characters.")
        sys.exit(1)
    members = []
    for value in args.member or []:
        d = _clean_digits(value, refuse)
        if d not in members:
            members.append(d)
    if not members:
        print(f"{refuse}: give at least one --member DIGITS.")
        sys.exit(1)
    token, room = _admin_session()
    me = _self_number(token, room)
    if me in members:
        print(f"{refuse}: your own number is added for you; remove it from --member.")
        sys.exit(1)
    ghosts = [_ghost_for_digits(token, room, d) for d in members]
    print(f'About to create GROUP "{name}" with {len(members)} member(s): {", ".join(members)}')
    if not args.yes:
        print("Dry run — pass --yes to create it. Every member is told they were added.")
        return
    resp = _request("POST", "/_matrix/client/v3/createRoom", token=token, body={
        "name": name, "preset": "private_chat", "visibility": "private",
        "invite": ghosts + [_bot_user()]})
    new_room = (resp or {}).get("room_id")
    if not new_room:
        print(f"{refuse}: the homeserver made no room.")
        sys.exit(UNCONFIRMED_EXIT)
    if not _poll(lambda: any(m.startswith("@whatsappbot") for m in _portal_members(token, new_room)),
                 tries=6, pause=1.5):
        try:
            _request("POST", f"/_matrix/client/v3/rooms/{_encode_room(new_room)}/leave",
                     token=token, body={})
        except (MatrixError, ConnectionError, OSError):
            pass
        print(f"{refuse}: the bridge bot did not join the new room [room {new_room}], so nothing "
              "was created on WhatsApp. The empty room was left.")
        sys.exit(UNCONFIRMED_EXIT)
    since_ms = int(time.time() * 1000) - 2000
    txn = f"wamx{int(time.time() * 1000)}"
    # Command-only write: the fixed literal below, never user text (like `sync`).
    _request("PUT", f"/_matrix/client/v3/rooms/{_encode_room(new_room)}/send/m.room.message/{txn}",
             token=token, body={"msgtype": "m.text", "body": "!wa create-group"})
    reply = _await_portal_bot_reply(token, new_room, since_ms, wait=15.0)
    holder = {}

    def bridged():
        holder["jid"] = _portal_channel_jid(token, new_room)
        return holder["jid"].endswith("@g.us")
    if not _poll(bridged, tries=5, pause=2.0):
        print(f"{refuse}: the new room [room {new_room}] is not bridged to a WhatsApp group."
              + (f" The bridge said: {reply.strip()}" if reply else " The bridge gave no reply."))
        sys.exit(UNCONFIRMED_EXIT)
    print(f'Created group "{name}" [room {new_room}] jid={holder["jid"]} with {len(members)} member(s).')


def cmd_block(args):
    """Block a contact through the bridge's `block-user` command, posted as the fixed literal
    `!wa block-user` into that contact's DM portal (command-only, not relayed text).
    Dry run unless --yes. The bridge's reply is read; a WhatsApp-side result is not confirmable."""
    digits = _clean_digits(args.digits, "NOT BLOCKED")
    token, room = _admin_session()
    if digits == _self_number(token, room):
        print("NOT BLOCKED: that is your own number.")
        sys.exit(1)
    chat = _resolve_chat(token, room, digits)
    portal_id = chat["portal_id"]
    if chat["is_group"]:
        print("NOT BLOCKED: that resolved to a group, and only a contact can be blocked.")
        sys.exit(1)
    command = "!wa block-user" + (" --report-spam" if getattr(args, "report_spam", False) else "")
    print(f'About to block {chat["label"]} [room {portal_id}] on WhatsApp via `{command}`')
    if not args.yes:
        print("Dry run — pass --yes to block them. There is no unblock command in this bridge.")
        return
    since_ms = int(time.time() * 1000) - 2000
    txn = f"wamx{int(time.time() * 1000)}"
    _request("PUT", f"/_matrix/client/v3/rooms/{_encode_room(portal_id)}/send/m.room.message/{txn}",
             token=token, body={"msgtype": "m.text", "body": command})
    reply = _await_portal_bot_reply(token, portal_id, since_ms)
    if reply and _REFUSAL_RE.search(reply):
        print(f"NOT BLOCKED: the bridge said: {reply.strip()}")
        sys.exit(UNCONFIRMED_EXIT)
    if reply:
        print(f"Bridge said: {reply.strip()}")
    print("Block command sent. The bridge reported no error; check blocked contacts in the "
          "WhatsApp app to confirm.")


def cmd_unblock(args):
    """The bridge has no unblock command, so this always exits 5 and touches nothing."""
    print("NOT UNBLOCKED: this bridge has no unblock command (checked its `help` on 2026-10-31: "
          "it lists `block-user` and nothing to unblock); unblock in the WhatsApp app.")
    sys.exit(UNSUPPORTED_EXIT)


def _add_gate_flags(sub):
    """Approvals the outbound gate reads (P0). Each waives exactly one check."""
    sub.add_argument("--money-ok", action="store_true",
                     help="The owner saw this exact money/price text and said yes (waives the money lock only)")
    sub.add_argument("--override-check", default=None, metavar="REASON",
                     help="The owner saw the exact text and accepted the draft_check findings "
                          "(waives style/budget only; reason of 4+ chars)")
    sub.add_argument("--resend-ok", action="store_true",
                     help="The owner said to send the same text again (waives the duplicate check only)")


def _run_logged(func, args):
    """Run a READ command; outside a hook runtime (no CLAUDECODE, no CLAUDE_CODE_SESSION_ID:
    Claude Code and pi's Claude-hooks bridge log reads in their hook) and outside comms' own
    scans (no COMMS_INTERNAL), archive what it printed. stdout passes through unchanged."""
    if (os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("COMMS_INTERNAL")):
        return func(args)
    C = _comms_module("commslib")
    if C is None or C.VERBS.get("whatsapp", {}).get(args.command) != "read":
        return func(args)
    tee = C.StdoutTee(sys.stdout)
    sys.stdout, ok = tee, False
    try:
        result = func(args)
        ok = True
        return result
    except SystemExit as e:
        ok = e.code in (None, 0)
        raise
    finally:
        sys.stdout = tee._s
        if ok:
            C.log_event("whatsapp", "read", getattr(args, "identifier", None), body=tee.getvalue(),
                        verb=args.command, command=" ".join(sys.argv))


def main():
    parser = argparse.ArgumentParser(
        description="WhatsApp (mautrix) CLI — read WhatsApp via Matrix; "
                    "all relayed sends require an explicit --yes"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # login
    login_parser = subparsers.add_parser("login", help="Password-login and cache a session")
    login_parser.add_argument("--password", default="",
                              help="Matrix password (or set MATRIX_PASSWORD)")

    # status
    subparsers.add_parser("status", help="Reachability + WhatsApp login state")

    # search
    search_parser = subparsers.add_parser("search", help="Search WhatsApp contacts")
    search_parser.add_argument("query", help="Search text (name or number)")

    # history
    history_parser = subparsers.add_parser("history", help="Read a chat's recent messages")
    history_parser.add_argument(
        "identifier",
        help="DM number/name, or a GROUP room_id (!...) / name / JID (<id>@g.us)")
    history_parser.add_argument("-n", "--number", type=int, default=20,
                                help="Number of messages (default 20)")
    history_parser.add_argument("--no-transcribe", action="store_true",
                                help="Skip inline voice-note transcription (faster; "
                                     "voice notes show as [voice note])")
    # Voice notes now transcribe BY DEFAULT; --transcribe kept as a no-op alias
    # so older invocations don't break.
    history_parser.add_argument("--transcribe", action="store_true",
                                help=argparse.SUPPRESS)
    history_parser.add_argument("--ids", action="store_true",
                                help="Add each message's event id (for `react --event`)")
    history_parser.add_argument(
        "--full", action="store_true",
        help="Lossless read: one msgschema/1 JSON object; -n counts real messages, edits and "
             "deletes folded, reactions, reply parents and media included")
    history_parser.add_argument("--before", default=None, metavar="EVENT_ID",
                                help="With --full: continue older than this event (the `more` hint)")
    history_parser.add_argument("--download", default=None, metavar="DIR",
                                help="With --full: save media into DIR and fill local_path")

    # chats
    chats_parser = subparsers.add_parser("chats", help="List materialized portal rooms")
    chats_parser.add_argument("-n", "--number", type=int, default=20,
                              help="Max portals to list (default 20)")

    # groups (read-only) — lists every group + its newest message
    groups_parser = subparsers.add_parser(
        "groups", help="List GROUP chats with each one's newest message (read-only)")
    groups_parser.add_argument("-n", "--number", type=int, default=None,
                               help="Max groups to list (default: all)")
    groups_parser.add_argument("--no-transcribe", action="store_true",
                               help="Skip inline voice-note transcription in previews "
                                    "(faster; voice notes show as [voice note])")

    # unread
    unread_parser = subparsers.add_parser(
        "unread", help="List rooms with unread messages")
    unread_parser.add_argument("--no-transcribe", action="store_true",
                               help="Skip inline voice-note transcription in the preview "
                                    "(faster; voice notes show as [voice note])")
    unread_parser.add_argument(
        "--full", action="store_true",
        help="Lossless read: every unread chat with its unread messages in full (msgschema/1 JSON)")

    # send (guarded write path #1 — dry-run unless --yes)
    send_parser = subparsers.add_parser(
        "send", help="Send a WhatsApp message (dry-run preview unless --yes)")
    send_parser.add_argument(
        "identifier",
        help="DM number/name, or a GROUP room_id (!...) / name / JID (<id>@g.us)")
    send_parser.add_argument("message", nargs="+", help="Message text")
    send_parser.add_argument(
        "--mention", action="append", default=None, metavar="NAME_OR_ID",
        help="Ping a group member by name or ghost id (repeatable). Resolved "
             "against the target room's own WhatsApp members; refuses to guess "
             "on a zero/ambiguous match. Omit for a plain, unchanged text send.")
    send_parser.add_argument("--yes", action="store_true",
                             help="Actually send (without it, preview only)")
    send_parser.add_argument(
        "--since", type=_parse_since, default=None, metavar="EPOCH",
        help="REQUIRED with --yes. Time of the newest message you read (the preview "
             "prints it; `empty` for a chat with no message). Refuses with exit "
             f"{STALE_EXIT} if anyone wrote after it, the owner included. Bubble 2+ of a "
             "reply: pass the `next --since` the previous send printed.")
    send_parser.add_argument(
        "--group-ok", action="store_true",
        help="Explicitly allow a GROUP send (Guard 2, active only when OUTBOUND_GUARDS=1). "
             "Without it, a multi-recipient audience is refused — the default is a DM.")
    send_parser.add_argument(
        "--exclude", action="append", default=None, metavar="NAME",
        help="Refuse the send if this person is in the resolved audience (repeatable; "
             "Guard 2, active only when OUTBOUND_GUARDS=1).")
    _add_gate_flags(send_parser)

    # react (WRITE: one emoji on one message; dry-run unless --yes)
    react_parser = subparsers.add_parser(
        "react", help="React to one message with an emoji (dry-run preview unless --yes)")
    react_parser.add_argument(
        "identifier",
        help="DM number/name, or a GROUP room_id (!...) / name / JID (<id>@g.us)")
    react_parser.add_argument(
        "emoji", nargs="?", default=None,
        help="ONE emoji, or heart, thumbsup, laugh, pray, fire, wow, sad, check, eyes, party")
    react_parser.add_argument(
        "--event", default=None, metavar="EVENT_ID",
        help="The message to react to. REQUIRED with --yes (the preview prints it; "
             "`history --ids` too). Without it the preview picks their newest message.")
    react_parser.add_argument("--remove", action="store_true",
                              help="Remove the reaction this tool sent (not one from your phone)")
    react_parser.add_argument("--money-ok", action="store_true",
                              help="The owner saw this money/price message and said yes to reacting")
    react_parser.add_argument("--yes", action="store_true",
                              help="Actually react (without it, preview only)")
    react_parser.add_argument("--group-ok", action="store_true", help=argparse.SUPPRESS)
    react_parser.add_argument("--exclude", action="append", default=None, help=argparse.SUPPRESS)

    # download (read-only)
    download_parser = subparsers.add_parser(
        "download", help="Download recent media from a chat (read-only)")
    download_parser.add_argument(
        "identifier",
        help="DM number/name, or a GROUP room_id (!...) / name / JID (<id>@g.us)")
    download_parser.add_argument("-n", "--number", type=int, default=1,
                                 help="Number of recent media items (default 1)")
    download_parser.add_argument("--out", default="./wa_downloads",
                                 help="Output directory (default ./wa_downloads)")

    # send-media (guarded write path #2 — dry-run unless --yes)
    send_media_parser = subparsers.add_parser(
        "send-media", help="Send a media file (dry-run preview unless --yes)")
    send_media_parser.add_argument(
        "identifier",
        help="DM number/name, or a GROUP room_id (!...) / name / JID (<id>@g.us)")
    send_media_parser.add_argument("filepath", help="Local file to send")
    send_media_parser.add_argument("--caption", default=None,
                                   help="Caption/body (default: filename)")
    send_media_parser.add_argument("--yes", action="store_true",
                                   help="Actually send (without it, preview only)")
    send_media_parser.add_argument(
        "--since", type=_parse_since, default=None, metavar="EPOCH",
        help="REQUIRED with --yes. Time of the newest message you read (the preview "
             "prints it; `empty` for a chat with no message). Refuses with exit "
             f"{STALE_EXIT} if anyone wrote after it, the owner included.")
    send_media_parser.add_argument(
        "--group-ok", action="store_true",
        help="Explicitly allow a GROUP media send (Guard 2, active only when OUTBOUND_GUARDS=1).")
    send_media_parser.add_argument(
        "--exclude", action="append", default=None, metavar="NAME",
        help="Refuse if this person is in the resolved audience (repeatable; Guard 2, "
             "active only when OUTBOUND_GUARDS=1).")
    _add_gate_flags(send_media_parser)

    # send-voice prepares exact cached audio and has no send flag/path.
    send_voice_parser = subparsers.add_parser(
        "send-voice",
        help="Generate cached cloned-voice previews; never sends to WhatsApp",
    )
    send_voice_parser.add_argument(
        "identifier",
        help="DM number/name, or a GROUP room_id (!...) / name / JID (<id>@g.us)",
    )
    send_voice_parser.add_argument("message", nargs="+", help="Text to speak")
    send_voice_parser.add_argument(
        "--expressive",
        action="store_true",
        help="Insert Eleven v3 [laughs], [giggles], or [chuckles] in the same cloned voice",
    )

    # send-voice-confirm is the only command that can relay a prepared preview.
    confirm_voice_parser = subparsers.add_parser(
        "send-voice-confirm",
        help="Re-show or send an exact prepared voice preview",
    )
    confirm_voice_parser.add_argument("preview_id", help="Prepared voice preview ID")
    confirm_voice_parser.add_argument(
        "--yes", action="store_true", help="Send exact cached files (preview otherwise)"
    )
    _add_gate_flags(confirm_voice_parser)

    # transcribe (read-only)
    transcribe_parser = subparsers.add_parser(
        "transcribe", help="Transcribe a voice/audio message (read-only)")
    transcribe_parser.add_argument(
        "identifier",
        help="DM number/name, or a GROUP room_id (!...) / name / JID (<id>@g.us)")
    transcribe_parser.add_argument("--message", default="",
                                   help="Event ID of a specific m.audio message")
    transcribe_parser.add_argument("--language", default=None,
                                   help="Language hint for whisper (e.g. en)")
    transcribe_parser.add_argument("--scan", type=int, default=50,
                                   help="Timeline depth to scan for audio (default 50)")

    # sync (portal write — a fixed `!wa sync-portal` BOT COMMAND, not relayed content)
    sync_parser = subparsers.add_parser(
        "sync", help="Refresh a portal's metadata (sends the !wa sync-portal command)")
    sync_parser.add_argument(
        "identifier",
        help="DM number/name, or a GROUP room_id (!...) / name / JID (<id>@g.us)")

    # --- message + group admin (leaf-1.1). Every write: dry run unless --yes.
    ident_help = "DM number/name, or a GROUP room_id (!...) / name / JID (<id>@g.us)"
    reply_parser = subparsers.add_parser(
        "reply", help="Reply to one message (a send; dry-run preview unless --yes)")
    reply_parser.add_argument("identifier", help=ident_help)
    reply_parser.add_argument("event", metavar="EVENT_ID", help="The message to reply to (`history --ids`)")
    reply_parser.add_argument("message", nargs="+", help="Reply text")
    reply_parser.add_argument("--yes", action="store_true", help="Actually send (without it, preview only)")
    reply_parser.add_argument(
        "--since", type=_parse_since, default=None, metavar="EPOCH",
        help="REQUIRED with --yes. Same guard as `send`: refuses with exit "
             f"{STALE_EXIT} if anyone wrote after it.")
    reply_parser.add_argument("--group-ok", action="store_true", help=argparse.SUPPRESS)
    reply_parser.add_argument("--exclude", action="append", default=None, help=argparse.SUPPRESS)
    _add_gate_flags(reply_parser)

    edit_parser = subparsers.add_parser(
        "edit", help="Edit one of YOUR OWN messages (dry-run preview unless --yes)")
    edit_parser.add_argument("identifier", help=ident_help)
    edit_parser.add_argument("event", metavar="EVENT_ID", help="Your message to edit (`history --ids`)")
    edit_parser.add_argument("message", nargs="+", help="The new text")
    edit_parser.add_argument("--yes", action="store_true", help="Actually edit (without it, preview only)")
    edit_parser.add_argument(
        "--since", type=_parse_since, default=None, metavar="EPOCH",
        help=f"REQUIRED with --yes, same guard as `send` (exit {STALE_EXIT} if the chat moved).")
    edit_parser.add_argument("--group-ok", action="store_true", help=argparse.SUPPRESS)
    edit_parser.add_argument("--exclude", action="append", default=None, help=argparse.SUPPRESS)
    _add_gate_flags(edit_parser)

    delete_parser = subparsers.add_parser(
        "delete", help="Delete one of YOUR OWN messages (dry-run preview unless --yes)")
    delete_parser.add_argument("identifier", help=ident_help)
    delete_parser.add_argument("event", metavar="EVENT_ID", help="Your message to delete (`history --ids`)")
    delete_parser.add_argument("--money-ok", action="store_true",
                               help="The owner saw this money/price message and said yes to deleting it")
    delete_parser.add_argument("--yes", action="store_true", help="Actually delete (without it, preview only)")

    mark_parser = subparsers.add_parser(
        "mark-read", help="Send a read receipt (dry-run preview unless --yes)")
    mark_parser.add_argument("identifier", help=ident_help)
    mark_parser.add_argument("--event", default=None, metavar="EVENT_ID",
                             help="Read up to this message (default: the newest)")
    mark_parser.add_argument("--yes", action="store_true", help="Actually send the receipt")

    find_parser = subparsers.add_parser(
        "find", help="Search message text (read-only; server search matches whole words)")
    find_parser.add_argument("text", help="Text to find")
    find_parser.add_argument("--chat", default=None, metavar="ID",
                             help="Only this chat (also enables a substring scan if the server finds none)")
    find_parser.add_argument("-n", "--number", type=int, default=10, help="Max results (default 10)")

    gcreate_parser = subparsers.add_parser(
        "group-create", help="Create a WhatsApp group (dry-run preview unless --yes)")
    gcreate_parser.add_argument("name", help="Group name")
    gcreate_parser.add_argument("--member", nargs="+", action="extend", default=[], metavar="DIGITS",
                                help="Member phone numbers in digits (at least one)")
    gcreate_parser.add_argument("--yes", action="store_true", help="Actually create it")

    gadd_parser = subparsers.add_parser(
        "group-add", help="Add a person to a group (dry-run preview unless --yes)")
    gadd_parser.add_argument("group", help=ident_help)
    gadd_parser.add_argument("digits", metavar="DIGITS", help="Phone number in digits")
    gadd_parser.add_argument("--yes", action="store_true", help="Actually add them")

    gremove_parser = subparsers.add_parser(
        "group-remove", help="Remove a person from a group (dry-run preview unless --yes)")
    gremove_parser.add_argument("group", help=ident_help)
    gremove_parser.add_argument("digits", metavar="DIGITS", help="Phone number in digits")
    gremove_parser.add_argument("--yes", action="store_true", help="Actually remove them")

    grename_parser = subparsers.add_parser(
        "group-rename", help="Rename a group (dry-run preview unless --yes)")
    grename_parser.add_argument("group", help=ident_help)
    grename_parser.add_argument("name", help="The new group name")
    grename_parser.add_argument("--yes", action="store_true", help="Actually rename it")

    gleave_parser = subparsers.add_parser(
        "group-leave", help="Leave a group (dry-run preview unless --yes)")
    gleave_parser.add_argument("group", help=ident_help)
    gleave_parser.add_argument("--yes", action="store_true", help="Actually leave")

    gmembers_parser = subparsers.add_parser(
        "group-members", help="List a group's members (read-only)")
    gmembers_parser.add_argument("group", help=ident_help)

    block_parser = subparsers.add_parser(
        "block", help="Block a contact via the bridge (dry-run preview unless --yes)")
    block_parser.add_argument("digits", metavar="DIGITS", help="Phone number in digits")
    block_parser.add_argument("--report-spam", action="store_true", help="Also report them as spam")
    block_parser.add_argument("--yes", action="store_true", help="Actually block them")

    unblock_parser = subparsers.add_parser(
        "unblock", help="Unblock a contact (this bridge cannot: exits 5)")
    unblock_parser.add_argument("digits", metavar="DIGITS", help="Phone number in digits")
    unblock_parser.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "login": cmd_login,
        "status": cmd_status,
        "search": cmd_search,
        "history": cmd_history,
        "chats": cmd_chats,
        "groups": cmd_groups,
        "unread": cmd_unread,
        "send": cmd_send,
        "react": cmd_react,
        "download": cmd_download,
        "send-media": cmd_send_media,
        "send-voice": cmd_send_voice,
        "send-voice-confirm": cmd_send_voice_confirm,
        "transcribe": cmd_transcribe,
        "sync": cmd_sync,
        "reply": cmd_reply,
        "edit": cmd_edit,
        "delete": cmd_delete,
        "mark-read": cmd_mark_read,
        "find": cmd_find,
        "group-create": cmd_group_create,
        "group-add": cmd_group_add,
        "group-remove": cmd_group_remove,
        "group-rename": cmd_group_rename,
        "group-leave": cmd_group_leave,
        "group-members": cmd_group_members,
        "block": cmd_block,
        "unblock": cmd_unblock,
    }

    try:
        _run_logged(commands[args.command], args)
    except ConnectionError as e:
        print(e)
        sys.exit(1)
    except MatrixError as e:
        print(f"Matrix API error: {e}")
        sys.exit(1)
    except VoiceConfigError as e:
        print(f"Voice memo error: {e}")
        sys.exit(2)
    except VoiceMemoError as e:
        print(f"Voice memo error: {e}")
        sys.exit(1)
    except kitconfig.ConfigError as e:
        print(e)
        sys.exit(2)


if __name__ == "__main__":
    main()
