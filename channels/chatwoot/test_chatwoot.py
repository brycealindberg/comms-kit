#!/usr/bin/env python3
"""Tests for chatwoot.py against a real local HTTP server on 127.0.0.1 (random port).

The real urllib code runs: headers, status codes, timeouts, dropped connections, redirects and
paging are all real. Nothing leaves 127.0.0.1. No account, no real Chatwoot.

Failure modes covered (written before the code):
  1. the token is missing from a request, sent in the wrong header, or printed anywhere
  2. a 401 or 500 prints a traceback or exits 0
  3. a redirect is followed, so the token goes to another host
  4. the token goes out over plain http to a real host
  5. a write runs without --yes
  6. --yes without --since still sends
  7. a newer message (from the customer, a reply to an old one, a private note) lets the send through
  8. a message with no readable time is ignored
  9. the money lock is skipped, or a money check that cannot run lets the text through
 10. a refusing outbound gate still lets the send through
 11. a timeout, a dropped connection or a 5xx on a send is retried, exits 0, or archives a row
 12. a clean 4xx refusal archives a row or exits 0
 13. a good send archives zero or two rows, or hook_covers is not False
 14. a private note reaches the customer (private must be true), or is archived as a send
 15. paging stops early, repeats rows, or never ends
 16. an id with a slash in it changes the request path
 17. a conversation that cannot be replied to still gets a reply
"""
import contextlib
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import chatwoot as C  # noqa: E402

TOKEN = "-".join(["tok", "FAKE", "TOKEN", "not", "real"])
T_OLD = 1_790_000_000
T_NEW = T_OLD + 60
ACCOUNT = "1"
BASE = f"/api/v1/accounts/{ACCOUNT}"


def cm(mid, ts, mtype=0, content="hello", private=False, in_reply_to=None, sender="Alex Doe"):
    """A Chatwoot message. message_type: 0 incoming, 1 outgoing, 2 activity, 3 template."""
    return {"id": mid, "content": content, "message_type": mtype, "created_at": ts, "private": private,
            "content_attributes": {"in_reply_to": in_reply_to} if in_reply_to else {},
            "sender": {"name": sender}}


def cv(cid, unread=0, name="Alex Doe", status="open", last="hi", ts=T_OLD):
    return {"id": cid, "status": status, "unread_count": unread, "inbox_id": 4, "last_activity_at": ts,
            "meta": {"sender": {"name": name, "email": "alex@example.com"}, "channel": "Channel::Email"},
            "messages": [cm(cid * 10, ts, content=last)]}


def detail(cid, can_reply=True, email="alex@example.com", phone=None):
    return {"id": cid, "status": "open", "can_reply": can_reply, "unread_count": 1,
            "meta": {"sender": {"name": "Alex Doe", "email": email, "phone_number": phone},
                     "channel": "Channel::Email"}}


