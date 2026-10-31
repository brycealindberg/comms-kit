"""Offline SIMULATED samples. This is not a connectivity or live-delivery check."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch

CODE = Path(os.path.abspath(__file__)).parent  # kit: code path


def find(*paths):
    """The first of these programs that exists, else None. A check whose program is not in this
    install is reported as SKIP, never as a pass."""
    for path in paths:
        if Path(path).exists():
            return Path(path)
    return None


# programs that live beside the core in the repo (channels/) or under ~/.claude (the live layout)
HOME_DIR = Path(os.path.expanduser("~"))
GATE_HOOK = find(CODE.parent / "hooks/outbound-gate-hook.py")
TOOLXML = find(CODE.parent / "scripts/block-toolxml-in-outbound.py")
WA_CLI = find(CODE.parent / "channels/whatsapp/whatsapp_mautrix.py",
              CODE.parent / "skills/whatsapp/whatsapp_mautrix.py")
SLACK_JS = find(CODE.parent / "channels/slack/scripts/slack-thread-read.js",
                CODE.parent / "skills/slack-bridge/scripts/slack-thread-read.js")
IMSG_CLI = find(CODE.parent / "channels/imessage/imessage.py",
                HOME_DIR / "pi-2/agent/skills/imessage/imessage.py",
                HOME_DIR / ".pi-2/agent/skills/imessage/imessage.py")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def run():
    """Return PASS/FAIL results, with every Python network boundary disabled."""
    F = load("_selftest_fixtures", CODE / "p0_fixtures.py")
    results = []
    with tempfile.TemporaryDirectory(prefix="comms-selftest-") as tmp:
        home = F.make_comms_home(tmp)
        registry = Path(tmp) / "developers"
        hidden = registry / "fixture-dev"
        hidden.mkdir(parents=True)
        (hidden / "profile.json").write_text(json.dumps({
            "slug": "fixture-dev", "name": "Casey", "identity_patterns": [r"\bCasey\b"]}))
        env = {"HOME": home, "GATE_MODE": "enforce", "DEV_REGISTRY_DIR": str(registry),
               "DEV_GUARD_ACK_LOG": str(Path(tmp) / "acks"), "DEV_OK": "", **{name: "" for name in os.environ if name.endswith("_OK")},
               "CLAUDECODE": "", "CLAUDE_CODE_SESSION_ID": "", "COMMS_INTERNAL": "",
               "COMMS_KIT_CONFIG": F.fixture_config_path(home)}
        with patch.dict(os.environ, env), patch.object(socket.socket, "connect",
                                                       side_effect=AssertionError("network forbidden")):
            G = load("_selftest_gate", CODE / "outbound_gate.py")
            C = load("_selftest_log", CODE / "commslib.py")
            S = load("_selftest_schema", CODE / "msgschema.py")

            def check(name, fn, needs=()):
                missing = [n for n, found in needs if not found]
                if missing:
                    results.append((name, None, "not installed: " + ", ".join(missing)))
                    return
                try:
                    fn()
                    results.append((name, True, "SIMULATED"))
                except Exception as exc:
                    results.append((name, False, "%s: %s" % (type(exc).__name__, exc)))

            def gate():
                p = G.OutboundPayload("whatsapp", "fixture", ["hello from the test"],
                                      recipients=["+12065550123"])
                require(G.evaluate(p).allow, "clean fixture refused")
                op = G.begin(p)
                G.finish(op, "uncertain")
                require(not G.evaluate(p).allow, "uncertain send retry allowed")
                p.texts = ["hello " + "<" + "/invoke" + ">"]
                require(G.evaluate(p).checks["markup"] == "finding", "markup missed")
                p.texts = ["deposit is $500"]
                require(G.evaluate(p).checks["money"] == "finding", "money missed")
                if not G.dev_guard_is_optional():
                    p.texts = ["Casey can handle this"]
                    require(G.evaluate(p).checks["dev"] == "finding", "hidden name missed")
                with patch.object(G, "_check_style_and_money", side_effect=OSError("fixture")):
                    require(G.evaluate(p).checks["style"] == "error", "check failure allowed")
            check("gate: clean/markup/money/dev/uncertain/error", gate,
                  needs=[("draft_check", Path(G.draft_check_path()).exists())])

            def external():
                H = load("_selftest_hook", GATE_HOOK)
                reason = H.decide({"tool_name": "mcp__upwork__upwork__send_message",
                                   "tool_input": {"action": "send", "params": {
                                       "room_id": "fixture", "message": "<" + "/invoke" + ">"}}})
                require(reason and "markup" in reason, "nested Upwork markup missed")
                body = Path(tmp) / "bad.txt"
                body.write_text("<" + "/invoke" + ">")
                X = load("_selftest_toolxml", TOOLXML)
                candidates = X.candidates("Bash", {"command":
                    "imessage send --chat-id 104 --text-file " + str(body)}, tmp)
                require(any(X.offending(text) for _, text in candidates), "body file missed")
            check("gate: Upwork nested body and body file", external,
                  needs=[("the outbound-gate hook", GATE_HOOK), ("block-toolxml-in-outbound", TOOLXML)])

            def logging():
                require(C.log_event("whatsapp", "send", "fixture", text=F.SENTINEL,
                                    message_id="$selftest"), "CLI archive failed")
                require(len(F.comms_rows(home)) == 1, "expected one CLI row")
                require(C.receipts_known(C.receipts_in_body("whatsapp",
                                                          "Sent. event_id=$selftest")),
                        "hook cannot recognize CLI receipt")
            check("logging: one CLI row and hook receipt", logging)

            def whatsapp():
                W = load("_selftest_wa", WA_CLI)
                args = argparse.Namespace(identifier="fixture", number=30, full=True,
                                          before=None, download=None, no_transcribe=False, ids=False)
                with contextlib.ExitStack() as stack:
                    for name, value in {
                        "_request": F.FakeMatrix(),
                        "_require_session": lambda: ("fixture", "!management"),
                        "_resolve_chat": lambda *a: {"portal_id": F.ROOM, "label": "Fixture", "is_group": False},
                        "_self_number": lambda *a: F.SELF_NUMBER,
                        "_transcribe_event": lambda *a, **k: "simulated transcript",
                    }.items():
                        stack.enter_context(patch.object(W, name, value))
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        W.cmd_history(args)
                doc = json.loads(out.getvalue())
                require(not S.validate(doc), "schema invalid")
                require(F.LONG in [m["text"] for m in doc["messages"]], "long message cut")
                require(F.HOWTO in [m["text"] for m in doc["messages"]], "long-text window cut")
            check("WhatsApp: golden full-read fixture", whatsapp, needs=[("the WhatsApp CLI", WA_CLI)])

            def slack():
                bridge_env = F.write_fake_bridge(str(Path(tmp) / "bridge"))
                script = SLACK_JS
                proc = subprocess.run(
                    ["node", "-", F.SLACK_CHANNEL, "fixture", "30", "full"],
                    input=script.read_text(), text=True, capture_output=True, timeout=30,
                    env=dict(os.environ, **bridge_env))
                require(proc.returncode == 0, "fake bridge reader failed")
                rows = json.loads(proc.stdout)["messages"]
                require(F.LONG in [m["text"] for m in rows], "long message cut")
                require(len([m for m in rows if m["thread_ts"]]) == 150, "thread replies cut")
            check("Slack: fake Node bridge, all 150 replies", slack,
                  needs=[("the Slack reader script", SLACK_JS), ("node", shutil.which("node"))])

            def imessage():
                I = load("_selftest_imsg", IMSG_CLI)
                args = argparse.Namespace(limit=20, chat_id=104, since=None, until=None,
                                          participant=[], before_row=None)
                chat = {"id": 104, "name": "Fixture", "is_group": False, "service": "iMessage",
                        "participants": ["+12065550123"]}
                # The owner's pinned wrapper has COMMS_DIR. The kit's copy finds core by itself.
                with patch.object(I, "COMMS_DIR", Path(home) / ".claude/communications", create=True), \
                        patch.object(I, "history_rows", return_value=F.imessage_rows()), \
                        patch.object(I, "run_imsg", side_effect=AssertionError("live binary forbidden")):
                    doc = I.history_full(args, chat)
                require(not S.validate(doc), "schema invalid")
                require(doc["count"] == 6, "reactions counted as messages")
                require(F.LONG in [m["text"] for m in doc["messages"]], "long message cut")
            check("iMessage: golden full-read fixture", imessage, needs=[("the iMessage CLI", IMSG_CLI)])
    return results


def main():
    results = run()
    print("Offline comms selftest — SIMULATED, no live sends or connectivity proof")
    for name, ok, detail in results:
        print("%s %s: %s" % ({True: "PASS", None: "SKIP"}.get(ok, "FAIL"), name, detail))
    return 0 if all(ok is not False for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
