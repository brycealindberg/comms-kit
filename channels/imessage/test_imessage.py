from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import pathlib
import signal
import stat
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


# Keep the shared archive and logs of a real install untouched: every test runs against an
# empty data dir. The kit config is read from this file, never from the owner's own.
import atexit
import shutil

ISOLATED_DATA = tempfile.mkdtemp(prefix="imessage-kit-test-")
atexit.register(shutil.rmtree, ISOLATED_DATA, ignore_errors=True)
ISOLATED_CONFIG = Path(ISOLATED_DATA) / "config.json"
ISOLATED_CONFIG.write_text(json.dumps({"data_dir": ISOLATED_DATA}))
os.environ["COMMS_KIT_CONFIG"] = str(ISOLATED_CONFIG)

HELPER = Path(__file__).resolve().parent / "imessage.py"
SPEC = importlib.util.spec_from_file_location("imessage_kit", HELPER)
assert SPEC and SPEC.loader
imessage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(imessage)

FAKE_IMSG = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["FAKE_IMSG_STATE"])
mode = os.environ.get("FAKE_IMSG_MODE", "")
argv_log = os.environ.get("FAKE_IMSG_ARGV")
if argv_log:
    with open(argv_log, "a") as handle:
        handle.write(json.dumps(args) + "\n")

chats = [
    {"id": 1, "name": "Jane", "display_name": "", "contact_name": "Jane", "identifier": "+12065550101", "guid": "iMessage;-;+12065550101", "service": "iMessage", "is_group": False, "participants": ["+12065550101"]},
    {"id": 2, "name": "Jane", "display_name": "", "contact_name": "Jane", "identifier": "jane@example.com", "guid": "iMessage;-;jane@example.com", "service": "iMessage", "is_group": False, "participants": ["jane@example.com"]},
    {"id": 3, "name": "Test Group", "display_name": "Test Group", "contact_name": "", "identifier": "chat-group", "guid": "iMessage;+;chat-group", "service": "iMessage", "is_group": True, "participants": ["+12065550101", "+12065550102"]},
    {"id": 4, "name": "SMS Person", "display_name": "", "contact_name": "SMS Person", "identifier": "+12065550104", "guid": "SMS;-;+12065550104", "service": "SMS", "is_group": False, "participants": ["+12065550104"]},
    {"id": 5, "name": "Alex Doe", "display_name": "", "contact_name": "Alex Doe", "identifier": "+12065550105", "guid": "iMessage;-;+12065550105", "service": "iMessage", "is_group": False, "participants": ["+12065550105"]},
    {"id": 6, "name": "Misclassified", "display_name": "", "contact_name": "", "identifier": "bad-direct", "guid": "iMessage;-;bad-direct", "service": "iMessage", "is_group": False, "participants": ["+12065550101", "+12065550102"]},
    {"id": 7, "name": "RCS Person", "display_name": "", "contact_name": "RCS Person", "identifier": "+12065550107", "guid": "RCS;-;+12065550107", "service": "RCS", "is_group": False, "participants": ["+12065550107"]},
]

if state.exists():
    # A send with `to` CREATES a chat. Rebuilt from the same state file on every
    # invocation, so the new chat is still there on the next call the way a real
    # one would be.
    for entry in json.loads(state.read_text()).get("sends", []):
        handle = entry["request"]["params"].get("to")
        if handle and not any(chat["participants"] == [handle] for chat in chats):
            chats.insert(0, {"id": 76, "name": "", "display_name": "", "contact_name": "", "identifier": handle, "guid": f"iMessage;-;{handle}", "service": "iMessage", "is_group": False, "participants": [handle]})

def emit(value):
    print(json.dumps(value, separators=(",", ":")))

def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

if args == ["--version"]:
    print("0.14.1")
elif args and args[0] == "chats":
    limit = int(args[args.index("--limit") + 1])
    listed = chats
    if "--unread-only" in args:
        listed = [dict(chat, unread_count=2) for chat in chats if chat["id"] in (1, 3)]
    for chat in listed[:limit]:
        emit(chat)
elif args and args[0] == "group":
    chat_id = int(args[args.index("--chat-id") + 1])
    emit(next(chat for chat in chats if chat["id"] == chat_id))
elif args and args[0] == "history":
    chat_id = int(args[args.index("--chat-id") + 1])
    limit = int(args[args.index("--limit") + 1])
    # Match pinned imsg: newest-first, with more than five incoming rows.
    rows = [
        {"id": row_id, "chat_id": chat_id, "guid": f"incoming-{row_id}", "is_from_me": False, "sender": "+12065550105", "sender_name": "Alex Doe", "text": f"Earlier message {row_id}", "created_at": now()}
        for row_id in range(108, 99, -1)
    ]
    if state.exists():
        payload = json.loads(state.read_text())
        def outgoing(entry, row_id):
            params = entry["request"]["params"]
            text = params.get("text")
            attachments = []
            if params.get("file"):
                # Measured on the real database: an attachment row carries one
                # U+FFFC per file in `text`, with any caption as a suffix.
                attachments = [{"transfer_name": params["file"].rsplit("/", 1)[-1]}]
                text = "\ufffc" + (text or "")
            return {"id": row_id, "chat_id": chat_id, "guid": f"outgoing-{row_id}", "is_from_me": True, "sender": None, "sender_name": None, "text": text, "attachments": attachments, "created_at": entry["created_at"]}
        # One outgoing row per recorded send, newest first, so a multi-part
        # send is verifiable part by part the way the real database is.
        for entry in payload["sends"]:
            if mode != "lost-row":  # "lost-row": the send is accepted but never lands in the chat
                rows.insert(0, outgoing(entry, entry["row_id"]))
        if mode == "duplicate":
            last = payload["sends"][-1]
            rows.insert(0, outgoing(last, last["row_id"] + 1))
    # Filters must actually remove rows, or a test cannot tell a working flag
    # from one the wrapper builds and never passes on.
    if "--start" in args and args[args.index("--start") + 1] > "2030":
        rows = []
    if "--end" in args and args[args.index("--end") + 1] < "2020":
        rows = []
    if "--participants" in args:
        wanted = set(args[args.index("--participants") + 1].split(","))
        rows = [row for row in rows if row.get("sender") in wanted]
    for row in rows[:limit]:
        emit(row)
elif args and args[0] == "status":
    emit({"sip": "enabled", "advanced_features": False, "bridge_version": 0, "version": "0.14.1"})
elif args and args[0] == "whois":
    emit({"source": "local", "known": True, "address": args[args.index("--address") + 1], "service": "imessage"})
elif args and args[0] == "stats":
    emit({
        "total_messages": 90, "sent_messages": 40, "received_messages": 50,
        "time_zone": "America/New_York",
        "chats": [{"chat": f"c{i}", "message_count": i} for i in range(30)],
        "senders": [{"sender": f"s{i}", "message_count": i} for i in range(30)],
        "services": [{"service": "iMessage", "message_count": 90}],
        "dates": [{"date": f"2026-01-{day:02d}", "message_count": day} for day in range(1, 21)],
    })
elif args == ["rpc"]:
    request = json.loads(sys.stdin.readline())
    if request.get("method") == "messages.search":
        # Deliberately spans an eligible direct chat, an eligible group, and
        # the SMS chat the read interface refuses -- the filter's control.
        emit({"jsonrpc": "2.0", "id": request["id"], "result": {"messages": [
            {"id": 900, "chat_id": 1, "text": "hit in a direct iMessage chat", "is_from_me": False},
            {"id": 901, "chat_id": 3, "text": "hit in an iMessage group", "is_from_me": False},
            {"id": 902, "chat_id": 4, "text": "hit in an SMS chat", "is_from_me": False},
            {"id": 903, "chat_id": 6, "text": "hit in a malformed direct chat", "is_from_me": False},
        ]}})
        raise SystemExit(0)
    if mode == "rpc-timeout":
        time.sleep(30)
    if mode == "rpc-malformed":
        print("not-json")
    elif mode == "rpc-not-started":
        emit({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32001, "message": "redacted", "data": {"disposition": "not_started", "retry_safe": True}}})
    else:
        sends = json.loads(state.read_text())["sends"] if state.exists() else []
        # Ids step by 2 so "duplicate" mode always has a free id above the last.
        row_id = 200 + 2 * len(sends)
        sends.append({"request": request, "argv": args, "row_id": row_id, "created_at": now()})
        state.write_text(json.dumps({"sends": sends, "count": len(sends), "request": request, "argv": args, "created_at": now()}))
        result = {"ok": True}
        # Measured live 2026-09-09: a TEXT send's reply carries id and guid, an
        # ATTACHMENT send's reply carries neither. A fake that always returns
        # both would let every attachment test take the easy id-matched path and
        # never exercise the fallback the real code actually runs.
        if mode != "no-rpc-ids" and not request["params"].get("file"):
            result.update({"id": row_id, "guid": f"outgoing-{row_id}"})
        emit({"jsonrpc": "2.0", "id": request["id"], "result": result})
else:
    print("unsupported fake imsg arguments", args, file=sys.stderr)
    raise SystemExit(2)