class FakeChatwoot:
    """A real HTTP server that records every request."""

    def __init__(self):
        self.requests = []
        self.overrides = []
        self.list_pages = []
        self.details = {7: detail(7)}
        self.messages = {7: [cm(70, T_OLD - 30, 1, "earlier from us"), cm(71, T_OLD, 0, "Can you send the details?")]}
        self.msg_page = 20
        self.token = TOKEN
        self.post_response = {"id": 99, "content": "ok", "created_at": T_NEW, "private": False}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, obj, headers=None):
                raw = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method):
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                outer.requests.append({"method": method, "path": parsed.path, "query": parse_qs(parsed.query),
                                       "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                for ov in outer.overrides:
                    if ov["method"] == method and ov["match"] in parsed.path:
                        if ov.get("delay"):
                            time.sleep(ov["delay"])
                        if ov.get("drop"):
                            self.close_connection = True
                            return
                        if ov.get("status"):
                            return self._reply(ov["status"], ov.get("body", {}), ov.get("headers"))
                if self.headers.get("api_access_token") != outer.token:
                    return self._reply(401, {"error": "Invalid Access Token"})
                path = parsed.path
                q = parse_qs(parsed.query)
                if method == "GET" and path == f"{BASE}/conversations":
                    page = int(q.get("page", ["1"])[0])
                    rows = outer.list_pages[page - 1] if 0 < page <= len(outer.list_pages) else []
                    return self._reply(200, {"data": {"meta": {"all_count": 99}, "payload": rows}})
                m = re.fullmatch(rf"{BASE}/conversations/(\d+)", path)
                if method == "GET" and m:
                    d = outer.details.get(int(m.group(1)))
                    return self._reply(200, d) if d else self._reply(404, {"error": "Resource could not be found"})
                m = re.fullmatch(rf"{BASE}/conversations/(\d+)/messages", path)
                if method == "GET" and m:
                    rows = outer.messages.get(int(m.group(1)))
                    if rows is None:
                        return self._reply(404, {"error": "Resource could not be found"})
                    if "before" in q:
                        rows = [r for r in rows if r["id"] < int(q["before"][0])]
                    return self._reply(200, {"meta": {}, "payload": rows[-outer.msg_page:]})
                if method == "POST" and m:
                    return self._reply(200, outer.post_response)
                if method == "POST" and re.fullmatch(rf"{BASE}/conversations/\d+/(toggle_status|update_last_seen)", path):
                    return self._reply(200, {"payload": {"success": True, "current_status": "resolved"}})
                return self._reply(404, {"error": "no route"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def override(self, method, match, **kw):
        self.overrides.append({"method": method, "match": match, **kw})

    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]

    def message_posts(self):
        return [r for r in self.posts() if r["path"].endswith("/messages")]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeGate:
    class GateDenied(Exception):
        pass

    class Approvals:
        @classmethod
        def from_env(cls, **kw):
            return cls()

    class OutboundPayload:
        def __init__(self, channel, **kw):
            self.channel, self.kw = channel, kw

    def __init__(self, allow=True):
        self.allow, self.finished, self.begun = allow, [], 0

    def evaluate(self, payload, approvals, dry_run=False, source=""):
        gate = self

        class V:
            allow = gate.allow

            def deny_line(self):
                return "NOT SENT: outbound gate refused."
        return V()

    def begin(self, payload, approvals):
        self.begun += 1
        return "op-1"

    def finish(self, op, status, receipts=()):
        self.finished.append((op, status, list(receipts)))


class FakeComms:
    def __init__(self):
        self.rows = []

    def log_event(self, *a, **kw):
        self.rows.append((a, kw))
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.srv = FakeChatwoot()
        self.addCleanup(self.srv.stop)
        self.gate = FakeGate()
        self.comms = FakeComms()
        self.money = []
        self.conf = {"chatwoot.base_url": self.srv.url, "chatwoot.account_id": ACCOUNT,
                     "chatwoot.token_command": ["fake-token-cmd"]}
        mods = {"outbound_gate": self.gate, "commslib": self.comms}
        patches = [
            mock.patch.object(C, "_conf", lambda key, default=None: self.conf.get(key, default)),
            mock.patch.object(C, "_run_key_cmd", lambda cmd: TOKEN),
            mock.patch.object(C, "_comms_module", lambda name: mods.get(name)),
            mock.patch.object(C, "_money_hits", lambda text: list(self.money)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                C.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    def reply(self, *extra, text="Happy to help, here is more.", since=str(T_OLD), cid="7"):
        args = ["reply", cid, text]
        if since:
            args += ["--since", since]
        return self.run_cli(*args, *extra)


class Http(Base):
    def test_every_request_carries_the_token_in_the_right_header_only(self):
        self.srv.list_pages = [[cv(7, 1)]]
        self.run_cli("conversations")
        self.run_cli("thread", "7")
        self.reply("--yes")
        self.assertGreaterEqual(len(self.srv.requests), 5)
        for r in self.srv.requests:
            self.assertEqual(r["headers"].get("api_access_token"), TOKEN)
            self.assertNotIn("authorization", r["headers"])
            self.assertNotIn("access_token", r["path"] + json.dumps(r["query"]))

    def test_the_token_never_appears_in_any_output(self):
        self.srv.override("GET", "/conversations", status=401, body={"error": "bad token " + TOKEN})
        self.srv.override("POST", "/messages", status=500, body={"error": TOKEN})
        runs = [self.run_cli("conversations"), self.run_cli("thread", "7"), self.reply(), self.reply("--yes")]
        for code, out, err in runs:
            self.assertNotIn(TOKEN, out + err)

    def test_401_on_a_read_is_a_plain_setup_line(self):
        self.srv.token = "something-else"
        code, out, err = self.run_cli("conversations")
        self.assertEqual(code, 2)
        self.assertIn("token", (out + err).lower())
        self.assertNotIn("Traceback", out + err)

    def test_401_on_a_send_is_not_sent_and_not_archived(self):
        self.srv.override("POST", "/messages", status=401, body={})
        code, out, err = self.reply("--yes")
        self.assertEqual(code, 2)
        self.assertIn("NOT SENT", out + err)
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.finished[-1][1], "failed_before_send")

    def test_500_on_a_read_is_a_failure(self):
        self.srv.override("GET", "/conversations", status=500, body={})
        code, out, err = self.run_cli("conversations")
        self.assertEqual(code, 1)
        self.assertIn("500", out + err)
        self.assertNotIn("Traceback", out + err)

    def test_404_on_a_read_is_a_failure(self):
        code, out, err = self.run_cli("thread", "12345")
        self.assertEqual(code, 1)

    def test_a_read_timeout_is_a_failure_not_a_hang(self):
        self.srv.override("GET", "/conversations", delay=3, status=200, body={"data": {"payload": []}})
        with mock.patch.object(C, "READ_TIMEOUT", 1):
            started = time.time()
            code, out, err = self.run_cli("conversations")
        self.assertEqual(code, 1)
        self.assertLess(time.time() - started, 3)
        self.assertNotIn("Traceback", out + err)

    def test_a_redirect_is_not_followed_and_the_token_goes_nowhere_else(self):
        other = FakeChatwoot()
        self.addCleanup(other.stop)
        self.srv.override("GET", "/conversations", status=302, headers={"Location": other.url + "/steal"}, body={})
        code, out, err = self.run_cli("conversations")
        self.assertNotEqual(code, 0)
        self.assertEqual(other.requests, [])

    def test_plain_http_to_a_real_host_is_refused_before_any_connection(self):
        self.conf["chatwoot.base_url"] = "http://chatwoot.example.com"
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("connected")):
            code, out, err = self.run_cli("conversations")
        self.assertEqual(code, 2)
        self.assertIn("https", out + err)

    def test_https_and_loopback_http_are_the_only_accepted_urls(self):
        for url in ("https://chatwoot.example.com", "https://app.chatwoot.example.com/", "http://127.0.0.1:3000",
                    "http://localhost:3000"):
            self.assertTrue(C.base_url_ok(url), url)
        for url in ("http://chatwoot.example.com", "ftp://x", "chatwoot.example.com", "", "https://", "file:///etc"):
            self.assertFalse(C.base_url_ok(url), url)

    def test_trailing_slash_in_the_base_url_does_not_double_the_path(self):
        self.conf["chatwoot.base_url"] = self.srv.url + "/"
        self.srv.list_pages = [[cv(7)]]
        code, _, _ = self.run_cli("conversations")
        self.assertEqual(code, 0)
        self.assertTrue(self.srv.requests[0]["path"].startswith("/api/v1/accounts/1/conversations"))

    def test_bad_ids_never_change_the_path(self):
        for bad in ("7/../x", "abc", "7?x=1", "-1", ""):
            code, _, _ = self.run_cli("thread", bad)
            self.assertEqual(code, 2, bad)
        self.conf["chatwoot.account_id"] = "1/../2"
        code, _, _ = self.run_cli("conversations")
        self.assertEqual(code, 2)
        self.assertEqual(self.srv.requests, [])

    def test_missing_settings_exit_2_and_name_the_key(self):
        for key in ("chatwoot.base_url", "chatwoot.account_id", "chatwoot.token_command"):
            saved = self.conf.pop(key)
            code, out, err = self.run_cli("conversations")
            self.assertEqual(code, 2, key)
            self.assertIn(key, out + err)
            self.conf[key] = saved

    def test_failed_token_command_hides_the_output(self):
        script = "import sys; sys.stderr.write('%s'); sys.exit(3)" % TOKEN
        self.conf["chatwoot.token_command"] = [sys.executable, "-c", script]
        with mock.patch.object(C, "_run_key_cmd", C._real_run_key_cmd):
            code, out, err = self.run_cli("conversations")
        self.assertEqual(code, 2)
        self.assertNotIn(TOKEN, out + err)
        self.assertEqual(self.srv.requests, [])

    def test_token_command_runs_without_shell(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(kw, cmd=cmd)
            return mock.Mock(returncode=0, stdout=TOKEN + "\n", stderr="")
        with mock.patch("subprocess.run", fake_run):
            self.assertEqual(C._real_run_key_cmd(["your-secret-tool", "X"]), TOKEN)
        self.assertFalse(seen.get("shell", False))


class Paging(Base):
    def test_conversations_pages_until_an_empty_page(self):
        self.srv.list_pages = [[cv(1), cv(2)], [cv(3), cv(4)], [cv(5)]]
        code, out, _ = self.run_cli("conversations", "-n", "50")
        self.assertEqual(code, 0)
        for cid in (1, 2, 3, 4, 5):
            self.assertIn(f"conv={cid} ", out)
        self.assertEqual(out.count("conv=3 "), 1)
        pages = [r["query"]["page"][0] for r in self.srv.requests]
        self.assertEqual(pages, ["1", "2", "3", "4"])

    def test_n_stops_paging_early(self):
        self.srv.list_pages = [[cv(1), cv(2)], [cv(3), cv(4)], [cv(5)]]
        code, out, _ = self.run_cli("conversations", "-n", "3")
        self.assertEqual(out.count("conv="), 3)
        self.assertEqual(len(self.srv.requests), 2)

    def test_paging_has_a_ceiling_when_the_server_never_ends(self):
        self.srv.list_pages = [[cv(i)] for i in range(1, 100)]
        with mock.patch.object(C, "MAX_PAGES", 3):
            code, out, _ = self.run_cli("conversations", "-n", "100")
        self.assertEqual(len(self.srv.requests), 3)
        self.assertEqual(code, 0)

    def test_list_asks_for_status_and_assignee(self):
        self.srv.list_pages = [[cv(1)]]
        self.run_cli("conversations", "--status", "resolved", "--assignee", "all")
        q = self.srv.requests[0]["query"]
        self.assertEqual(q["status"], ["resolved"])
        self.assertEqual(q["assignee_type"], ["all"])

    def test_bad_status_is_refused_before_any_request(self):
        code, _, _ = self.run_cli("conversations", "--status", "bogus")
        self.assertEqual(code, 2)
        self.assertEqual(self.srv.requests, [])

    def test_unread_keeps_only_unread_conversations_across_pages(self):
        self.srv.list_pages = [[cv(1, 0), cv(2, 3)], [cv(3, 0), cv(4, 1)]]
        code, out, _ = self.run_cli("unread")
        self.assertEqual(code, 0)
        self.assertIn("conv=2 ", out)
        self.assertIn("conv=4 ", out)
        self.assertNotIn("conv=1 ", out)
        self.assertNotIn("conv=3 ", out)

    def test_unread_with_nothing_says_so(self):
        code, out, _ = self.run_cli("unread")
        self.assertEqual(code, 0)
        self.assertIn("No unread", out)

    def test_find_sends_the_text_as_q_over_every_status(self):
        self.srv.list_pages = [[cv(2, 0, last="the refund")]]
        code, out, _ = self.run_cli("find", "refund")
        self.assertEqual(code, 0)
        q = self.srv.requests[0]["query"]
        self.assertEqual(q["q"], ["refund"])
        self.assertEqual(q["status"], ["all"])
        self.assertIn("conv=2 ", out)

    def test_thread_shows_the_latest_page_oldest_first(self):
        self.srv.messages[7] = [cm(i, T_OLD + i, 0, f"line{i}") for i in range(1, 6)]
        self.srv.msg_page = 3
        code, out, _ = self.run_cli("thread", "7")
        self.assertEqual(code, 0)
        self.assertLess(out.index("line3"), out.index("line5"))
        self.assertNotIn("line2", out)

    def test_thread_all_walks_back_with_before_until_empty_without_repeats(self):
        self.srv.messages[7] = [cm(i, T_OLD + i, 0, f"line{i}") for i in range(1, 8)]
        self.srv.msg_page = 3
        code, out, _ = self.run_cli("thread", "7", "--all")
        self.assertEqual(code, 0)
        positions = [out.index(f"line{i}") for i in range(1, 8)]
        self.assertEqual(positions, sorted(positions))
        for i in range(1, 8):
            self.assertEqual(out.count(f"line{i}\n") + out.count(f"line{i} "), 1, i)
        befores = [r["query"]["before"][0] for r in self.srv.requests if "before" in r["query"]]
        self.assertEqual(befores, ["5", "2", "1"])

    def test_thread_labels_notes_and_activity(self):
        self.srv.messages[7] = [cm(1, T_OLD, 1, "hidden note", private=True), cm(2, T_OLD + 1, 2, "assigned"),
                                cm(3, T_OLD + 2, 1, "public reply")]
        code, out, _ = self.run_cli("thread", "7")
        self.assertRegex(out, r"\[NOTE\][^\n]*\n[^\n]*hidden note")
        self.assertRegex(out, r"\[ACTIVITY\][^\n]*\n[^\n]*assigned")
        self.assertRegex(out, r"\[OUT\][^\n]*\n[^\n]*public reply")


class ReplyGuards(Base):
    def test_dry_run_posts_nothing_and_shows_who_gets_it(self):
        code, out, _ = self.reply()
        self.assertEqual(code, 0)
        self.assertEqual(self.srv.posts(), [])
        for needle in ("DRY RUN", "alex@example.com", "Happy to help", "Channel::Email"):
            self.assertIn(needle, out)
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.begun, 0)

    def test_yes_without_since_exits_2_before_any_request(self):
        code, out, err = self.reply("--yes", since=None)
        self.assertEqual(code, 2)
        self.assertIn("--since", out + err)
        self.assertEqual(self.srv.requests, [])

    def test_newer_message_from_the_customer_exits_5(self):
        self.srv.messages[7].append(cm(72, T_NEW, 0, "wait, one more thing"))
        code, out, _ = self.reply("--yes")
        self.assertEqual(code, 5)
        self.assertIn("NOT SENT:", out)
        self.assertEqual(self.srv.message_posts(), [])
        self.assertEqual(self.comms.rows, [])

    def test_newer_message_that_replies_to_an_old_one_still_counts(self):
        self.srv.messages[7].append(cm(72, T_NEW, 0, "re: your first line", in_reply_to=70))
        code, _, _ = self.reply("--yes")
        self.assertEqual(code, 5)
        self.assertEqual(self.srv.message_posts(), [])

    def test_a_newer_private_note_blocks_on_purpose(self):
        # a teammate's note can say "do not reply", so it must stop the send
        self.srv.messages[7] = [cm(71, T_OLD, 0, "hi"), cm(72, T_NEW, 1, "note from a teammate", private=True)]
        code, out, _ = self.reply("--yes")
        self.assertEqual(code, 5)
        self.assertIn("NOT SENT:", out)
        self.assertEqual(self.srv.message_posts(), [])

    def test_a_newer_activity_row_does_not_block(self):
        # Chatwoot's own rows (assignment, label, status) are not something a person said
        for text in ("Alex Doe was assigned to Sam Roe", "added the label billing", "reopened"):
            self.srv.requests.clear()
            self.srv.messages[7] = [cm(71, T_OLD, 0, "hi"), cm(72, T_NEW, 2, text)]
            code, out, _ = self.reply("--yes")
            self.assertEqual(code, 0, text)
            self.assertEqual(len(self.srv.message_posts()), 1, text)

    def test_only_activity_rows_on_the_latest_page_pass(self):
        self.srv.messages[7] = [cm(72, T_NEW, 2, "assigned"), cm(73, T_NEW + 5, 2, "labelled", )]
        self.srv.messages[7][1]["created_at"] = None        # an activity row with no time never blocks
        code, _, _ = self.reply("--yes")
        self.assertEqual(code, 0)

    def test_activity_does_not_hide_a_newer_customer_message_behind_it(self):
        self.srv.messages[7] = [cm(71, T_OLD, 0, "hi"), cm(72, T_NEW, 0, "one more thing"),
                                cm(73, T_NEW + 5, 2, "assigned")]
        code, _, _ = self.reply("--yes")
        self.assertEqual(code, 5)
        self.assertEqual(self.srv.message_posts(), [])

    def test_message_at_exactly_since_is_not_newer(self):
        code, _, _ = self.reply("--yes")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.srv.message_posts()), 1)

    def test_message_with_no_time_is_refused_not_ignored(self):
        broken = cm(72, None)
        self.srv.messages[7].append(broken)
        code, _, _ = self.reply("--yes")
        self.assertEqual(code, 5)
        self.assertEqual(self.srv.message_posts(), [])

    def test_since_accepts_iso_and_refuses_junk(self):
        code, _, _ = self.reply("--yes", since="2020-01-01T00:00:00Z")
        self.assertEqual(code, 5)
        code, _, _ = self.reply("--yes", since="yesterday")
        self.assertEqual(code, 2)
        self.assertEqual(self.srv.message_posts(), [])

    def test_empty_text_is_refused(self):
        code, _, _ = self.reply("--yes", text="  ")
        self.assertEqual(code, 1)
        self.assertEqual(self.srv.requests, [])

    def test_a_conversation_that_cannot_be_replied_to_is_refused(self):
        self.srv.details[7] = detail(7, can_reply=False)
        code, out, _ = self.reply("--yes")
        self.assertEqual(code, 1)
        self.assertIn("NOT SENT", out)
        self.assertEqual(self.srv.message_posts(), [])

    def test_unknown_conversation_is_refused(self):
        code, _, _ = self.reply("--yes", cid="404")
        self.assertEqual(code, 1)
        self.assertEqual(self.srv.message_posts(), [])

    def test_money_lock_exits_6_without_money_ok(self):
        self.money = ["$500"]
        code, out, _ = self.reply("--yes", text="It is $500 a month")
        self.assertEqual(code, 6)
        self.assertIn("MONEY LOCK", out)
        self.assertEqual(self.srv.message_posts(), [])

    def test_dry_run_shows_the_money_lock(self):
        self.money = ["$500"]
        code, out, _ = self.reply(text="It is $500 a month")
        self.assertEqual(code, 0)
        self.assertIn("MONEY LOCK", out)

    def test_money_ok_lets_it_through(self):
        self.money = ["$500"]
        code, _, _ = self.reply("--yes", "--money-ok", text="It is $500 a month")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.srv.message_posts()), 1)

    def test_money_check_that_cannot_run_locks(self):
        with mock.patch.object(C, "_money_hits", lambda text: None):
            code, _, _ = self.reply("--yes")
        self.assertEqual(code, 6)
        self.assertEqual(self.srv.message_posts(), [])

    def test_gate_refusal_sends_nothing(self):
        self.gate.allow = False
        code, _, _ = self.reply("--yes")
        self.assertEqual(code, 7)
        self.assertEqual(self.srv.message_posts(), [])
        self.assertEqual(self.comms.rows, [])

    def test_gate_that_cannot_load_in_enforce_mode_sends_nothing(self):
        with mock.patch.object(C, "_comms_module", lambda name: None), \
                mock.patch.object(C, "_gate_mode_fallback", lambda: "enforce"):
            code, _, _ = self.reply("--yes")
        self.assertEqual(code, 7)
        self.assertEqual(self.srv.message_posts(), [])


class ReplySend(Base):
    def test_one_post_with_the_right_body_and_one_archive_row(self):
        code, out, _ = self.reply("--yes")
        self.assertEqual(code, 0)
        posts = self.srv.message_posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["path"], f"{BASE}/conversations/7/messages")
        self.assertEqual(posts[0]["body"], {"content": "Happy to help, here is more.",
                                            "message_type": "outgoing", "private": False})
        self.assertEqual(posts[0]["headers"]["content-type"], "application/json")
        self.assertEqual(len(self.comms.rows), 1)
        args, kw = self.comms.rows[0]
        self.assertEqual(args[:3], ("chatwoot", "send", "alex@example.com"))
        self.assertIs(kw["hook_covers"], False)
        self.assertEqual(kw["verb"], "reply")
        self.assertEqual(kw["message_id"], "99")
        self.assertEqual(kw["scope"], ACCOUNT)
        self.assertEqual(self.gate.finished[-1][1], "sent")

    def test_target_falls_back_when_the_contact_has_no_email_or_phone(self):
        self.srv.details[7] = detail(7, email=None)
        self.reply("--yes")
        self.assertEqual(self.comms.rows[0][0][2], "chatwoot:1:7")
        self.srv.details[7] = detail(7, email=None, phone="+1 206 555 0101")
        self.comms.rows.clear()
        self.reply("--yes")
        self.assertEqual(self.comms.rows[0][0][2], "+1 206 555 0101")

    def test_a_timeout_is_uncertain_never_retried_never_archived(self):
        self.srv.override("POST", "/messages", delay=3, status=200, body={})
        with mock.patch.object(C, "WRITE_TIMEOUT", 1):
            code, out, err = self.reply("--yes")
        self.assertEqual(code, C.UNCERTAIN_EXIT)
        self.assertIn("UNCERTAIN", out + err)
        self.assertIn("do not resend", (out + err).lower())
        self.assertEqual(len(self.srv.message_posts()), 1)
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.finished[-1][1], "uncertain")

    def test_a_dropped_connection_after_the_request_left_is_uncertain(self):
        self.srv.override("POST", "/messages", drop=True)
        code, out, err = self.reply("--yes")
        self.assertEqual(code, C.UNCERTAIN_EXIT)
        self.assertIn("UNCERTAIN", out + err)
        self.assertEqual(len(self.srv.message_posts()), 1)
        self.assertEqual(self.comms.rows, [])

    def test_a_500_on_a_send_is_uncertain_and_never_retried(self):
        self.srv.override("POST", "/messages", status=500, body={"error": "boom"})
        code, out, err = self.reply("--yes")
        self.assertEqual(code, C.UNCERTAIN_EXIT)
        self.assertEqual(len(self.srv.message_posts()), 1)
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.finished[-1][1], "uncertain")

    def test_a_2xx_answer_without_a_message_id_is_uncertain(self):
        self.srv.post_response = {}
        code, _, _ = self.reply("--yes")
        self.assertEqual(code, C.UNCERTAIN_EXIT)
        self.assertEqual(self.comms.rows, [])

    def test_a_clean_422_refusal_is_a_failure_and_not_archived(self):
        self.srv.override("POST", "/messages", status=422, body={"message": "no"})
        code, out, err = self.reply("--yes")
        self.assertEqual(code, 1)
        self.assertIn("NOT SENT", out + err)
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.finished[-1][1], "failed_before_send")