'''


def send_args(*argv: str):
    """Build `send` arguments through the REAL parser.

    A hand-built SimpleNamespace drifts silently the moment the dispatcher
    reads a flag the fixture does not set. That is exactly how every one of
    these tests broke the day `send` gained --service and --region: the
    production code was correct and the fixtures were lying.
    """
    return imessage.build_parser().parse_args(["send", *argv])


class ImessageKitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fake = self.root / "fake-imsg"
        self.fake.write_text(FAKE_IMSG)
        self.fake.chmod(0o755)
        self.fake_hash = hashlib.sha256(self.fake.read_bytes()).hexdigest()
        self.fake_state = self.root / "fake-send-state.json"
        self.argv_log = self.root / "fake-argv.log"
        self.preview_state = self.root / "state"
        self.patches = [
            mock.patch.object(imessage, "upstream_binary", return_value=self.fake),
            mock.patch.object(imessage, "state_root", return_value=self.preview_state),
            mock.patch.object(imessage, "UPSTREAM_BINARY_SHA256", self.fake_hash),
            mock.patch.dict(
                os.environ,
                {"FAKE_IMSG_STATE": str(self.fake_state), "FAKE_IMSG_ARGV": str(self.argv_log)},
                clear=False,
            ),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    def prepare(self, *texts: str) -> dict:
        return imessage.create_preview(5, list(texts) or ["Safe preview"])

    def parsed(self, *argv: str):
        """Build args through the REAL parser.

        A hand-built SimpleNamespace drifts silently the moment the parser
        gains a flag the dispatcher reads, which is exactly how a new command
        ships broken while the suite stays green.
        """
        return imessage.build_parser().parse_args(list(argv))

    def upstream_calls(self) -> list[list[str]]:
        if not self.argv_log.exists():
            return []
        return [json.loads(line) for line in self.argv_log.read_text().splitlines() if line.strip()]

    def preview_file(self, preview: dict, status: str = "pending") -> Path:
        return self.preview_state / "previews" / status / f"{preview['preview_id']}.json"

    def native_process(self, stdout: str, returncode: int = 0) -> mock.Mock:
        process = mock.Mock(pid=43210, returncode=returncode)
        process.communicate.return_value = (stdout, "")
        process.poll.return_value = returncode
        return process

    def test_doctor_says_no_on_a_system_that_is_not_macos(self) -> None:
        with mock.patch.object(imessage.platform, "system", return_value="Linux"):
            result, ok = imessage.doctor()
        self.assertFalse(ok)
        self.assertIn("iMessage sending requires macOS", result["errors"])

    def test_doctor_is_read_only(self) -> None:
        with mock.patch.object(imessage.platform, "system", return_value="Darwin"):
            result, ok = imessage.doctor()
        self.assertTrue(ok)
        self.assertTrue(result["database_readable"])
        self.assertFalse(self.fake_state.exists())

    def test_exact_resolution_reads_sms_and_fails_on_ambiguity_and_malformed_direct(self) -> None:
        self.assertEqual(imessage.resolve_exact_chat("5", 20)["id"], 5)
        with self.assertRaisesRegex(imessage.ImsgError, "Ambiguous"):
            imessage.resolve_exact_chat("Jane", 20)
        # Owner decision: SMS chats are readable (read only).
        self.assertEqual(imessage.resolve_exact_chat("4", 20)["id"], 4)
        with self.assertRaisesRegex(imessage.ImsgError, "exactly one"):
            imessage.resolve_exact_chat("6", 20)

    def test_group_resolution_and_bounded_history_are_read_only(self) -> None:
        for query in ("3", "Test Group", "iMessage;+;chat-group"):
            with self.subTest(query=query):
                self.assertTrue(imessage.resolve_exact_chat(query, 20)["is_group"])
        with self.assertRaisesRegex(imessage.ImsgError, "Ambiguous"):
            imessage.resolve_exact_chat("+12065550101", 20)
        args = self.parsed("history", "--chat-id", "3", "--limit", "2", "--attachments")
        result, ok = imessage.handle(args)
        self.assertTrue(ok)
        self.assertEqual(result["chat"]["id"], 3)
        self.assertTrue(result["chat"]["is_group"])
        self.assertEqual(len(result["messages"]), 2)
        self.assertTrue(all(row["chat_id"] == 3 for row in result["messages"]))
        self.assertFalse(self.fake_state.exists())
        self.assertFalse(self.preview_state.exists())

    def test_direct_sms_and_rcs_chats_read_but_unknown_services_and_the_dialog_path_do_not(self) -> None:
        """Owner decision: let the tool read SMS chats, read only. The chat list has a
        client's chat as `any;-;+1...` with service SMS; it is the same one-to-one shape."""
        sms = {"id": 41, "identifier": "+12065550104", "guid": "any;-;+12065550104",
               "service": "SMS", "is_group": False, "participants": ["+12065550104"]}
        self.assertEqual(imessage.classify_readable_chat(sms)["id"], 41)
        self.assertEqual(imessage.classify_readable_chat(dict(sms, service="RCS"))["id"], 41)
        for bad, why in ((dict(sms, service="unknown"), "unknown"),
                         (dict(sms, service=""), "unknown"),
                         (dict(sms, participants=["+12065550104", "+12065550105"]), "exactly one"),
                         (dict(sms, guid="any;+;+12065550104"), "GUID"),
                         (dict(sms, is_group=None), "Group")):
            with self.subTest(why=why), self.assertRaisesRegex(imessage.ImsgError, why):
                imessage.classify_readable_chat(bad)
        # The dialog send path keeps its iMessage-only rule.
        with self.assertRaisesRegex(imessage.ImsgError, "SMS"):
            imessage.require_direct_imessage(sms)

    def test_malformed_and_non_imessage_groups_cannot_be_read(self) -> None:
        group = imessage.chat_detail(3)
        cases = [
            dict(group, service="SMS"), dict(group, service="unknown"),
            dict(group, participants=[]), dict(group, participants=[""]),
            dict(group, participants=[None]), dict(group, participants="not-a-list"),
            dict(group, identifier=""), dict(group, identifier="different"),
            dict(group, guid="iMessage;-;chat-group"), dict(group, guid=None),
            dict(group, guid="SMS;+;chat-group"), dict(group, guid=";+;chat-group"),
            dict(group, guid="RCS;+;chat-group"),
            dict(group, is_group=None), dict(group, is_group=False),
        ]
        with mock.patch.object(imessage, "history_rows") as history:
            for detail in cases:
                with self.subTest(detail=detail), mock.patch.object(imessage, "chat_detail", return_value=detail):
                    args = self.parsed("history", "--chat-id", "3", "--limit", "2")
                    with self.assertRaises(imessage.ImsgError):
                        imessage.handle(args)
            history.assert_not_called()
        with mock.patch.object(imessage, "chat_detail", return_value=dict(group, guid="any;+;chat-group")):
            self.assertEqual(imessage.readable_chat(3)["id"], 3)
        self.assertFalse(self.fake_state.exists())

    def test_the_dialog_path_still_refuses_a_group_and_a_swapped_chat(self) -> None:
        """Groups opened up for headless sends on 2026-09-09. The dialog did not.

        A typed phrase is a poor place to notice you are about to text five
        people, so create_preview keeps the narrow gate unless the caller says
        headless=True. Separately, swapping the chat under an already prepared
        preview is refused at commit whichever path made it.
        """
        group = imessage.resolve_exact_chat("3", 20)
        with mock.patch.object(imessage, "history_rows") as history:
            with self.assertRaisesRegex(imessage.ImsgError, "Group chats are blocked"):
                imessage.create_preview(3, ["Never send to a group"])
            history.assert_not_called()
        preview = self.prepare()
        with mock.patch.object(imessage, "chat_detail", return_value=group):
            with self.assertRaisesRegex(imessage.ImsgError, "routing changed"):
                imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        self.assertTrue(self.preview_file(preview, "invalid").is_file())
        self.assertFalse(self.fake_state.exists())

    def test_preview_context_uses_newest_five_rows_from_descending_history(self) -> None:
        rows = imessage.history_rows(5, 50)
        self.assertGreater(len(rows), 5)
        self.assertEqual([row["id"] for row in rows], list(range(108, 99, -1)))
        preview = self.prepare()
        self.assertEqual([row["id"] for row in preview["recent_context"]], [108, 107, 106, 105, 104])
        self.assertEqual(preview["recent_context"][0]["text"], "Earlier message 108")
        self.assertEqual(imessage.recent_context(rows, 0), [])
        self.assertFalse(self.fake_state.exists())

    def test_prepare_is_private_content_bound_and_unsent(self) -> None:
        preview = self.prepare("Nothing should send")
        self.assertTrue(preview["nothing_sent"])
        path = self.preview_file(preview)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(preview["approval_phrase"], f"SEND IMESSAGE {preview['preview_id']}")
        self.assertFalse(self.fake_state.exists())

    def drafts_dir(self) -> pathlib.Path:
        directory = self.preview_state / "drafts"
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        return directory

    def test_prepare_text_file_must_be_private(self) -> None:
        draft = self.drafts_dir() / "draft.txt"
        draft.write_text("Exact private draft")
        draft.chmod(0o644)
        with self.assertRaisesRegex(imessage.ImsgError, "permissions are too broad"):
            imessage.read_private_text(draft)
        draft.chmod(0o600)
        self.assertEqual(imessage.read_private_text(draft), "Exact private draft")

    def test_a_private_file_outside_the_drafts_directory_is_not_a_draft(self) -> None:
        """0600 and user-owned describes an SSH key as well as it describes a draft.

        Both fit under the 4,000-character cap and nothing downstream shows the
        bytes to a human, so the path is the only place this can be caught.
        """
        for name in ("id_ed25519", ".env", "draft.txt"):
            with self.subTest(name=name):
                intruder = self.root / name
                intruder.write_text("SUPER SECRET KEY MATERIAL")
                intruder.chmod(0o600)
                with self.assertRaisesRegex(imessage.ImsgError, "must live directly in"):
                    imessage.read_private_text(intruder)

    def test_a_draft_cannot_escape_the_directory_with_dot_dot(self) -> None:
        intruder = self.root / "escaped.txt"
        intruder.write_text("outside")
        intruder.chmod(0o600)
        sneaky = self.drafts_dir() / ".." / "escaped.txt"
        with self.assertRaisesRegex(imessage.ImsgError, "must live directly in"):
            imessage.read_private_text(sneaky)

    def test_wrong_approval_and_tampering_cannot_send(self) -> None:
        preview = self.prepare()
        with self.assertRaisesRegex(imessage.ImsgError, "Approval must exactly match"):
            imessage.commit_preview(preview["preview_id"], "SEND IMESSAGE wrong")
        document = json.loads(self.preview_file(preview).read_text())
        document["payload"]["texts"] = ["tampered"]
        self.preview_file(preview).write_text(json.dumps(document))
        self.preview_file(preview).chmod(0o600)
        with self.assertRaisesRegex(imessage.ImsgError, "integrity check failed"):
            imessage.show_preview(preview["preview_id"])
        self.assertFalse(self.fake_state.exists())

    def test_gui_approval_uses_exact_phrase(self) -> None:
        expected = "SEND IMESSAGE 0123456789abcdef01234567"
        process = self.native_process(json.dumps({"status": "submitted", "text": expected}) + "\n")
        with mock.patch.object(imessage.platform, "system", return_value="Darwin"), mock.patch.object(
            imessage.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(imessage.os, "killpg") as kill:
            self.assertEqual(imessage.prompt_for_approval("0123456789abcdef01234567"), expected)
        command = popen.call_args.args[0]
        self.assertEqual(command[:4], ["/usr/bin/osascript", "-l", "JavaScript", "-e"])
        self.assertEqual(command[5], expected)
        self.assertEqual(command[-1], str(imessage.APPROVAL_DIALOG_SECONDS))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        process.communicate.assert_called_once_with(timeout=imessage.APPROVAL_DIALOG_SECONDS + 10)
        process.stdout.close.assert_called_once_with()
        process.stderr.close.assert_called_once_with()
        kill.assert_not_called()
        script = command[4]
        self.assertIn("NSApplicationActivationPolicyRegular", script)
        self.assertLess(script.index("app.activate;"), script.index("Number(alert.runModal)"))
        self.assertIn("window.makeKeyAndOrderFront($())", script)
        self.assertIn("window.makeFirstResponder(field)", script)
        self.assertIn("field.stringValue = ''", script)
        self.assertEqual(script.count("field.stringValue ="), 1)
        self.assertIn("addTimerForMode(timer, $.NSModalPanelRunLoopMode)", script)
        self.assertIn("timedOut = true", script)
        self.assertIn("app.stopModalWithCode(-1002)", script)
        self.assertIn("Number($.NSProcessInfo.processInfo.systemUptime) >= deadline", script)
        self.assertLess(script.index("if (timedOut"), script.index("field.validateEditing"))
        self.assertLess(script.index("response !== Number($.NSAlertFirstButtonReturn)"), script.index("field.validateEditing"))
        self.assertLess(script.index("field.validateEditing"), script.index("ObjC.unwrap(field.stringValue)"))
        self.assertIn("JSON.stringify({status: 'submitted', text: text})", script)
        self.assertIn("timer.invalidate;", script)
        self.assertIn("window.orderOut($())", script)
        for forbidden in ("Application(", "includeStandardAdditions", "displayDialog", ".trim(", "runModal()"):
            self.assertNotIn(forbidden, script)
        # The four mechanisms that make the window REACHABLE. Measured 2026-09-09:
        # this process cannot win cooperative activation (macOS 14+), so all three
        # activation calls return false and the window lands at ordinary level, on
        # one Space, on the menu-bar display, and never key. It renders and is
        # untypeable -- which is why two sessions read it as "no dialog appeared".
        # Nothing else in this suite can see that: a dialog that is drawn but
        # unreachable satisfies every other assertion here.
        for mechanism in (
            "$.NSWindowStyleMaskNonactivatingPanel",   # may become key while inactive
            "window.level = $.NSStatusWindowLevel",    # above ordinary windows
            "$.NSWindowCollectionBehaviorCanJoinAllSpaces",
            "$.NSWindowCollectionBehaviorFullScreenAuxiliary",
            "$.NSEvent.mouseLocation",                 # the pointer's screen, not main
            "window.orderFrontRegardless",             # ordering must not need activation
        ):
            self.assertIn(mechanism, script)
        # `target` already holds the NSTimer's target in this same function scope,
        # and JavaScript `var` is function-scoped -- reusing the name for the
        # screen lookup would quietly overwrite it.
        self.assertEqual(script.count("var target ="), 1)
        self.assertIn("var pointerScreen =", script)

    def test_the_two_dialog_timeouts_do_not_share_one_sentence(self) -> None:
        """A no-output child and an unanswered window are opposite diagnoses.

        They raised byte-identical text, so the error could not say which had
        happened, and three sessions guessed. Assert they differ AND that each
        still says nothing was sent -- the safety half of the sentence.
        """
        module_path = imessage.__file__
        assert module_path is not None
        source = pathlib.Path(module_path).read_text(encoding="utf-8")
        subprocess_timeout = "Native dialog timed out; nothing was sent. Do not retry automatically."
        unanswered = "Native dialog was shown but no phrase was typed in time; nothing was sent."
        self.assertNotEqual(subprocess_timeout, unanswered)
        # One occurrence each: the shared string coming back is the regression.
        self.assertEqual(source.count(subprocess_timeout), 1)
        self.assertEqual(source.count(unanswered), 1)
        for sentence in (subprocess_timeout, unanswered):
            self.assertIn("nothing was sent", sentence)

    def test_native_failures_never_reach_commit(self) -> None:
        preview = self.prepare()
        args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
        failures = [
            subprocess.CompletedProcess([], 1, stdout="", stderr="Host unavailable"),
            subprocess.CompletedProcess([], 0, stdout='{"status":"cancelled"}\n', stderr=""),
            subprocess.CompletedProcess([], 0, stdout='{"status":"timed_out"}\n', stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="invalid\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout='{"status":"submitted"}', stderr=""),
            subprocess.CompletedProcess([], 0, stdout='{"status":"submitted","text":null}', stderr=""),
            subprocess.CompletedProcess([], 0, stdout='{"status":"submitted","text":42}', stderr=""),
            subprocess.CompletedProcess([], 0, stdout='{"status":"submitted","text":{}}', stderr=""),
            subprocess.CompletedProcess([], 0, stdout="null", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="[]", stderr=""),
        ]
        for status in ("timed_out", "cancelled", "unknown"):
            failures.append(subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({"status": status, "text": preview["approval_phrase"]}), stderr=""
            ))
        phrase_json = json.dumps(preview["approval_phrase"])
        for raw in (
            '{"status":"cancelled","status":"submitted","text":' + phrase_json + '}',
            '{"status":"submitted","status":"cancelled","text":' + phrase_json + '}',
            '{"status":"submitted","text":"wrong","text":' + phrase_json + '}',
            '{"status":"submitted","text":' + phrase_json + ',"extra":true}',
        ):
            failures.append(subprocess.CompletedProcess([], 0, stdout=raw, stderr=""))
        with mock.patch.object(imessage.platform, "system", return_value="Darwin"):
            for failure in failures:
                with self.subTest(failure=repr(failure)):
                    process = self.native_process(failure.stdout, failure.returncode)
                    with mock.patch.object(
                        imessage.subprocess, "Popen", return_value=process
                    ), mock.patch.object(imessage, "commit_preview") as commit:
                        with self.assertRaises(imessage.ImsgError):
                            imessage.handle(args)
                        commit.assert_not_called()
                    self.assertTrue(self.preview_file(preview).is_file())
        self.assertFalse(self.fake_state.exists())

    def test_dialog_process_timeout_interrupt_and_io_failure_cleanup_before_commit(self) -> None:
        preview = self.prepare()
        args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
        previous_handler = signal.getsignal(signal.SIGTERM)
        failures = [subprocess.TimeoutExpired(["osascript"], 310), KeyboardInterrupt(), OSError("IO failed")]
        for failure in failures:
            process = self.native_process("")
            process.poll.return_value = None
            process.communicate.side_effect = failure
            with self.subTest(failure=repr(failure)), mock.patch.object(
                imessage.platform, "system", return_value="Darwin"
            ), mock.patch.object(imessage.subprocess, "Popen", return_value=process), mock.patch.object(
                imessage.os, "killpg"
            ) as kill, mock.patch.object(imessage, "commit_preview") as commit:
                with self.assertRaises((imessage.ImsgError, KeyboardInterrupt)):
                    imessage.handle(args)
                commit.assert_not_called()
                kill.assert_called_once_with(process.pid, signal.SIGTERM)
            process.wait.assert_called_once_with(timeout=2)
            process.stdout.close.assert_called_once_with()
            process.stderr.close.assert_called_once_with()
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handler)
            self.assertTrue(self.preview_file(preview).is_file())
        self.assertFalse(self.fake_state.exists())

    def test_dialog_cleanup_escalates_only_its_owned_group_and_is_bounded(self) -> None:
        for reaped in (True, False):
            process = self.native_process("")
            process.poll.return_value = None
            process.communicate.side_effect = subprocess.TimeoutExpired(["osascript"], 70)
            timeout = subprocess.TimeoutExpired(["osascript"], 2)
            process.wait.side_effect = [timeout, 0 if reaped else timeout]
            with self.subTest(reaped=reaped), mock.patch.object(
                imessage.subprocess, "Popen", return_value=process
            ), mock.patch.object(imessage.os, "killpg") as kill:
                with self.assertRaises(imessage.ImsgError):
                    imessage.run_dialog_process(["osascript"], timeout=70)
                self.assertEqual(kill.call_args_list, [
                    mock.call(process.pid, signal.SIGTERM), mock.call(process.pid, signal.SIGKILL)
                ])
            self.assertEqual(process.wait.call_args_list, [mock.call(timeout=2), mock.call(timeout=2)])
            process.stdout.close.assert_called_once_with()
            process.stderr.close.assert_called_once_with()
        self.assertFalse(self.preview_state.exists())
        self.assertFalse(self.fake_state.exists())

    def test_dialog_sigterm_cleans_up_and_restores_handler_without_commit(self) -> None:
        preview = self.prepare()
        args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
        previous_handler = signal.getsignal(signal.SIGTERM)
        process = self.native_process("")
        process.poll.return_value = None

        def interrupted(**kwargs):
            # Invoke only the helper's handler, not a real OS signal.
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        process.communicate.side_effect = interrupted
        with mock.patch.object(
            imessage.platform, "system", return_value="Darwin"
        ), mock.patch.object(imessage.subprocess, "Popen", return_value=process), mock.patch.object(
            imessage.os, "killpg"
        ) as kill, mock.patch.object(imessage, "commit_preview") as commit:
            with self.assertRaisesRegex(imessage.ImsgError, "interrupted"):
                imessage.handle(args)
            commit.assert_not_called()
            kill.assert_called_once_with(process.pid, signal.SIGTERM)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handler)
        self.assertTrue(self.preview_file(preview).is_file())
        self.assertFalse(self.fake_state.exists())

    def test_dialog_cancellation_during_startup_waits_for_ownership_then_cleans_up(self) -> None:
        preview = self.prepare()
        args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        for sig in previous:
            process = self.native_process("")
            process.poll.return_value = None

            def starting(*args, **kwargs):
                signal.getsignal(sig)(sig, None)
                return process

            with self.subTest(signal=sig), mock.patch.object(
                imessage.platform, "system", return_value="Darwin"
            ), mock.patch.object(imessage.subprocess, "Popen", side_effect=starting), mock.patch.object(
                imessage.os, "killpg"
            ) as kill, mock.patch.object(imessage, "commit_preview") as commit:
                with self.assertRaisesRegex(imessage.ImsgError, "interrupted"):
                    imessage.handle(args)
                process.communicate.assert_not_called()
                process.wait.assert_called_once_with(timeout=2)
                kill.assert_called_once_with(process.pid, signal.SIGTERM)
                commit.assert_not_called()
            self.assertEqual({sig: signal.getsignal(sig) for sig in previous}, previous)
            self.assertTrue(self.preview_file(preview).is_file())
        self.assertFalse(self.fake_state.exists())

    def test_dialog_repeated_signals_cannot_interrupt_cleanup_escalation(self) -> None:
        preview = self.prepare()
        args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        process = self.native_process("")
        process.poll.return_value = None
        process.communicate.side_effect = subprocess.TimeoutExpired(["osascript"], 310)

        def waiting(*, timeout):
            for sig in previous:
                signal.getsignal(sig)(sig, None)
            if process.wait.call_count == 1:
                raise subprocess.TimeoutExpired(["osascript"], timeout)
            return 0

        process.wait.side_effect = waiting
        with mock.patch.object(
            imessage.platform, "system", return_value="Darwin"
        ), mock.patch.object(imessage.subprocess, "Popen", return_value=process), mock.patch.object(
            imessage.os, "killpg"
        ) as kill, mock.patch.object(imessage, "commit_preview") as commit:
            with self.assertRaisesRegex(imessage.ImsgError, "interrupted"):
                imessage.handle(args)
            self.assertEqual(kill.call_args_list, [
                mock.call(process.pid, signal.SIGTERM), mock.call(process.pid, signal.SIGKILL)
            ])
            commit.assert_not_called()
        self.assertEqual(process.wait.call_args_list, [mock.call(timeout=2), mock.call(timeout=2)])
        self.assertEqual({sig: signal.getsignal(sig) for sig in previous}, previous)
        self.assertTrue(self.preview_file(preview).is_file())
        self.assertFalse(self.fake_state.exists())

    def test_dialog_cancellation_during_successful_cleanup_discards_approval(self) -> None:
        preview = self.prepare()
        args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        process = self.native_process(json.dumps({"status": "submitted", "text": preview["approval_phrase"]}))

        def finished():
            for sig in previous:
                signal.getsignal(sig)(sig, None)
            return 0

        process.poll.side_effect = finished
        with mock.patch.object(
            imessage.platform, "system", return_value="Darwin"
        ), mock.patch.object(imessage.subprocess, "Popen", return_value=process), mock.patch.object(
            imessage.os, "killpg"
        ) as kill, mock.patch.object(imessage, "commit_preview") as commit:
            with self.assertRaisesRegex(imessage.ImsgError, "interrupted"):
                imessage.handle(args)
            kill.assert_not_called()
            commit.assert_not_called()
        self.assertEqual({sig: signal.getsignal(sig) for sig in previous}, previous)
        self.assertTrue(self.preview_file(preview).is_file())
        self.assertFalse(self.fake_state.exists())

    def test_dialog_startup_failure_restores_handler_and_never_commits(self) -> None:
        preview = self.prepare()
        args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
        previous_handler = signal.getsignal(signal.SIGTERM)
        with mock.patch.object(
            imessage.platform, "system", return_value="Darwin"
        ), mock.patch.object(imessage.subprocess, "Popen", side_effect=OSError("No host")), mock.patch.object(
            imessage.os, "killpg"
        ) as kill, mock.patch.object(imessage, "commit_preview") as commit:
            with self.assertRaisesRegex(imessage.ImsgError, "unavailable"):
                imessage.handle(args)
            commit.assert_not_called()
            kill.assert_not_called()
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handler)
        self.assertTrue(self.preview_file(preview).is_file())
        self.assertFalse(self.fake_state.exists())

    def test_wrong_or_blank_native_text_never_claims_or_sends(self) -> None:
        preview = self.prepare()
        args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
        for typed in ("", "wrong", preview["approval_phrase"] + " ", preview["approval_phrase"] + "\n"):
            process = self.native_process(json.dumps({"status": "submitted", "text": typed}) + "\n")
            with self.subTest(typed=repr(typed)), mock.patch.object(
                imessage.platform, "system", return_value="Darwin"
            ), mock.patch.object(imessage.subprocess, "Popen", return_value=process), mock.patch.object(
                imessage, "run_imsg_send_rpc"
            ) as send:
                with self.assertRaisesRegex(imessage.ImsgError, "Approval must exactly match"):
                    imessage.handle(args)
                send.assert_not_called()
            self.assertTrue(self.preview_file(preview).is_file())
        self.assertFalse(self.fake_state.exists())

    def test_expiry_during_native_approval_cannot_send(self) -> None:
        preview = self.prepare()
        args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
        with mock.patch.object(imessage, "preview_expired", side_effect=[False, True]), mock.patch.object(
            imessage, "prompt_for_approval", return_value=preview["approval_phrase"]
        ) as prompt:
            with self.assertRaisesRegex(imessage.ImsgError, "expired"):
                imessage.handle(args)
            prompt.assert_called_once_with(preview["preview_id"], "1 bubble to Alex Doe.")
        self.assertTrue(self.preview_file(preview).is_file())
        self.assertFalse(self.fake_state.exists())

    def test_ui_diagnostic_cannot_access_messages_previews_or_send(self) -> None:
        args = imessage.build_parser().parse_args(["doctor", "--approval-ui"])
        with mock.patch.object(
            imessage, "typed_dialog", side_effect=lambda expected, **kwargs: expected
        ) as dialog, mock.patch.object(
            imessage, "doctor", side_effect=AssertionError("No ordinary doctor")
        ), mock.patch.object(imessage, "run_imsg", side_effect=AssertionError("No backend")), mock.patch.object(
            imessage, "state_root", side_effect=AssertionError("No preview state")
        ), mock.patch.object(imessage, "commit_preview", side_effect=AssertionError("No commit")), mock.patch.object(
            imessage, "run_imsg_send_rpc", side_effect=AssertionError("No send")
        ):
            result, ok = imessage.handle(args)
        self.assertTrue(ok)
        self.assertTrue(result["nothing_sent"])
        self.assertFalse(result["messages_accessed"])
        self.assertEqual(result["send_automation"], "not tested")
        self.assertRegex(dialog.call_args.args[0], r"^CHECK UI [0-9a-f]{8}$")
        self.assertEqual(dialog.call_args.kwargs, {"diagnostic": True})
        self.assertFalse(self.preview_state.exists())
        self.assertFalse(self.fake_state.exists())

    def test_ui_diagnostic_uses_same_native_renderer_with_short_timeout(self) -> None:
        expected = "CHECK UI 01010101"
        process = self.native_process(json.dumps({"status": "submitted", "text": expected}) + "\n")
        with mock.patch.object(imessage.os, "urandom", return_value=b"\x01" * 4), mock.patch.object(
            imessage.platform, "system", return_value="Darwin"
        ), mock.patch.object(imessage.subprocess, "Popen", return_value=process) as popen, mock.patch.object(
            imessage, "run_imsg", side_effect=AssertionError("No backend")
        ), mock.patch.object(imessage, "state_root", side_effect=AssertionError("No preview state")):
            result = imessage.check_approval_ui()
        self.assertEqual(result["approval_ui"], "typed diagnostic confirmed")
        command = popen.call_args.args[0]
        self.assertEqual(command[5], expected)
        self.assertEqual(command[6], "iMessage UI diagnostic")
        self.assertEqual(command[-2], "Check UI")
        self.assertEqual(command[-1], "60")
        process.communicate.assert_called_once_with(timeout=70)
        self.assertFalse(self.preview_state.exists())
        self.assertFalse(self.fake_state.exists())

    def test_ui_diagnostic_rejects_wrong_phrase_and_non_macos(self) -> None:
        with mock.patch.object(imessage, "typed_dialog", return_value="SEND IMESSAGE " + "0" * 24):
            with self.assertRaisesRegex(imessage.ImsgError, "did not match"):
                imessage.check_approval_ui()
        with mock.patch.object(imessage.platform, "system", return_value="Linux"), mock.patch.object(
            imessage.subprocess, "Popen"
        ) as popen:
            with self.assertRaisesRegex(imessage.ImsgError, "requires macOS"):
                imessage.check_approval_ui()
            popen.assert_not_called()
        self.assertFalse(self.preview_state.exists())
        self.assertFalse(self.fake_state.exists())

    def test_approved_preview_uses_rpc_stdin_sends_once_and_verifies(self) -> None:
        preview = self.prepare("Approved exact bytes")
        result = imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        self.assertEqual(result["status"], "sent_and_verified")
        state = json.loads(self.fake_state.read_text())
        self.assertEqual(state["count"], 1)
        self.assertEqual(state["argv"], ["rpc"])
        self.assertEqual(state["request"]["method"], "send")
        self.assertEqual(state["request"]["params"], {"chat_id": 5, "text": "Approved exact bytes", "service": "imessage"})
        self.assertTrue(self.preview_file(preview, "sent").is_file())
        with self.assertRaisesRegex(imessage.ImsgError, "unavailable"):
            imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        self.assertEqual(json.loads(self.fake_state.read_text())["count"], 1)

    def test_routing_change_invalidates_preview_before_send(self) -> None:
        preview = self.prepare()
        with mock.patch.object(imessage, "current_routing_matches", return_value=False):
            with self.assertRaisesRegex(imessage.ImsgError, "routing changed"):
                imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        self.assertTrue(self.preview_file(preview, "invalid").is_file())
        self.assertFalse(self.fake_state.exists())

    def test_expiry_during_final_checks_invalidates_without_send(self) -> None:
        preview = self.prepare()
        with mock.patch.object(imessage, "preview_expired", side_effect=[False, True]):
            with self.assertRaisesRegex(imessage.ImsgError, "expired before send"):
                imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        self.assertTrue(self.preview_file(preview, "invalid").is_file())
        self.assertFalse(self.fake_state.exists())

    def test_expiry_during_binary_validation_prevents_rpc_dispatch(self) -> None:
        preview = self.prepare()
        payload = imessage.load_preview(preview["preview_id"])["payload"]
        clock = [payload["expires_at"] - 1]

        def delayed_binary_validation():
            clock[0] = payload["expires_at"]
            return self.fake

        with mock.patch.object(imessage.time, "time", side_effect=lambda: clock[0]), mock.patch.object(
            imessage, "current_routing_matches", return_value=True
        ), mock.patch.object(
            imessage, "validate_upstream_binary", side_effect=delayed_binary_validation
        ), mock.patch.object(imessage.subprocess, "run") as dispatch:
            with self.assertRaisesRegex(imessage.ImsgError, "Preview expired before RPC dispatch"):
                imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
            dispatch.assert_not_called()
        self.assertTrue(self.preview_file(preview, "failed").is_file())
        self.assertFalse(self.fake_state.exists())

    def test_interrupt_and_post_claim_failure_become_uncertain(self) -> None:
        for failure in (KeyboardInterrupt(), imessage.ImsgError("history failed")):
            preview = self.prepare(str(type(failure).__name__))
            target = "run_imsg_send_rpc" if isinstance(failure, KeyboardInterrupt) else "find_verified_send"
            with mock.patch.object(imessage, target, side_effect=failure):
                with self.assertRaisesRegex(imessage.ImsgError, "uncertain"):
                    imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
            self.assertTrue(self.preview_file(preview, "uncertain").is_file())

    def test_not_started_rpc_failure_is_failed_and_non_retryable(self) -> None:
        preview = self.prepare()
        with mock.patch.dict(os.environ, {"FAKE_IMSG_MODE": "rpc-not-started"}):
            with self.assertRaisesRegex(imessage.ImsgError, "failed"):
                imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        self.assertTrue(self.preview_file(preview, "failed").is_file())
        self.assertFalse(self.fake_state.exists())

    def test_same_text_collision_without_rpc_ids_is_uncertain(self) -> None:
        preview = self.prepare("Duplicate text")
        with mock.patch.dict(os.environ, {"FAKE_IMSG_MODE": "no-rpc-ids"}):
            # Force ambiguous newest-first history after the mock send.
            original = imessage.history_rows
            def duplicate_history(chat_id, limit, attachments=False):
                rows = original(chat_id, limit, attachments=attachments)
                if self.fake_state.exists():
                    duplicate = dict(rows[0], id=201, guid="outgoing-201")
                    rows.insert(0, duplicate)
                return rows
            with mock.patch.object(imessage, "history_rows", side_effect=duplicate_history):
                with self.assertRaisesRegex(imessage.ImsgError, "uncertain"):
                    imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        self.assertTrue(self.preview_file(preview, "uncertain").is_file())

    def test_binary_checksum_and_symlink_are_rejected(self) -> None:
        original_hash = imessage.UPSTREAM_BINARY_SHA256
        with mock.patch.object(imessage, "UPSTREAM_BINARY_SHA256", "0" * 64):
            with self.assertRaisesRegex(imessage.ImsgError, "checksum mismatch"):
                imessage.validate_upstream_binary()
        link = self.root / "imsg-link"
        link.symlink_to(self.fake)
        with mock.patch.object(imessage, "upstream_binary", return_value=link), mock.patch.object(
            imessage, "UPSTREAM_BINARY_SHA256", original_hash
        ):
            with self.assertRaisesRegex(imessage.ImsgError, "non-symlink"):
                imessage.validate_upstream_binary()

    def test_binary_location_is_not_taken_from_the_environment(self) -> None:
        """The pinned binary comes from config or the default data dir, never from an env var."""
        text = HELPER.read_text()
        self.assertNotIn("IMSG_BINARY", text)
        self.assertNotIn("COMMS_KIT_CORE", text)
        self.assertIn("/usr/bin/python3", (HELPER.parent / "imessage").read_text())

    def test_send_handle_requires_gui_approval(self) -> None:
        preview = self.prepare("GUI gated")
        args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
        with mock.patch.object(imessage, "prompt_for_approval", return_value=preview["approval_phrase"]) as prompt:
            value, ok = imessage.handle(args)
        self.assertTrue(ok)
        self.assertEqual(value["status"], "sent_and_verified")
        prompt.assert_called_once_with(preview["preview_id"], "1 bubble to Alex Doe.")

    def test_one_typed_phrase_sends_every_bubble_in_order(self) -> None:
        """The whole point of the change: one approval, a whole message."""
        preview = self.prepare("First bubble", "Second bubble", "Third bubble")
        self.assertEqual(preview["bubbles"], 3)
        args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
        with mock.patch.object(
            imessage, "prompt_for_approval", return_value=preview["approval_phrase"]
        ) as prompt:
            value, ok = imessage.handle(args)

        self.assertTrue(ok)
        self.assertEqual(prompt.call_count, 1, "one message must cost exactly one dialog")
        self.assertEqual(value["status"], "sent_and_verified")
        self.assertEqual(value["bubbles"], 3)
        self.assertEqual([row["bubble"] for row in value["sent"]], [1, 2, 3])
        # Distinct rows, or a duplicate id would mean one bubble verified twice.
        self.assertEqual(len({row["message_id"] for row in value["sent"]}), 3)

        sends = json.loads(self.fake_state.read_text())["sends"]
        self.assertEqual(
            [entry["request"]["params"]["text"] for entry in sends],
            ["First bubble", "Second bubble", "Third bubble"],
            "bubbles must reach the chat in the order they were previewed",
        )
        # Shared RPC ids would let one bubble's reply answer for another.
        self.assertEqual(len({entry["request"]["id"] for entry in sends}), 3)
        self.assertTrue(self.preview_file(preview, "sent").is_file())

    def test_the_dialog_says_how_many_bubbles_one_phrase_will_send(self) -> None:
        """The phrase is only a preview id, so the count has to be on the window.

        Without this the person approving cannot tell a one-bubble send from a
        five-bubble one, which is the exact difference this change introduced.
        """
        captured: dict = {}

        def capture(expected, *, diagnostic=False, summary=None):
            captured["summary"] = summary
            return expected

        for texts, wanted in (
            (["Only one"], "1 bubble to Alex Doe."),
            (["One", "Two"],
             "2 bubbles to Alex Doe, sent in order. This one phrase authorizes all 2 parts."),
        ):
            with self.subTest(bubbles=len(texts)):
                captured.clear()
                preview = imessage.create_preview(5, texts)
                args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
                with mock.patch.object(imessage, "typed_dialog", side_effect=capture):
                    imessage.handle(args)
                self.assertEqual(captured["summary"], wanted)
                # The recipient must be named, not just the count.
                self.assertIn("Alex Doe", captured["summary"])

    def test_a_bubble_that_fails_mid_sequence_is_reported_as_partial(self) -> None:
        """A partial send is a NEW outcome per-bubble approval could not produce.

        Reporting it as a plain failure would say nothing was sent while the
        recipient is holding bubble 1, which is a lie in the dangerous direction.
        """
        preview = self.prepare("Bubble one lands", "Bubble two dies", "Bubble three never runs")
        original = imessage.run_imsg_send_rpc
        attempted: list = []

        def flaky(chat_id, text, request_id, **kwargs):
            attempted.append(text)
            if len(attempted) == 2:
                raise imessage.RpcSendError("upstream refused", "unknown")
            return original(chat_id, text, request_id, **kwargs)

        with mock.patch.object(imessage, "run_imsg_send_rpc", side_effect=flaky):
            with self.assertRaises(imessage.SendSequenceError) as caught:
                imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])

        report = caught.exception.report
        self.assertEqual(report["status"], "partial")
        self.assertEqual([row["bubble"] for row in report["sent"]], [1])
        self.assertEqual(report["failed_bubble"], 2)
        self.assertEqual(report["never_attempted"], [3], "bubble 3 must not be sent after 2 failed")
        self.assertIn("PARTIAL SEND", str(caught.exception))
        # Bubble 3 was never dispatched, so exactly one row reached the chat.
        self.assertEqual(len(json.loads(self.fake_state.read_text())["sends"]), 1)
        self.assertTrue(self.preview_file(preview, "partial").is_file())
        self.assertFalse(self.preview_file(preview).exists())

    def test_a_partial_send_can_never_be_retried(self) -> None:
        """Retrying would re-send the bubbles that already landed."""
        preview = self.prepare("Lands", "Dies")
        original = imessage.run_imsg_send_rpc
        attempted: list = []

        def flaky(chat_id, text, request_id, **kwargs):
            attempted.append(text)
            if len(attempted) == 2:
                raise imessage.RpcSendError("upstream refused", "unknown")
            return original(chat_id, text, request_id, **kwargs)

        with mock.patch.object(imessage, "run_imsg_send_rpc", side_effect=flaky):
            with self.assertRaises(imessage.SendSequenceError):
                imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])

        before = len(json.loads(self.fake_state.read_text())["sends"])
        with self.assertRaises(imessage.ImsgError):
            imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        self.assertEqual(
            len(json.loads(self.fake_state.read_text())["sends"]), before,
            "a retry after a partial send must not put another bubble on the wire",
        )

    def test_a_first_bubble_that_never_starts_is_failed_not_partial(self) -> None:
        """Nothing left the machine, so the honest status is still 'failed'."""
        preview = self.prepare("Never starts", "Nor this")

        def dead(chat_id, text, request_id, **kwargs):
            raise imessage.RpcSendError("nothing dispatched", "not_started")

        with mock.patch.object(imessage, "run_imsg_send_rpc", side_effect=dead):
            with self.assertRaises(imessage.SendSequenceError) as caught:
                imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        report = caught.exception.report
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["sent"], [])
        self.assertNotIn("PARTIAL SEND", str(caught.exception))
        self.assertTrue(self.preview_file(preview, "failed").is_file())
        self.assertFalse(self.fake_state.exists())

    def test_identical_bubbles_each_verify_against_their_own_row(self) -> None:
        """Repeated text is the case the advancing baseline exists for.

        Run with no upstream ids, so the row id is the ONLY thing telling the
        two rows apart. Without the baseline advance, bubble 2 matches both rows
        and a perfectly good send is reported as an ambiguous failure.
        """
        preview = self.prepare("Same words", "Same words")
        with mock.patch.dict(os.environ, {"FAKE_IMSG_MODE": "no-rpc-ids"}):
            result = imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        self.assertEqual(result["status"], "sent_and_verified")
        self.assertEqual([row["bubble"] for row in result["sent"]], [1, 2])
        self.assertEqual(len({row["message_id"] for row in result["sent"]}), 2)

    def test_the_bubble_and_total_character_caps_hold_before_any_backend_call(self) -> None:
        over_bubbles = [f"bubble {n}" for n in range(imessage.MAX_PREVIEW_BUBBLES + 1)]
        half = "x" * (imessage.MAX_TEXT_CHARS // 2 + 10)
        cases = (
            (over_bubbles, "at most"),
            ([half, half], "safety limit"),
            ([], "at least one"),
            (["fine", "   "], "Bubble 2 is empty"),
            (["fine", "hello " + "antml" + ":invoke"], "tool-call markup"),
        )
        with mock.patch.object(imessage, "safe_chat") as lookup:
            for texts, expected in cases:
                with self.subTest(expected=expected):
                    with self.assertRaisesRegex(imessage.ImsgError, expected):
                        imessage.create_preview(5, texts)
            lookup.assert_not_called()

        # Calibration: refusing one over the cap is only evidence if the cap
        # itself is accepted -- otherwise the rule could be "refuse everything".
        at_cap = [f"bubble {n}" for n in range(imessage.MAX_PREVIEW_BUBBLES)]
        self.assertEqual(imessage.create_preview(5, at_cap)["bubbles"], imessage.MAX_PREVIEW_BUBBLES)

    def test_the_digest_covers_every_bubble_not_only_the_first(self) -> None:
        """Editing any bubble must break the phrase bound to the whole message."""
        preview = self.prepare("Untouched first", "Rewritten second")
        path = self.preview_file(preview)
        for index in (0, 1):
            with self.subTest(bubble=index + 1):
                document = json.loads(path.read_text())
                document["payload"]["texts"][index] = "swapped in later"
                path.write_text(json.dumps(document))
                with self.assertRaisesRegex(imessage.ImsgError, "integrity"):
                    imessage.show_preview(preview["preview_id"])

    def test_empty_oversized_and_markup_drafts_fail_before_backend_access(self) -> None:
        texts = [
            "", " \t\n", "x" * (imessage.MAX_TEXT_CHARS + 1),
            "hello </invoke>", "</parameter>", "<function_calls>",
            "</function_calls>", '<invoke name="send">', '<parameter name="text">',
            "antml:invoke",
        ]
        with mock.patch.object(imessage, "safe_chat") as lookup:
            for text in texts:
                with self.subTest(text=text[:40]), self.assertRaises(imessage.ImsgError):
                    self.prepare(text)
            lookup.assert_not_called()
        self.assertFalse(self.fake_state.exists())

    def test_unicode_multiline_draft_is_preserved_exactly(self) -> None:
        text = "Hello 🌎\nTwo lines, café — שלום.\n"
        draft = self.drafts_dir() / "unicode.txt"
        draft.write_text(text, encoding="utf-8")
        draft.chmod(0o600)
        preview = self.prepare(imessage.read_private_text(draft))
        self.assertEqual(preview["texts"], [text])
        self.assertEqual(imessage.show_preview(preview["preview_id"])["texts"], [text])
        self.assertFalse(self.fake_state.exists())

    def test_missing_and_symlink_drafts_fail_closed(self) -> None:
        draft = self.root / "missing.txt"
        with self.assertRaises(imessage.ImsgError):
            imessage.read_private_text(draft)
        target = self.root / "target.txt"
        target.write_text("Private")
        target.chmod(0o600)
        draft.symlink_to(target)
        with self.assertRaises(imessage.ImsgError):
            imessage.read_private_text(draft)
        self.assertFalse(self.fake_state.exists())

    def test_invalid_expired_and_tampered_previews_never_open_approval(self) -> None:
        preview = self.prepare()
        args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
        with mock.patch.object(imessage, "prompt_for_approval") as prompt:
            with mock.patch.object(imessage, "preview_expired", return_value=True):
                with self.assertRaisesRegex(imessage.ImsgError, "expired"):
                    imessage.handle(args)
            document = json.loads(self.preview_file(preview).read_text())
            document["payload"]["texts"] = ["Changed after preview"]
            self.preview_file(preview).write_text(json.dumps(document))
            with self.assertRaisesRegex(imessage.ImsgError, "integrity"):
                imessage.handle(args)
            for bad_id in ("invalid", "0" * 24):
                args.preview_id = bad_id
                with self.assertRaises(imessage.ImsgError):
                    imessage.handle(args)
            prompt.assert_not_called()
        self.assertFalse(self.fake_state.exists())

    def test_malformed_target_snapshot_never_opens_approval(self) -> None:
        preview = self.prepare()
        original = json.loads(self.preview_file(preview).read_text())
        routing = original["payload"]["routing"]
        display = original["payload"]["display"]
        targets = [
            ("routing", {}),
            ("routing", dict(routing, id=True)),
            ("routing", dict(routing, id=-1)),
            ("routing", dict(routing, service="SMS")),
            ("routing", dict(routing, is_group=True)),
            ("routing", dict(routing, participants=[])),
            ("routing", dict(routing, guid="invalid")),
            ("display", {}),
            ("display", dict(display, participants=["different@example.invalid"])),
        ]
        for field, value in targets:
            with self.subTest(field=field, value=value):
                document = json.loads(json.dumps(original))
                document["payload"][field] = value
                digest = imessage.payload_digest(document["payload"])
                document.update(digest=digest, preview_id=digest[:24])
                path = self.preview_state / "previews" / "pending" / f"{digest[:24]}.json"
                path.write_text(json.dumps(document))
                path.chmod(0o600)
                args = SimpleNamespace(command="send-preview", preview_id=digest[:24])
                with mock.patch.object(imessage, "prompt_for_approval") as prompt, mock.patch.object(
                    imessage, "commit_preview", side_effect=imessage.ImsgError("Commit must not run")
                ) as commit:
                    with self.assertRaises(imessage.ImsgError):
                        imessage.handle(args)
                    prompt.assert_not_called()
                    commit.assert_not_called()
        self.assertFalse(self.fake_state.exists())

    def test_payload_marker_check_rejects_every_tool_markup_marker(self) -> None:
        markers = ("</invoke>", "</parameter>", "</function_calls>", "<function_calls>",
                   "<invoke name=", "<parameter name=", "antml:")
        for marker in markers:
            with self.subTest(marker=marker), self.assertRaisesRegex(imessage.ImsgError, "tool-call markup"):
                imessage.reject_tool_markup("Draft " + marker)
        imessage.reject_tool_markup("Normal text with < and > is fine.")
        self.assertFalse(self.fake_state.exists())

    def rewrite_preview(self, preview, mutate) -> str:
        """Hand-edit a stored preview and re-seal it with a VALID digest.

        A tampered record that recomputes its own hash is exactly the case the
        digest cannot catch, so every content rule has to hold again at load
        time rather than only at creation.
        """
        document = json.loads(self.preview_file(preview).read_text())
        mutate(document["payload"])
        digest = imessage.payload_digest(document["payload"])
        document.update(digest=digest, preview_id=digest[:24])
        path = self.preview_state / "previews" / "pending" / f"{digest[:24]}.json"
        path.write_text(json.dumps(document))
        path.chmod(0o600)
        return digest[:24]

    def assert_preview_never_reaches_approval(self, preview_id, pattern) -> None:
        args = SimpleNamespace(command="send-preview", preview_id=preview_id)
        with mock.patch.object(imessage, "prompt_for_approval") as prompt:
            with self.assertRaisesRegex(imessage.ImsgError, pattern):
                imessage.handle(args)
            prompt.assert_not_called()
        self.assertFalse(self.fake_state.exists())

    def test_existing_markup_preview_is_rejected_even_with_valid_digest(self) -> None:
        poisoned = "Bad </invoke>"

        def poison(payload):
            # BOTH representations, so this exercises the markup refusal rather
            # than tripping the texts/parts consistency check first.
            payload["texts"] = [poisoned]
            payload["parts"] = [{"kind": "text", "text": poisoned}]

        preview_id = self.rewrite_preview(self.prepare(), poison)
        self.assert_preview_never_reaches_approval(preview_id, "tool-call markup")

    def test_a_preview_whose_text_and_parts_disagree_is_rejected(self) -> None:
        """Two representations of one message, edited apart, each clean alone.

        Validation read `parts` while the send read `texts`, so a substituted
        `texts` beside an innocent `parts` passed every content rule and would
        still have gone out. Once they differ, neither copy is trustworthy.
        """
        def split(payload):
            payload["texts"] = ["Something Alex Doe never approved"]
            payload["parts"] = [{"kind": "text", "text": "The words he did approve"}]

        preview_id = self.rewrite_preview(self.prepare(), split)
        self.assert_preview_never_reaches_approval(preview_id, "text and parts disagree")

    def test_cancelled_approval_keeps_preview_unsent(self) -> None:
        preview = self.prepare()
        args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
        with mock.patch.object(
            imessage, "prompt_for_approval", side_effect=imessage.ImsgError("Cancelled")
        ):
            with self.assertRaisesRegex(imessage.ImsgError, "Cancelled"):
                imessage.handle(args)
        self.assertTrue(self.preview_file(preview).is_file())
        self.assertFalse(self.fake_state.exists())

    def test_consumed_preview_never_reopens_approval(self) -> None:
        preview = self.prepare()
        imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        args = SimpleNamespace(command="send-preview", preview_id=preview["preview_id"])
        with mock.patch.object(imessage, "prompt_for_approval") as prompt:
            with self.assertRaises(imessage.ImsgError):
                imessage.handle(args)
            prompt.assert_not_called()
        self.assertEqual(json.loads(self.fake_state.read_text())["count"], 1)

    def test_concurrent_claims_send_at_most_once(self) -> None:
        preview = self.prepare("Only one mock send")
        barrier = threading.Barrier(2, timeout=10)
        original_load = imessage.load_preview

        def synchronized_load(*args, **kwargs):
            document = original_load(*args, **kwargs)
            barrier.wait()
            return document

        def commit():
            try:
                return imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
            except imessage.ImsgError:
                return None

        with mock.patch.object(imessage, "load_preview", side_effect=synchronized_load):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _: commit(), range(2)))
        self.assertEqual(sum(value is not None for value in outcomes), 1)
        self.assertEqual(json.loads(self.fake_state.read_text())["count"], 1)
        self.assertTrue(self.preview_file(preview, "sent").is_file())

    def test_rpc_timeout_and_malformed_response_are_non_retryable(self) -> None:
        failures = [
            subprocess.TimeoutExpired(["fake-imsg", "rpc"], 20),
            subprocess.CompletedProcess(["fake-imsg", "rpc"], 0, stdout="not-json", stderr=""),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                preview = self.prepare(type(failure).__name__)
                with mock.patch.object(
                    imessage, "current_routing_matches", return_value=True
                ), mock.patch.object(imessage, "validate_upstream_binary", return_value=self.fake):
                    kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                    with mock.patch.object(imessage.subprocess, "run", **kwargs):
                        with self.assertRaisesRegex(imessage.ImsgError, "uncertain"):
                            imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
                self.assertTrue(self.preview_file(preview, "uncertain").is_file())
                with self.assertRaises(imessage.ImsgError):
                    imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        self.assertFalse(self.fake_state.exists())

    def test_parser_rejects_malformed_arguments_without_any_backend(self) -> None:
        cases = [
            ["send", "routing@example.invalid", "hello", "world"],
            ["send-file", "routing@example.invalid"],
            ["prepare-send", "--chat-id", "5"],
            ["prepare-send", "--text", "inline text", "--chat-id", "5"],
            ["history"], ["history", "--chat-id", "not-a-number"],
            ["history", "--chat-id", "5", "--limit", "1001"],
            ["chats", "--limit", "0"], ["chats", "--limit", "501"],
            ["chats", "--all"], ["resolve"], ["resolve", "extra", "args"],
            ["send-preview"], ["send-preview", "0" * 24, "--approval", "typed-by-agent"],
            ["send-preview", "0" * 24, "--approval-ui"],
            ["doctor", "--approval", "typed-by-agent"],
            ["doctor", "--approval-ui", "--chat-id", "5"],
            ["--no-use-secondary-instance", "chats"], ["--json", "chats"],
        ]
        for args in cases:
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    imessage.build_parser().parse_args(args)
                self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.fake_state.exists())

    # ------------------------------------------------------------------
    # Read-side capabilities added 2026-09-09. Every one of these is a read;
    # none of them may reach a preview, a dialog, or a send.
    # ------------------------------------------------------------------

    def test_search_excludes_every_chat_this_interface_cannot_read(self) -> None:
        result, ok = imessage.handle(self.parsed("search", "needle"))
        self.assertTrue(ok)
        kept = sorted(row["chat_id"] for row in result["messages"])
        # Control: the fake returns four hits. Three are eligible (a direct
        # iMessage chat, an iMessage group and, since 2026-10-09, a direct SMS
        # chat); chat 6 is a direct chat with two participants. A filter that
        # kept everything would pass an assertion on the survivors alone.
        self.assertEqual(kept, [1, 3, 4])
        self.assertEqual(result["excluded"]["messages"], 1)
        self.assertEqual(result["excluded"]["chats"], [6])
        self.assertEqual(result["excluded"]["unchecked_chats"], [])
        self.assertEqual([chat["id"] for chat in result["chats"]], [1, 3, 4])
        self.assertIn("not proof", result["scope"])

    def test_a_search_query_never_becomes_a_process_argument(self) -> None:
        secret = "a private thing Alex Doe searched for"
        imessage.handle(self.parsed("search", secret))
        calls = self.upstream_calls()
        self.assertIn(["rpc"], calls)
        for call in calls:
            for token in call:
                self.assertNotIn(secret, token)

    def test_the_read_rpc_refuses_every_method_that_can_write(self) -> None:
        writers = (
            "send", "send.rich", "send.attachment", "message.edit", "message.unsend",
            "message.delete", "tapback", "typing", "read", "chats.create",
            "chats.markUnread", "group.addParticipant", "group.leave", "poll.send",
        )
        for method in writers:
            with self.subTest(method=method):
                before = len(self.upstream_calls())
                with self.assertRaisesRegex(imessage.ImsgError, "non-read-only"):
                    imessage.run_imsg_read_rpc(method, {})
                # Refused before the binary is spawned, not after it answers.
                self.assertEqual(len(self.upstream_calls()), before)
        self.assertEqual(imessage.READ_ONLY_RPC_METHODS, frozenset({"messages.search"}))

    def test_the_history_window_and_participant_filters_reach_the_binary(self) -> None:
        rows = lambda *argv: imessage.handle(self.parsed(*argv))[0]["messages"]
        baseline = rows("history", "--chat-id", "3", "--limit", "9")
        self.assertTrue(baseline)
        # Each filter is checked with a value that must return nothing, so a
        # flag the wrapper builds and never passes on is visible.
        self.assertEqual(rows("history", "--chat-id", "3", "--limit", "9", "--since", "2031-01-01T00:00:00Z"), [])
        self.assertEqual(rows("history", "--chat-id", "3", "--limit", "9", "--until", "2019-01-01T00:00:00Z"), [])
        self.assertEqual(rows("history", "--chat-id", "3", "--limit", "9", "--participant", "+19995550100"), [])
        kept = rows("history", "--chat-id", "3", "--limit", "9", "--participant", "+12065550105")
        self.assertTrue(kept)
        self.assertTrue(all(row["sender"] == "+12065550105" for row in kept))
        flat = [token for call in self.upstream_calls() for token in call]
        for flag in ("--start", "--end", "--participants"):
            self.assertIn(flag, flat)

    def test_a_participant_filter_on_a_direct_chat_warns_that_it_is_a_no_op(self) -> None:
        # Chat 5 is direct, chat 3 is a group. The warning must appear on the
        # first and not the second, and never without a participant filter --
        # otherwise it is decoration rather than a signal.
        direct, _ = imessage.handle(
            self.parsed("history", "--chat-id", "5", "--limit", "5", "--participant", "+12065550105")
        )
        self.assertIn("note", direct["window"])
        self.assertIn("is_from_me", direct["window"]["note"])

        group, _ = imessage.handle(
            self.parsed("history", "--chat-id", "3", "--limit", "5", "--participant", "+12065550105")
        )
        self.assertNotIn("note", group["window"])

        unfiltered, _ = imessage.handle(self.parsed("history", "--chat-id", "5", "--limit", "5"))
        self.assertNotIn("note", unfiltered["window"])
        self.assertEqual(unfiltered["window"]["participants"], [])

    def test_two_participants_are_passed_as_one_comma_separated_value(self) -> None:
        imessage.history_rows(3, 5, participants=["+12065550105", "+12065550101"])
        call = next(c for c in reversed(self.upstream_calls()) if c and c[0] == "history")
        self.assertEqual(call[call.index("--participants") + 1], "+12065550105,+12065550101")

    def test_malformed_instants_and_handles_are_refused_before_the_binary(self) -> None:
        bad_instants = ("2026-09-01", "2026-09-01T00:00:00", "2026-09-01T00:00:00+00:00", "now", "")
        for value in bad_instants:
            with self.subTest(instant=value):
                before = len(self.upstream_calls())
                with self.assertRaisesRegex(imessage.ImsgError, "ISO-8601"):
                    imessage.history_rows(3, 5, since=value)
                self.assertEqual(len(self.upstream_calls()), before)
        for handle in ("+1555 000", "a;b", "--limit", "x" * 200, ""):
            with self.subTest(handle=handle):
                with self.assertRaisesRegex(imessage.ImsgError, "phone number or email"):
                    imessage.history_rows(3, 5, participants=[handle])

    def test_unread_only_is_a_read_and_reaches_the_binary(self) -> None:
        result, ok = imessage.handle(self.parsed("chats", "--unread-only", "--limit", "20"))
        self.assertTrue(ok)
        self.assertTrue(result["unread_only"])
        self.assertEqual([chat["id"] for chat in result["chats"]], [1, 3])
        plain, _ = imessage.handle(self.parsed("chats", "--limit", "20"))
        self.assertFalse(plain["unread_only"])
        self.assertGreater(len(plain["chats"]), len(result["chats"]))
        self.assertIn("--unread-only", [t for c in self.upstream_calls() for t in c])

    def test_whois_is_local_only_and_reports_real_chat_eligibility(self) -> None:
        result, ok = imessage.handle(self.parsed("whois", "+12065550101"))
        self.assertTrue(ok)
        self.assertEqual(result["service"], "imessage")
        # This handle is in direct chat 1, group chat 3, and malformed chat 6
        # (two participants on a direct chat), which must not be offered.
        self.assertEqual(result["readable_direct_chat_ids"], [1])
        self.assertEqual(result["readable_group_chat_ids"], [3])
        self.assertTrue(result["has_readable_direct_chat"])
        call = next(c for c in reversed(self.upstream_calls()) if c and c[0] == "whois")
        # --local is what keeps this off the SIP bridge; without it the
        # upstream default is a live reachability probe.
        self.assertIn("--local", call)
        self.assertNotIn("sendable_here", result)
        self.assertIn("not permission", result["scope"])

    def test_whois_offers_a_handle_with_only_an_sms_thread_as_readable(self) -> None:
        # Chat 4 is SMS. Since 2026-10-09 (owner decision: SMS chats are readable) it is
        # a readable direct chat, so whois offers it.
        result, ok = imessage.handle(self.parsed("whois", "+12065550104"))
        self.assertTrue(ok)
        self.assertEqual(result["readable_direct_chat_ids"], [4])
        self.assertEqual(result["readable_group_chat_ids"], [])
        self.assertTrue(result["has_readable_direct_chat"])

    def test_whois_refuses_a_malformed_handle_before_the_binary(self) -> None:
        before = len(self.upstream_calls())
        with self.assertRaisesRegex(imessage.ImsgError, "phone number or email"):
            imessage.whois_local("not a handle", "phone")
        with self.assertRaisesRegex(imessage.ImsgError, "Handle type must be"):
            imessage.whois_local("+12065550101", "guid")
        self.assertEqual(len(self.upstream_calls()), before)

    def test_the_chat_verdict_budget_bounds_lookups_and_is_reported(self) -> None:
        eligibility = imessage.ChatEligibility(lookup_budget=1)
        self.assertTrue(eligibility.readable(1))
        # Budget spent. The second distinct chat is refused as unreadable AND
        # named as unchecked, so a bounded scan can never be mistaken for a
        # verdict of "not eligible".
        self.assertFalse(eligibility.readable(3))
        self.assertEqual(eligibility.unchecked, {3})
        # A cached chat must not spend budget a second time.
        self.assertTrue(eligibility.readable(1))
        self.assertEqual(eligibility.unchecked, {3})

    def test_search_never_exhausts_its_budget_because_it_scales_with_the_limit(self) -> None:
        for limit in (1, 4, imessage.MAX_SEARCH_LIMIT):
            with self.subTest(limit=limit):
                result = imessage.search_messages("needle", "contains", limit)
                self.assertEqual(result["excluded"]["unchecked_chats"], [])

    def test_stats_summarises_rather_than_dumping_the_series(self) -> None:
        result, ok = imessage.handle(self.parsed("stats"))
        self.assertTrue(ok)
        self.assertEqual(result["totals"]["messages"], 90)
        self.assertEqual(result["totals"]["chats"], 30)
        self.assertEqual(len(result["top_chats"]), imessage.STATS_TOP_N)
        self.assertEqual(len(result["top_senders"]), imessage.STATS_TOP_N)
        self.assertEqual(result["days_covered"], 20)
        self.assertEqual((result["first_day"], result["last_day"]), ("2026-01-01", "2026-01-20"))
        self.assertNotIn("dates", result)
        self.assertIn("SMS", result["scope"])

    def test_doctor_says_why_the_write_features_are_absent(self) -> None:
        with mock.patch.object(imessage.platform, "system", return_value="Darwin"):
            result, ok = imessage.doctor()
        self.assertTrue(ok)
        advanced = result["advanced_features"]
        self.assertEqual(advanced["sip"], "enabled")
        self.assertFalse(advanced["available_here"])
        self.assertIn("unsend", advanced["requires_sip_disabled"])
        self.assertIn("live watch", advanced["withheld_by_this_wrapper"])

    def test_every_read_command_dispatches_from_the_real_parser(self) -> None:
        # A hand-built namespace let `history` ship reading an attribute the
        # parser never declared. Drive each read command end to end instead.
        commands = (
            ["doctor"], ["chats", "--limit", "3"], ["chats", "--unread-only"],
            ["resolve", "5"], ["history", "--chat-id", "5", "--limit", "2"],
            ["history", "--chat-id", "5", "--limit", "2", "--since", "2020-01-01T00:00:00Z"],
            ["search", "needle"], ["search", "needle", "--match", "exact", "--limit", "3"],
            ["whois", "+12065550101"], ["whois", "jane@example.com", "--type", "email"],
            ["stats"],
        )
        for argv in commands:
            with self.subTest(argv=argv):
                with mock.patch.object(imessage.platform, "system", return_value="Darwin"):
                    value, ok = imessage.handle(self.parsed(*argv))
                self.assertTrue(ok)
                self.assertIsInstance(value, dict)
        # None of them may leave preview state or touch a send path.
        self.assertFalse((self.preview_state / "previews").exists())
        for call in self.upstream_calls():
            self.assertNotIn(call[0] if call else "", {"send", "send-rich", "send-attachment", "react", "tapback", "edit", "unsend", "launch", "watch"})

    def test_the_new_read_commands_are_rejected_when_malformed(self) -> None:
        cases = [
            ["search"], ["search", "a", "--match", "regex"], ["search", "a", "--limit", "0"],
            ["search", "a", "--limit", str(imessage.MAX_SEARCH_LIMIT + 1)],
            ["whois"], ["whois", "a", "--type", "handle"],
            ["stats", "--limit", "5"], ["chats", "--unread-only", "true"],
            ["history", "--chat-id", "5", "--since"],
            ["watch"], ["send"], ["react"], ["edit"], ["unsend"], ["launch"], ["rpc"],
        ]
        for args in cases:
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    imessage.build_parser().parse_args(args)
                self.assertEqual(error.exception.code, 2)



    # ---------------------------------------------------------------- #
    # Everything the tool can do: attachments, groups, SMS, new people.  #
    # Unblocked 2026-09-09 at the owner's explicit instruction. The gates    #
    # that stayed are routing revalidation, the wording gate, the        #
    # credential-path refusal and per-part verification.                 #
    # ---------------------------------------------------------------- #

    def headless_send(self, *argv: str) -> dict:
        """Run one real headless send all the way through the dispatcher.

        The wording gate is stubbed clean deliberately: these tests are about
        routing, parts and verification, and calling the real checker here
        would make them fail every time its style rules change.
        """
        with mock.patch.object(imessage, "run_draft_check", return_value=("clean", "ok")):
            value, ok = imessage.handle(send_args(*argv))
        self.assertTrue(ok)
        return value

    def rpc_params(self) -> list[dict]:
        return [
            entry["request"]["params"]
            for entry in json.loads(self.fake_state.read_text())["sends"]
        ]

    def attachment(self, name: str = "photo.png", data: bytes = b"\x89PNG\x00\x1a\n\x00binary") -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_a_file_part_is_sent_as_a_file_and_verified_on_its_ffffc_row(self) -> None:
        """An attachment carries no text, so the row it produces has no text either.

        Verification therefore cannot compare message bodies. It compares the
        U+FFFC row Messages actually writes, plus the rowid and GUID from the
        RPC reply, with the filename as corroboration only.
        """
        photo = self.attachment()
        result = self.headless_send("--chat-id", "5", "--file", str(photo))
        self.assertEqual(result["status"], "sent_and_verified")
        params = self.rpc_params()
        self.assertEqual(len(params), 1)
        self.assertEqual(params[0]["file"], str(photo.resolve()))
        # A file part must not smuggle a text body along with it.
        self.assertNotIn("text", params[0])
        self.assertEqual(result["sent"][0]["kind"], "file")
        self.assertEqual(result["sent"][0]["file"], "photo.png")
        self.assertEqual(result["parts"], ["photo.png"])

    def test_text_and_files_leave_in_the_order_the_flags_were_given(self) -> None:
        photo = self.attachment()
        result = self.headless_send(
            "--chat-id", "5", "--text", "Here it is", "--file", str(photo), "--text", "Thoughts?"
        )
        self.assertEqual(result["status"], "sent_and_verified")
        params = self.rpc_params()
        self.assertEqual(
            [(item.get("text"), Path(item["file"]).name if item.get("file") else None)
             for item in params],
            [("Here it is", None), (None, "photo.png"), ("Thoughts?", None)],
        )
        self.assertEqual([row["kind"] for row in result["sent"]], ["text", "file", "text"])

    def test_two_attachments_in_one_send_do_not_verify_against_each_other(self) -> None:
        """Both rows carry the identical text U+FFFC, so the baseline must advance.

        Without that, part 2 matches part 1's row as well as its own,
        verification is correctly called ambiguous, and a perfectly good send
        gets reported as partial.
        """
        first = self.attachment("one.png")
        second = self.attachment("two.png")
        result = self.headless_send(
            "--chat-id", "5", "--file", str(first), "--file", str(second)
        )
        self.assertEqual(result["status"], "sent_and_verified")
        self.assertEqual([row["file"] for row in result["sent"]], ["one.png", "two.png"])
        self.assertLess(result["sent"][0]["message_id"], result["sent"][1]["message_id"])

    def test_a_group_send_is_allowed_headless_and_still_verified(self) -> None:
        result = self.headless_send("--chat-id", "3", "--text", "Hi both")
        self.assertEqual(result["status"], "sent_and_verified")
        self.assertEqual(result["chat_id"], 3)
        self.assertEqual(self.rpc_params()[0]["chat_id"], 3)

    def test_an_unnamed_group_is_described_by_its_people_not_a_wall_of_digits(self) -> None:
        detail = imessage.chat_detail(3)
        detail["name"] = ""
        snapshot = imessage.display_snapshot(detail)
        self.assertEqual(snapshot["name"], "Group of 2: ...0101, ...0102")

    def test_an_sms_chat_sends_as_sms_without_being_told(self) -> None:
        """The chat decides the service. Defaulting to iMessage would fail every
        SMS send, and defaulting to auto would quietly downgrade encrypted ones."""
        result = self.headless_send("--chat-id", "4", "--text", "Green bubble")
        self.assertEqual(result["status"], "sent_and_verified")
        self.assertEqual(self.rpc_params()[0]["service"], "sms")

    def test_an_explicit_service_overrides_the_chat(self) -> None:
        self.headless_send("--chat-id", "5", "--text", "Fall back if you must", "--service", "auto")
        self.assertEqual(self.rpc_params()[0]["service"], "auto")

    def test_a_handle_that_already_has_a_chat_reuses_it(self) -> None:
        """Opening a second thread to somebody Alex Doe already texts is a bug on
        his phone, and a bare handle gives weaker routing and no baseline."""
        result = self.headless_send("--to", "+12065550105", "--text", "Hello again")
        self.assertEqual(result["status"], "sent_and_verified")
        self.assertEqual(result["chat_id"], 5)
        self.assertEqual(result["target"]["chat_id"], 5)
        self.assertEqual(result["target"]["resolved_from"], "+12065550105")
        # Without this, nothing in the output would reveal that a typed handle
        # and the chat it resolved to are different people.
        self.assertEqual(result["target"]["recipient"]["participants"], ["+12065550105"])
        self.assertEqual(result["target"]["recipient"]["name"], "Alex Doe")
        params = self.rpc_params()[0]
        self.assertEqual(params["chat_id"], 5)
        self.assertNotIn("to", params)

    def test_a_bare_national_number_matches_the_chat_stored_in_e164(self) -> None:
        result = self.headless_send("--to", "2065550105", "--text", "Digits only")
        self.assertEqual(result["chat_id"], 5)
        self.assertTrue(imessage.handles_match("+12065550105", "2065550101") is False)

    def test_a_brand_new_recipient_creates_the_chat_and_verifies_inside_it(self) -> None:
        result = self.headless_send("--to", "+12065550188", "--text", "First contact")
        self.assertEqual(result["status"], "sent_and_verified")
        self.assertTrue(result["target"]["new_recipient"])
        params = self.rpc_params()[0]
        self.assertEqual(params["to"], "+12065550188")
        self.assertEqual(params["region"], "US")
        self.assertEqual(params["service"], "imessage")
        self.assertNotIn("chat_id", params)
        # The chat the runtime created is what the outgoing row was verified in.
        self.assertEqual(result["chat_id"], 76)

    def test_later_parts_to_a_new_recipient_go_by_chat_id(self) -> None:
        result = self.headless_send(
            "--to", "+12065550188", "--text", "First", "--text", "Second"
        )
        self.assertEqual(result["status"], "sent_and_verified")
        first, second = self.rpc_params()
        self.assertEqual(first["to"], "+12065550188")
        self.assertNotIn("to", second)
        self.assertEqual(second["chat_id"], 76)

    def test_an_ambiguous_handle_refuses_before_anything_is_sent(self) -> None:
        twins = [{"id": 1, "is_group": False, "participants": ["+12065550109"]},
                 {"id": 2, "is_group": False, "participants": ["+1 206 555 0109"]}]
        with mock.patch.object(imessage, "list_chats", return_value=twins):
            with self.assertRaisesRegex(imessage.ImsgError, "2 existing chats match"):
                self.headless_send("--to", "+12065550109", "--text", "Which one?")
        self.assertFalse(self.fake_state.exists())

    def test_a_shortcode_cannot_be_a_target(self) -> None:
        with self.assertRaisesRegex(imessage.ImsgError, "10-digit number"):
            self.headless_send("--to", "50195", "--text", "Nope")
        self.assertFalse(self.fake_state.exists())

    def test_a_new_recipient_preview_is_void_once_a_chat_for_them_exists(self) -> None:
        """Ten minutes is long enough for Messages, or Alex Doe, to open the thread.

        The preview promised somebody with no chat. If one exists at commit
        time, the send would land in a thread the preview never described.
        """
        preview = imessage.create_handle_preview(
            "+12065550188", [{"kind": "text", "text": "Hi"}], service="imessage", region="US"
        )
        appeared = [{"id": 90, "is_group": False, "participants": ["+12065550188"]}]
        with mock.patch.object(imessage, "list_chats", return_value=appeared):
            with self.assertRaisesRegex(imessage.ImsgError, "routing changed"):
                imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        self.assertTrue(self.preview_file(preview, "invalid").is_file())
        self.assertFalse(self.fake_state.exists())

    def test_a_handle_preview_cannot_smuggle_chat_routing(self) -> None:
        """Two targets in one record is one target too many.

        A handle preview with a chat id would be validated as a handle send and
        delivered to the chat, so the only safe answer is to refuse the record.
        """
        preview = imessage.create_handle_preview(
            "+12065550188", [{"kind": "text", "text": "Hi"}], service="imessage", region="US"
        )
        preview_id = self.rewrite_preview(
            preview, lambda payload: payload["routing"].update(id=5)
        )
        self.assert_preview_never_reaches_approval(preview_id, "must not carry chat routing")

    def test_a_tampered_service_cannot_downgrade_an_imessage_to_sms(self) -> None:
        """The one field that changes where a message goes without changing a
        single visible character of it."""
        preview_id = self.rewrite_preview(
            self.prepare(), lambda payload: payload.update(service="carrier-pigeon")
        )
        self.assert_preview_never_reaches_approval(preview_id, "Malformed preview service")

    def test_the_audit_log_records_counts_and_routing_but_never_content(self) -> None:
        photo = self.attachment("receipt.png")
        self.headless_send("--chat-id", "5", "--text", "Secret words", "--file", str(photo))
        entries = [
            json.loads(line)
            for line in (self.preview_state / "send-audit.jsonl").read_text().splitlines()
            if line.strip()
        ]
        self.assertEqual(entries[-1]["bubbles"], 1)
        self.assertEqual(entries[-1]["files"], 1)
        self.assertEqual(entries[-1]["chat_id"], 5)
        self.assertEqual(entries[-1]["status"], "sent_and_verified")
        raw = (self.preview_state / "send-audit.jsonl").read_text()
        self.assertNotIn("Secret words", raw)
        self.assertNotIn("receipt.png", raw)

    def test_a_file_only_send_still_cannot_carry_a_credential(self) -> None:
        secret = self.root / "id_rsa"
        secret.write_bytes(b"PRIVATE KEY")
        with self.assertRaisesRegex(imessage.ImsgError, "credentials live"):
            self.headless_send("--chat-id", "5", "--file", str(secret))
        self.assertFalse(self.fake_state.exists())

    def test_a_binary_only_send_says_plainly_that_nothing_was_read(self) -> None:
        """Reporting "passed" would claim a check ran that never did."""
        photo = self.attachment()
        with mock.patch.object(
            imessage, "run_draft_check", side_effect=AssertionError("nothing to read")
        ):
            value, ok = imessage.handle(send_args("--chat-id", "5", "--file", str(photo)))
        self.assertTrue(ok)
        self.assertIn("nothing readable to check", value["draft_check"])
        self.assertIn("were NOT read", value["draft_check"])

    def test_a_text_attachment_goes_through_the_wording_gate(self) -> None:
        """The MONEY LOCK bypass an adversarial review found.

        `--file note.txt` containing "$5,000 up front" skipped the gate entirely,
        while the identical words as `--text-file` did not. The gate reads words;
        those are words, whichever flag carries them.
        """
        note = self.root / "quote.txt"
        note.write_text("yes, $5,000 up front works for me")
        seen: list = []

        def spy(texts):
            seen.append(list(texts))
            return ("clean", "ok")

        with mock.patch.object(imessage, "run_draft_check", side_effect=spy):
            value, ok = imessage.handle(
                send_args("--chat-id", "5", "--text", "Sending the quote", "--file", str(note))
            )
        self.assertTrue(ok)
        self.assertEqual(seen[0], ["Sending the quote", "yes, $5,000 up front works for me"])
        self.assertIn("also read the text of quote.txt", value["draft_check"])

    def test_a_text_attachment_finding_stops_the_send(self) -> None:
        note = self.root / "quote.txt"
        note.write_text("invoice attached, $5,000 deposit due")
        with mock.patch.object(
            imessage, "run_draft_check", return_value=("finding", "MONEY LOCK")
        ):
            with self.assertRaisesRegex(imessage.ImsgError, "nothing was sent"):
                imessage.handle(send_args("--chat-id", "5", "--file", str(note)))
        self.assertFalse(self.fake_state.exists())


    def test_an_attachment_verifies_with_no_rpc_id_and_no_guid(self) -> None:
        """The path a real attachment send actually takes.

        Measured 2026-09-09: an attachment's RPC reply carries neither `id` nor
        `guid`, so nothing identifies the row outright. Verification falls back
        to the U+FFFC row text, the transfer_name, the advancing rowid baseline
        and the time window. The fake reproduces that omission, so this is the
        real path, not a friendlier one.
        """
        photo = self.attachment()
        result = self.headless_send("--chat-id", "5", "--file", str(photo))
        upstream = result["sent"][0]["upstream"]
        self.assertNotIn("id", upstream)
        self.assertNotIn("guid", upstream)
        self.assertEqual(result["status"], "sent_and_verified")
        # The row was still pinned down, from history rather than the reply.
        self.assertIsInstance(result["sent"][0]["message_id"], int)

    def test_two_identically_named_attachments_need_the_baseline_advance(self) -> None:
        """Same filename, same U+FFFC row text, no rpc ids: the ONLY thing
        telling the two rows apart is the advancing baseline.

        Without it part 2 matches both rows, find_verified_send correctly calls
        the verification ambiguous, and a perfectly good send is reported as a
        partial failure.
        """
        first = self.root / "a"
        second = self.root / "b"
        for folder in (first, second):
            folder.mkdir()
            (folder / "same.png").write_bytes(b"\x89PNG\x00\x1a\n\x00binary")
        result = self.headless_send(
            "--chat-id", "5",
            "--file", str(first / "same.png"),
            "--file", str(second / "same.png"),
        )
        self.assertEqual(result["status"], "sent_and_verified")
        self.assertEqual([row["file"] for row in result["sent"]], ["same.png", "same.png"])
        self.assertEqual(len({row["message_id"] for row in result["sent"]}), 2)

    def test_a_file_part_failing_mid_sequence_is_reported_as_partial(self) -> None:
        """A file failing half way is its own outcome and must name itself.

        The text-only partial tests would pass even if the is_file branch got
        the index, the expected row text or the never-attempted list wrong.
        """
        photo = self.attachment()
        original = imessage.run_imsg_send_rpc
        attempted: list = []

        def flaky(chat_id, text, request_id, **kwargs):
            attempted.append(kwargs.get("file") or text)
            if kwargs.get("file"):
                raise imessage.RpcSendError("upstream refused the file", "unknown")
            return original(chat_id, text, request_id, **kwargs)

        with mock.patch.object(imessage, "run_imsg_send_rpc", side_effect=flaky):
            with self.assertRaises(imessage.SendSequenceError) as caught:
                self.headless_send(
                    "--chat-id", "5",
                    "--text", "Here it comes", "--file", str(photo), "--text", "Never runs",
                )
        report = caught.exception.report
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["failed_bubble"], 2)
        self.assertEqual(report["failed_kind"], "file")
        self.assertEqual(report["never_attempted"], [3])
        self.assertEqual([row["bubble"] for row in report["sent"]], [1])
        self.assertEqual(report["parts"], ["Here it comes", "photo.png", "Never runs"])
        self.assertEqual(len(json.loads(self.fake_state.read_text())["sends"]), 1)

    def test_an_rcs_chat_sends_as_auto_because_the_runtime_has_no_rcs_service(self) -> None:
        """RCS is a real service on this Mac but not one of the runtime's three
        --service values, so it is handed back as `auto` rather than refused."""
        self.assertEqual(imessage.service_for_chat(imessage.chat_detail(7)), "auto")
        result = self.headless_send("--chat-id", "7", "--text", "Over RCS")
        self.assertEqual(result["status"], "sent_and_verified")
        self.assertEqual(self.rpc_params()[0]["service"], "auto")

    def test_a_send_with_no_parts_is_refused_before_any_backend_call(self) -> None:
        with mock.patch.object(imessage, "create_preview", side_effect=AssertionError("No preview")):
            with self.assertRaisesRegex(imessage.ImsgError, "at least one --text"):
                imessage.handle(send_args("--chat-id", "5"))
        self.assertFalse(self.fake_state.exists())

    def test_a_handle_preview_with_any_malformed_field_is_rejected(self) -> None:
        """Five guards on the new-recipient branch, each able to rot silently.

        A handle preview has no chat to revalidate against, so these ARE the
        routing checks. Every one is exercised on a record that recomputed its
        own digest, which is what a tampered preview looks like.
        """
        cases = (
            ("Malformed preview recipient handle",
             lambda payload: payload["routing"].update(handle="not a handle!!")),
            ("Malformed preview service for a new-recipient send",
             lambda payload: payload["routing"].update(service="carrier-pigeon")),
            ("Malformed preview region",
             lambda payload: payload["routing"].update(region="USA")),
            ("must not carry chat routing",
             lambda payload: payload["routing"].update(guid="iMessage;-;+12065550188")),
            ("display target does not match its routing",
             lambda payload: payload["display"].update(participants=["+12065550101"])),
        )
        for expected, mutate in cases:
            with self.subTest(expected=expected):
                preview = imessage.create_handle_preview(
                    "+12065550188", [{"kind": "text", "text": "Hi"}],
                    service="imessage", region="US",
                )
                preview_id = self.rewrite_preview(preview, mutate)
                self.assert_preview_never_reaches_approval(preview_id, expected)

    def test_a_tampered_file_part_cannot_point_at_a_credential(self) -> None:
        """The exact hole the texts/parts divergence taught us to look for.

        The path check ran when the part was collected. A hand-edited preview
        that recomputes its own digest gets past that, so the pure path rule has
        to hold again at load time -- and the name has to still match the path.
        """
        photo = self.attachment()
        cases = (
            ("credentials live",
             lambda payload: payload["parts"][0].update(path="/home/you/.ssh/id_rsa")),
            ("relative attachment path",
             lambda payload: payload["parts"][0].update(path="photo.png")),
            ("names a different file than its path",
             lambda payload: payload["parts"][0].update(name="innocent.txt")),
        )
        for expected, mutate in cases:
            with self.subTest(expected=expected):
                preview = imessage.create_preview(
                    5, parts=[dict(imessage.validate_attachment(photo), kind="file")],
                    service="imessage", headless=True,
                )
                preview_id = self.rewrite_preview(preview, mutate)
                self.assert_preview_never_reaches_approval(preview_id, expected)
        self.assertFalse(self.fake_state.exists())

    def test_an_attachment_that_vanishes_before_send_stops_the_whole_message(self) -> None:
        """Better to send nothing than to send bubble 1 and then discover the
        file is gone, which leaves a dangling sentence on somebody's phone."""
        photo = self.attachment()
        preview = imessage.create_preview(
            5,
            parts=[{"kind": "text", "text": "Look at this"},
                   dict(imessage.validate_attachment(photo), kind="file")],
            service="imessage", headless=True,
        )
        photo.unlink()
        with self.assertRaisesRegex(imessage.ImsgError, "Attachment not found"):
            imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        self.assertFalse(self.fake_state.exists(), "the text bubble must not have gone out")
        self.assertTrue(self.preview_file(preview, "invalid").is_file())

    def test_create_handle_preview_rejects_a_bad_service_or_region(self) -> None:
        parts = [{"kind": "text", "text": "Hi"}]
        with self.assertRaisesRegex(imessage.ImsgError, "Unsupported service"):
            imessage.create_handle_preview("+12065550188", parts, service="fax", region="US")
        with self.assertRaisesRegex(imessage.ImsgError, "two-letter country code"):
            imessage.create_handle_preview("+12065550188", parts, service="imessage", region="USA")

    def test_require_parts_rejects_malformed_part_shapes(self) -> None:
        cases = (
            ([{"kind": "carrier-pigeon"}], "not a text or file part"),
            ([{"kind": "file", "name": "a.png"}], "file part with no path"),
            ([{"kind": "file", "path": "/tmp/a.png"}], "file part with no name"),
            ([{"kind": "file", "path": "  ", "name": "a.png"}], "file part with no path"),
            ("not-a-list", "at least one part"),
            ([], "at least one part"),
        )
        for parts, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(imessage.ImsgError, expected):
                    imessage.require_parts(parts)

    def test_handles_match_covers_email_and_short_number_branches(self) -> None:
        """Suffix matching on digits is right for phone numbers and wrong for
        everything else, so the branches have to be asserted separately."""
        self.assertTrue(imessage.handles_match("Jane@Example.com", "jane@example.com"))
        self.assertFalse(imessage.handles_match("jane@example.com", "+12065550101"))
        # A shortcode is compared whole. Suffix matching it would collide with
        # nearly every real number.
        self.assertTrue(imessage.handles_match("50195", "50195"))
        self.assertFalse(imessage.handles_match("+12065550195", "50195"))
        self.assertFalse(imessage.handles_match("", "+12065550101"))
        # National vs E.164 is the case suffix matching exists for.
        self.assertTrue(imessage.handles_match("+12065550101", "2065550101"))
        self.assertFalse(imessage.handles_match("+12065550101", "+12065550102"))

    def test_a_lowercase_region_is_normalized_not_smuggled_through(self) -> None:
        snapshot = imessage.handle_routing_snapshot("+12065550188", "imessage", "us")
        self.assertEqual(snapshot["region"], "US")
        preview = imessage.create_handle_preview(
            "+12065550188", [{"kind": "text", "text": "Hi"}], service="imessage", region="us"
        )
        # It must survive its own validator, which requires a two-letter code.
        self.assertEqual(imessage.show_preview(preview["preview_id"])["to"], "+12065550188")


    def test_an_uncertain_send_is_never_reported_as_nothing_sent(self) -> None:
        """The most dangerous sentence this tool could print.

        "uncertain" means we cannot tell. Saying "nothing was sent" is the
        opposite, and it is how a 60 MB attachment that timed out at 20s but
        finished uploading gets sent to somebody a second time.
        """
        preview = self.prepare("Might have landed")

        def in_flight(chat_id, text, request_id, **kwargs):
            raise imessage.RpcSendError("timed out", "still_in_flight")

        with mock.patch.object(imessage, "run_imsg_send_rpc", side_effect=in_flight):
            with self.assertRaises(imessage.SendSequenceError) as caught:
                imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        message = str(caught.exception)
        self.assertEqual(caught.exception.report["status"], "uncertain")
        self.assertIn("UNCERTAIN SEND", message)
        self.assertIn("Do NOT resend", message)
        self.assertNotIn("nothing was sent", message)

    def test_a_send_that_never_started_still_says_nothing_was_sent(self) -> None:
        """The other half of the same fix: `failed` must stay unambiguous."""
        preview = self.prepare("Never dispatched")

        def dead(chat_id, text, request_id, **kwargs):
            raise imessage.RpcSendError("nothing dispatched", "not_started")

        with mock.patch.object(imessage, "run_imsg_send_rpc", side_effect=dead):
            with self.assertRaises(imessage.SendSequenceError) as caught:
                imessage.commit_preview(preview["preview_id"], preview["approval_phrase"])
        self.assertIn("nothing was sent", str(caught.exception))
        self.assertNotIn("UNCERTAIN", str(caught.exception))

    def test_a_local_number_can_never_be_matched_into_an_existing_chat(self) -> None:
        """The wrong-person send an adversarial review found.

        Open suffix matching meant `--to 555-1234` -- exactly what a model
        extracts from a transcript -- delivered to whichever of the owner's direct
        chats ended in those digits, and then verified it as a success.
        """
        self.assertFalse(imessage.handles_match("+13475550123", "5550123"))
        self.assertFalse(imessage.handles_match("+13475550123", "555-0123"))
        # A country code is not a free prefix either: a UK number can end with
        # the same ten digits as a US 206 number.
        self.assertFalse(imessage.handles_match("+44" + "2065550101", "2065550101"))
        # And the flag refuses the input outright, before any chat scan.
        with self.assertRaisesRegex(imessage.ImsgError, "10-digit number"):
            self.headless_send("--to", "555-0123", "--text", "Wrong person")
        self.assertFalse(self.fake_state.exists())

    def test_the_region_flag_is_actually_usable(self) -> None:
        """`default="US"` plus SingleValue made every `--region GB` exit 2 with
        "may only be given once", because argparse installs defaults before it
        parses. The flag existed and could not be used."""
        self.assertEqual(self.parsed("send", "--chat-id", "5", "--text", "x").region, None)
        self.assertEqual(
            self.parsed("send", "--to", "+44" + "7700900123", "--text", "x", "--region", "GB").region,
            "GB",
        )
        result = self.headless_send(
            "--to", "+44" + "7700900123", "--text", "Hello", "--region", "GB"
        )
        self.assertEqual(self.rpc_params()[0]["region"], "GB")

    def test_prepare_send_refuses_a_repeated_chat_id(self) -> None:
        """The same last-wins defect SingleValue was written for, left on the
        one command that did not get it."""
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                self.parsed("prepare-send", "--chat-id", "104", "--chat-id", "7",
                            "--text-file", "/x")

    def test_the_dialog_path_runs_the_same_computed_gate(self) -> None:
        """`prepare-send` reached commit_preview with no wording gate at all.

        The dialog shows a count and a recipient, never the words, so a human
        approving there has not necessarily read them either.
        """
        draft = self.drafts_dir() / "gated.txt"
        draft.write_text("invoice attached, $5,000 deposit due")
        draft.chmod(0o600)
        args = self.parsed("prepare-send", "--chat-id", "5", "--text-file", str(draft))
        with mock.patch.object(
            imessage, "run_draft_check", return_value=("finding", "MONEY LOCK")
        ), mock.patch.object(imessage, "create_preview", side_effect=AssertionError("No preview")):
            with self.assertRaisesRegex(imessage.ImsgError, "no preview was created"):
                imessage.handle(args)

    def test_a_private_key_is_not_a_text_bubble(self) -> None:
        """`--file key` is refused, but `--text "$(cat key)"` used to walk
        through: measured, the draft checker calls a full OPENSSH private key
        "Clean"."""
        key = "-----" + "BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
        with self.assertRaisesRegex(imessage.ImsgError, "private key block"):
            self.headless_send("--chat-id", "5", "--text", key)
        self.assertFalse(self.fake_state.exists())

    def test_a_partial_send_is_written_to_the_audit_log(self) -> None:
        """The one outcome that never wrote a line, and the one where it matters
        most: an overridden gate followed by a half-delivered message."""
        original = imessage.run_imsg_send_rpc
        attempted: list = []

        def flaky(chat_id, text, request_id, **kwargs):
            attempted.append(text)
            if len(attempted) == 2:
                raise imessage.RpcSendError("upstream refused", "unknown")
            return original(chat_id, text, request_id, **kwargs)

        with mock.patch.object(imessage, "run_draft_check", return_value=("finding", "MONEY LOCK")), \
             mock.patch.object(imessage, "run_imsg_send_rpc", side_effect=flaky):
            with self.assertRaises(imessage.SendSequenceError):
                imessage.handle(send_args(
                    "--chat-id", "5", "--text", "Lands", "--text", "Dies",
                    "--override-check", "Alex Doe approved the price line",
                ))
        entries = [
            json.loads(line)
            for line in (self.preview_state / "send-audit.jsonl").read_text().splitlines()
            if line.strip()
        ]
        self.assertEqual(entries[-1]["status"], "partial")
        self.assertEqual(entries[-1]["draft_check"], "finding")
        self.assertEqual(entries[-1]["override_reason"], "Alex Doe approved the price line")


class AttachmentGuardTests(unittest.TestCase):
    """Attachments cannot be confined to the drafts directory the way bubbles are.

    A real attachment lives on the Desktop or in a client folder, so the drafts
    rule that stops `--text-file ~/.ssh/id_ed25519` cannot apply here. What is
    left is a blocklist of the paths where a mistaken send is a credential
    disclosure rather than an embarrassment.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)

    def make(self, name: str, data: bytes = b"content") -> pathlib.Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_an_ordinary_file_is_accepted_with_the_data_needed_to_verify_it(self) -> None:
        report = imessage.validate_attachment(self.make("quote.pdf", b"%PDF-1.4"))
        self.assertEqual(report["name"], "quote.pdf")
        self.assertEqual(report["bytes"], 8)
        # An attachment carries no text, so name and size are the only things a
        # delivered history row can be matched on later.
        self.assertTrue(pathlib.Path(report["path"]).is_absolute())

    def test_credential_paths_are_refused(self) -> None:
        for name in (
            ".ssh/id_ed25519", ".ssh/id_rsa", ".aws/credentials", "prod.env",
            "server.pem", "tokens.json", "login.keychain", "secrets.yaml",
            ".gnupg/secring.gpg", ".netrc", "chat.db",
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(imessage.ImsgError, "credentials live"):
                    imessage.validate_attachment(self.make(name))

    def test_the_blocklist_is_case_insensitive_and_matches_anywhere_in_the_path(self) -> None:
        with self.assertRaisesRegex(imessage.ImsgError, "credentials live"):
            imessage.validate_attachment(self.make("Backups/ID_ED25519"))

    def test_missing_empty_oversized_and_non_regular_files_are_refused(self) -> None:
        with self.assertRaisesRegex(imessage.ImsgError, "not found"):
            imessage.validate_attachment(self.root / "nope.jpg")
        empty = self.root / "empty.png"
        empty.touch()
        with self.assertRaisesRegex(imessage.ImsgError, "empty"):
            imessage.validate_attachment(empty)
        with self.assertRaisesRegex(imessage.ImsgError, "regular file"):
            imessage.validate_attachment(self.root)
        big = self.make("big.bin", b"x")
        with mock.patch.object(imessage, "MAX_ATTACHMENT_BYTES", 0):
            with self.assertRaisesRegex(imessage.ImsgError, "ceiling"):
                imessage.validate_attachment(big)

    def test_the_files_an_adversarial_review_found_the_old_list_missing(self) -> None:
        """Every one of these exists on this Mac and passed the old blocklist."""
        for path in (
            ".config/gh/hosts.yml", ".npmrc", ".claude.json", ".zsh_history",
            ".gitconfig", "Library/Application Support/Google/Chrome/Default/Login Data",
            "Library/Application Support/Google/Chrome/Default/Cookies",
            "keys/AuthKey_ABC123.p8", ".ssh/id_dsa", "vpn/office.ovpn",
            "store.keystore", "vault.kdbx", "passwords.pdf",
        ):
            with self.subTest(path=path):
                with self.assertRaisesRegex(imessage.ImsgError, "credentials live"):
                    imessage.validate_attachment(self.make(path))

    def test_ordinary_files_that_merely_look_alarming_are_allowed(self) -> None:
        """The other direction of the same finding.

        A flat substring list refused `.envrc` and `client.envelope.pdf` because
        ".env" appears inside them, and told the caller to rename a file that was
        never a credential. A guard that cries wolf gets ignored.
        """
        for path in (
            ".envrc", "client.envelope.pdf", "keychain-diagram.png",
            "secrets-project/notes.md", "quarterly-deck.key", "my-secrets.mov",
        ):
            with self.subTest(path=path):
                report = imessage.validate_attachment(self.make(path))
                self.assertEqual(report["name"], pathlib.Path(path).name)

    def test_a_symlink_is_judged_by_where_it_actually_points(self) -> None:
        """`resolve(strict=True)` follows the link, so the blocklist sees the target.

        A symlink named holiday.jpg pointing at an SSH key must be refused for
        what it is, not accepted for what it is called.
        """
        secret = self.make("id_ed25519", b"KEY")
        link = self.root / "holiday.jpg"
        link.symlink_to(secret)
        with self.assertRaisesRegex(imessage.ImsgError, "credentials live"):
            imessage.validate_attachment(link)


class DraftGateTests(unittest.TestCase):
    """The computed gate moved from the caller into the engine on 2026-09-09.

    It moved because the caller forgot it within an hour of the headless send
    shipping: the checker printed BLOCKED and one dropped exit-code test let the
    message go anyway. These tests exist to keep that failure impossible rather
    than merely unlikely, so every arm here asserts that NOTHING was sent.
    """

    SECRET = "PRIVATE_DRAFT_SENTINEL"

    def setUp(self) -> None:
        # The real checker is a separate module; these tests only need a file that exists.
        self.checker_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.checker_dir.cleanup)
        checker = Path(self.checker_dir.name) / "draft_check.py"
        checker.write_text("print('stub')\n")
        patcher = mock.patch.object(imessage, "DRAFT_CHECKER", checker)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_run(self, returncode: int, stdout: str = "", stderr: str = ""):
        captured: dict[str, object] = {}

        def runner(command, **kwargs):
            captured["command"] = list(command)
            captured["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(command, returncode, stdout, stderr)

        return runner, captured

    def test_a_clean_message_reports_clean_and_really_ran_the_checker(self) -> None:
        runner, captured = self.fake_run(0, "Clean. Safe to preview, then send.\n")
        with mock.patch.object(imessage.subprocess, "run", runner):
            verdict, detail = imessage.run_draft_check(["hello"])
        self.assertEqual(verdict, "clean")
        self.assertIn("Clean.", detail)
        # Without this the test would still pass against a gutted `return`.
        self.assertIn(str(imessage.DRAFT_CHECKER), captured["command"])

    def test_a_blocked_message_reports_a_finding_and_carries_it(self) -> None:
        runner, _ = self.fake_run(1, "BLOCKED. 4 prose bubbles, ceiling is 3.\n")
        with mock.patch.object(imessage.subprocess, "run", runner):
            verdict, detail = imessage.run_draft_check(["a", "b", "c", "d"])
        self.assertEqual(verdict, "finding")
        self.assertIn("ceiling is 3", detail)

    def test_a_finding_stops_the_send_when_there_is_no_override(self) -> None:
        args = send_args("--chat-id", "5", "--text", "a")
        with mock.patch.object(
            imessage, "run_draft_check", return_value=("finding", "ceiling is 3")
        ), mock.patch.object(
            imessage, "create_preview", side_effect=AssertionError("No preview")
        ), mock.patch.object(
            imessage, "commit_preview", side_effect=AssertionError("No send")
        ):
            with self.assertRaises(imessage.ImsgError) as error:
                imessage.handle(args)
        message = str(error.exception)
        self.assertIn("nothing was sent", message)
        self.assertIn("--override-check", message)

    def test_a_missing_checker_fails_closed(self) -> None:
        absent = pathlib.Path("/private/tmp/no-such-draft-check-8d3f.py")
        self.assertFalse(absent.exists())
        with mock.patch.object(imessage, "DRAFT_CHECKER", absent):
            with self.assertRaises(imessage.ImsgError) as error:
                imessage.run_draft_check(["hello"])
        self.assertIn("missing", str(error.exception))

    def test_a_broken_checker_fails_closed_whatever_its_exit_code(self) -> None:
        """draft_check.py has no try/except, so a crash leaves through exit 1.

        That is the same code a real finding uses, so before this was separated
        a broken gate arrived as "refused, you may override" -- an invitation to
        wave through the exact case that must never be waved through.
        """
        crash = "Traceback (most recent call last):\nValueError\n"
        for code, out, err in ((2, "", "boom\n"), (1, "", crash), (7, "", "")):
            with self.subTest(code=code):
                runner, _ = self.fake_run(code, out, err)
                with mock.patch.object(imessage.subprocess, "run", runner):
                    with self.assertRaises(imessage.ImsgError) as error:
                        imessage.run_draft_check(["hello"])
                message = str(error.exception)
                self.assertIn("could not answer", message)
                self.assertIn("nothing was sent", message)
                # Deliberately not mentioned: a broken gate is repaired, never
                # sent around, so the error must not advertise the escape hatch.
                self.assertNotIn("--override-check", message)

    def test_the_checker_times_out_rather_than_hanging_a_send(self) -> None:
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="draft_check.py", timeout=30)

        with mock.patch.object(imessage.subprocess, "run", timeout):
            with self.assertRaises(imessage.ImsgError) as error:
                imessage.run_draft_check(["hello"])
        self.assertIn("could not run", str(error.exception))

    def test_the_checker_never_sees_message_text_in_argv(self) -> None:
        runner, captured = self.fake_run(0)
        with mock.patch.object(imessage.subprocess, "run", runner):
            imessage.run_draft_check([self.SECRET])
        command = captured["command"]
        self.assertNotIn(self.SECRET, " ".join(str(part) for part in command))
        self.assertTrue(str(command[-1]).endswith("bubble1.txt"))
        self.assertNotIn("PYTHONPATH", captured["env"] or {})

    def test_the_headless_send_runs_the_gate_before_building_anything(self) -> None:
        args = send_args("--chat-id", "5", "--text", "over budget")
        with mock.patch.object(
            imessage, "run_draft_check", side_effect=imessage.ImsgError("gate said no")
        ), mock.patch.object(
            imessage, "create_preview", side_effect=AssertionError("No preview")
        ), mock.patch.object(
            imessage, "commit_preview", side_effect=AssertionError("No send")
        ):
            with self.assertRaises(imessage.ImsgError):
                imessage.handle(args)

    def test_an_override_waives_a_finding_and_is_recorded_in_the_result(self) -> None:
        args = send_args(
            "--chat-id", "5", "--text", "hello", "--override-check", "Alex Doe said go"
        )
        with mock.patch.object(
            imessage, "run_draft_check", return_value=("finding", "money lock")
        ), mock.patch.object(
            imessage, "create_preview",
            # A realistic stub. `target` is what names who the message reached,
            # and a stub without it hid a crash in the dispatcher.
            return_value={"preview_id": "abc123", "target": {"participants": ["+1555"]}},
        ), mock.patch.object(
            imessage, "commit_preview", return_value={"status": "sent_and_verified"}
        ), mock.patch.object(imessage, "append_send_audit"):
            value, ok = imessage.handle(args)
        self.assertTrue(ok)
        self.assertEqual(value["draft_check"], "overridden: Alex Doe said go")

    def test_an_override_can_never_waive_an_unanswered_gate(self) -> None:
        """The exact hole two independent reviewers found on 2026-09-09.

        `--override-check` used to skip run_draft_check entirely, so it also
        skipped the missing-checker and crashed-checker refusals that its own
        help text promised it could not skip. The gate must run FIRST, every
        time, and only its verdict is a thing a human may overrule.
        """
        for failure in ("The computed draft gate is missing at /nope",
                        "The computed draft gate could not answer (exit 1)"):
            with self.subTest(failure=failure):
                args = send_args(
                    "--chat-id", "5", "--text", "hello",
                    "--override-check", "Alex Doe approved this",
                )
                with mock.patch.object(
                    imessage, "run_draft_check", side_effect=imessage.ImsgError(failure)
                ), mock.patch.object(
                    imessage, "create_preview", side_effect=AssertionError("No preview")
                ), mock.patch.object(
                    imessage, "commit_preview", side_effect=AssertionError("No send")
                ):
                    with self.assertRaises(imessage.ImsgError) as error:
                        imessage.handle(args)
                self.assertIn("gate", str(error.exception))

    def test_a_reason_too_short_to_act_on_is_not_an_override(self) -> None:
        for reason in ("", "   ", "ok"):
            with self.subTest(reason=reason):
                args = send_args(
                    "--chat-id", "5", "--text", "hello", "--override-check", reason
                )
                with mock.patch.object(
                    imessage, "create_preview", side_effect=AssertionError("No preview")
                ), mock.patch.object(
                    imessage, "commit_preview", side_effect=AssertionError("No send")
                ):
                    with self.assertRaises(imessage.ImsgError) as error:
                        imessage.handle(args)
                self.assertIn("real reason", str(error.exception))


if __name__ == "__main__":
    unittest.main()