class Note(Base):
    def note(self, *extra, text="Customer asked about the invoice."):
        return self.run_cli("note", "7", text, *extra)

    def test_dry_run_posts_nothing(self):
        code, out, _ = self.note()
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertEqual(self.srv.posts(), [])

    def test_yes_posts_a_private_message_and_archives_nothing(self):
        code, out, _ = self.note("--yes")
        self.assertEqual(code, 0)
        posts = self.srv.message_posts()
        self.assertEqual(len(posts), 1)
        self.assertIs(posts[0]["body"]["private"], True)
        self.assertEqual(posts[0]["body"]["message_type"], "outgoing")
        self.assertEqual(self.comms.rows, [])
        self.assertEqual(self.gate.begun, 0)

    def test_timeout_is_uncertain(self):
        self.srv.override("POST", "/messages", delay=3, status=200, body={})
        with mock.patch.object(C, "WRITE_TIMEOUT", 1):
            code, out, err = self.note("--yes")
        self.assertEqual(code, C.UNCERTAIN_EXIT)
        self.assertIn("UNCERTAIN", out + err)
        self.assertEqual(len(self.srv.message_posts()), 1)

    def test_empty_note_is_refused(self):
        code, _, _ = self.note("--yes", text=" ")
        self.assertEqual(code, 1)
        self.assertEqual(self.srv.requests, [])


class ResolveAndMarkRead(Base):
    def test_resolve_dry_run_posts_nothing(self):
        code, out, _ = self.run_cli("resolve", "7")
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertEqual(self.srv.posts(), [])

    def test_resolve_yes_sets_status_resolved_once(self):
        code, _, _ = self.run_cli("resolve", "7", "--yes")
        self.assertEqual(code, 0)
        posts = self.srv.posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["path"], f"{BASE}/conversations/7/toggle_status")
        self.assertEqual(posts[0]["body"], {"status": "resolved"})
        self.assertEqual(self.comms.rows, [])

    def test_resolve_with_an_unknown_result_is_uncertain(self):
        self.srv.override("POST", "/toggle_status", drop=True)
        code, out, err = self.run_cli("resolve", "7", "--yes")
        self.assertEqual(code, C.UNCERTAIN_EXIT)
        self.assertIn("UNCERTAIN", out + err)

    def test_resolve_404_is_a_failure(self):
        self.srv.override("POST", "/toggle_status", status=404, body={})
        code, _, _ = self.run_cli("resolve", "7", "--yes")
        self.assertEqual(code, 1)

    def test_mark_read_dry_run_then_yes(self):
        code, out, _ = self.run_cli("mark-read", "7")
        self.assertEqual(code, 0)
        self.assertEqual(self.srv.posts(), [])
        code, _, _ = self.run_cli("mark-read", "7", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual([r["path"] for r in self.srv.posts()], [f"{BASE}/conversations/7/update_last_seen"])


class DryRunsCheckTheConversation(Base):
    """note, resolve and mark-read must not promise what they cannot do."""

    CASES = (("note", ["note", "7", "Customer asked about the invoice."]),
             ("resolve", ["resolve", "7"]), ("mark-read", ["mark-read", "7"]))

    def test_a_dry_run_loads_the_settings_and_reads_the_conversation_first(self):
        for name, argv in self.CASES:
            self.srv.requests.clear()
            code, out, _ = self.run_cli(*argv)
            self.assertEqual(code, 0, name)
            self.assertIn("DRY RUN", out)
            self.assertIn("Alex Doe", out)           # who it is about, from the conversation read
            self.assertEqual([(r["method"], r["path"]) for r in self.srv.requests],
                             [("GET", f"{BASE}/conversations/7")], name)

    def test_missing_settings_stop_a_dry_run_with_exit_2_and_no_promise(self):
        self.conf.pop("chatwoot.base_url")
        for name, argv in self.CASES:
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 2, name)
            self.assertIn("chatwoot.base_url", out + err)
            self.assertNotIn("Would", out)

    def test_a_conversation_that_does_not_exist_stops_a_dry_run(self):
        for name, argv in self.CASES:
            argv = [a if a != "7" else "404" for a in argv]
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 1, name)
            self.assertNotIn("Would", out)
            self.assertNotIn("DRY RUN", out)
            self.assertIn("404", out + err)

    def test_a_refused_token_stops_a_dry_run(self):
        self.srv.token = "something-else"
        for name, argv in self.CASES:
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 2, name)
            self.assertNotIn("Would", out)

    def test_the_note_preview_prints_once(self):
        code, out, _ = self.run_cli("note", "7", "Customer asked about the invoice.")
        self.assertEqual(out.count("Customer asked about the invoice."), 1)
        self.assertEqual(out.count("DRY RUN"), 1)

    def test_yes_still_posts_once_after_the_read(self):
        for name, argv in self.CASES:
            self.srv.requests.clear()
            code, _, _ = self.run_cli(*argv, "--yes")
            self.assertEqual(code, 0, name)
            self.assertEqual(len(self.srv.posts()), 1, name)


class SupportModule(Base):
    """channel_support.py holds the shared guards. Without it, a write must stop, never pass."""

    def test_a_missing_support_module_stops_a_write_with_exit_2(self):
        with mock.patch.object(C, "_CS", None):
            code, out, err = self.reply("--yes")
        self.assertEqual(code, 2)
        self.assertIn("channel_support", out + err)
        self.assertEqual(len((out + err).strip().splitlines()), 1)
        self.assertEqual(self.srv.requests, [])

    def test_bootstrap_takes_the_first_folder_in_order(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules):
            first, second = os.path.join(tmp, "first"), os.path.join(tmp, "second")
            for d, tag in ((first, "first"), (second, "second")):
                os.makedirs(d)
                with open(os.path.join(d, "channel_support.py"), "w") as fh:
                    fh.write("TAG = %r\n" % tag)
            sys.modules.pop("channel_support", None)
            with mock.patch.object(C, "_core_dirs", lambda: [first, second]):
                mod = C._bootstrap()
        self.assertEqual(mod.TAG, "first")

    def test_bootstrap_with_no_module_anywhere_is_none(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules):
            sys.modules.pop("channel_support", None)
            with mock.patch.object(C, "_core_dirs", lambda: [tmp]):
                self.assertIsNone(C._bootstrap())

    def test_the_money_check_does_not_change_sys_path(self):
        before = list(sys.path)
        REAL_MONEY_HITS("It is $500 a month")
        self.assertEqual(sys.path, before)


class RealArchive(Base):
    """One reply through the REAL core into a temp archive: exactly one row, no more."""

    CORE = os.path.abspath(os.path.join(HERE, "..", "..", "core"))

    def test_one_send_one_row_in_the_real_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "config.json")
            with open(conf, "w") as fh:
                json.dump({"data_dir": os.path.join(tmp, "data")}, fh)
            names = ("commslib", "kitconfig", "outbound_gate")
            saved = {n: sys.modules.pop(n, None) for n in names}
            try:
                with mock.patch.dict(os.environ, {"COMMS_KIT_CONFIG": conf, "COMMS_KIT_CORE": self.CORE,
                                                  "HOME": tmp, "GATE_MODE": "report"}), \
                        mock.patch.object(C, "_comms_module", REAL_COMMS_MODULE):
                    self.reply("--yes", text="Real core check, one row please.")
                rows = []
                for root, _, files in os.walk(os.path.join(tmp, "data", "store")):
                    for name in files:
                        with open(os.path.join(root, name), encoding="utf-8") as fh:
                            rows += [json.loads(line) for line in fh if line.strip().startswith("{")]
            finally:
                for n in names:
                    sys.modules.pop(n, None)
                    if saved[n] is not None:
                        sys.modules[n] = saved[n]
        sends = [r for r in rows if r.get("channel") == "chatwoot" and r.get("dir") == "send"]
        self.assertEqual(len(self.srv.message_posts()), 1)
        self.assertEqual(len(sends), 1, rows)
        self.assertIn("Real core check", sends[0]["text"])


REAL_COMMS_MODULE = C._comms_module   # the function as shipped, before any test patches it
REAL_MONEY_HITS = C._money_hits

if __name__ == "__main__":
    unittest.main()
