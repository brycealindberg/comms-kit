#!/usr/bin/env python3
"""Tests for calls.py. No network, no vault, no real Notes store, no model.

Failure modes covered (written before the code):
 Notes
  1. a store path with a space is not opened (URI not encoded), or WAL-only rows are missed
  2. the snapshot copy stays on disk after a success or an error
  3. the store is written to
  4. a missing folder is an error instead of a plain message and exit 0
  5. audio in another folder is pulled; a locked or deleted note is read or saved
  6. no transcript is lost instead of saved as a "no transcript" row; a pending one is saved too early
  7. the two attachment rows of one recording give two calls
  8. a late transcript is never picked up, or is picked up twice
  9. schema drift gives a traceback
 Whisper
 10. it runs without --whisper; it posts a locked note; a failure kills the pull; a bad URL is used
 Quo
 11. the key has a Bearer prefix or is printed; no key is an error; a 401 is swallowed
 12. page 2 is never fetched; segments are joined out of order; an in-progress call is saved
 13. a request that is not a GET; a bad call id reaches the URL
 litepipe
 14. a call still running is saved; the wrong app is read; the live db is written to
 Common
 15. a second pull adds files or rows; a dry run writes; a lost index duplicates rows
 16. the archive row is dropped inside a Claude Code session (hook runtime)
 17. a transcript or key appears in pull/list output; show accepts a path
"""
import contextlib
import glob
import hashlib
import http.server
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import calls as C  # noqa: E402

SECRET_TEXT = "zebra-quartz-phrase-7731"
KEY = "-".join(["quo", "FAKE", "key", "not", "real"])
COCOA = 978307200


def cocoa(iso_epoch: float) -> float:
    return iso_epoch - COCOA


class Env(unittest.TestCase):
    """Temp config, temp data dir, fake commslib that records archive rows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.data = os.path.join(self.root, "data")
        self.cfg_path = os.path.join(self.root, "config-%s.json" % time.time_ns())
        # never the real Notes store: a path that does not exist unless a test sets its own
        self.cfg = {"data_dir": self.data, "calls": {"notes_db": os.path.join(self.root, "no-notes.sqlite")}}
        self.rows = []
        outer = self

        class FakeLib:
            @staticmethod
            def log_event(*a, **kw):
                outer.rows.append((a, kw))
                return True
        self.fake = FakeLib
        self._old = (C._get_commslib, os.environ.get("COMMS_KIT_CONFIG"))
        C._get_commslib = lambda: FakeLib
        self.addCleanup(self._restore)
        os.environ["COMMS_KIT_CONFIG"] = self.cfg_path
        self.write_cfg()
        self.old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(self._tz)

    def _tz(self):
        if self.old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old_tz
        time.tzset()

    def _restore(self):
        C._get_commslib = self._old[0]
        if self._old[1] is None:
            os.environ.pop("COMMS_KIT_CONFIG", None)
        else:
            os.environ["COMMS_KIT_CONFIG"] = self._old[1]

    def write_cfg(self):
        # a new file name each time so kitconfig's mtime/size cache can never serve stale data
        self.cfg_path = os.path.join(self.root, "config-%s.json" % time.time_ns())
        with open(self.cfg_path, "w") as fh:
            json.dump(self.cfg, fh)
        os.environ["COMMS_KIT_CONFIG"] = self.cfg_path

    def set_calls(self, **kw):
        self.cfg["calls"].update(kw)
        self.write_cfg()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                C.main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        return code, out.getvalue() + err.getvalue()

    def files(self):
        return sorted(glob.glob(os.path.join(self.data, "calls", "*.txt")))


# ---- fake Notes store ------------------------------------------------------------------
NOTE_SCHEMA = """CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZTYPEUTI TEXT,
 ZADDITIONALINDEXABLETEXT TEXT, ZNEEDSTRANSCRIPTION INTEGER, ZDURATION REAL, ZNOTE INTEGER, ZMEDIA INTEGER,
 ZIDENTIFIER TEXT, ZFOLDER INTEGER, ZTITLE1 TEXT, ZTITLE2 TEXT, ZISPASSWORDPROTECTED INTEGER,
 ZMARKEDFORDELETION INTEGER, ZCREATIONDATE3 REAL, ZFILENAME TEXT, ZGENERATION1 TEXT)"""
CREATED = 1_790_000_000.0   # a unix time in Sep 2026


def build_notes(path, notes, folders=("Call Recordings",), wal=False):
    """notes: dicts {ident, folder, title, created(unix), locked, deleted, atts:[{uti,text,needs,dur,file,media}]}.
    Returns the open connection when wal=True (so the WAL file stays populated)."""
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute(NOTE_SCHEMA)
    pk = 1000
    fpk = {}
    for name in folders:
        pk += 1
        fpk[name] = pk
        con.execute("INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK,Z_ENT,ZTITLE2) VALUES (?,15,?)", (pk, name))
    for n in notes:
        pk += 1
        npk = pk
        con.execute("INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK,Z_ENT,ZIDENTIFIER,ZFOLDER,ZTITLE1,ZISPASSWORDPROTECTED,"
                    "ZMARKEDFORDELETION,ZCREATIONDATE3) VALUES (?,12,?,?,?,?,?,?)",
                    (npk, n["ident"], fpk[n.get("folder", folders[0])], n.get("title", "Call with Someone"),
                     int(n.get("locked", False)), int(n.get("deleted", False)),
                     cocoa(n.get("created", CREATED))))
        for a in n.get("atts", []):
            pk += 1
            mpk = None
            if a.get("file"):
                pk += 1
                mpk = pk
                con.execute("INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK,Z_ENT,ZIDENTIFIER,ZFILENAME,ZGENERATION1) "
                            "VALUES (?,11,?,?,?)", (mpk, a["media"], a["file"], "1_GEN"))
            pk += 1
            con.execute("INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK,Z_ENT,ZTYPEUTI,ZADDITIONALINDEXABLETEXT,"
                        "ZNEEDSTRANSCRIPTION,ZDURATION,ZNOTE,ZMEDIA,ZIDENTIFIER) VALUES (?,5,?,?,?,?,?,?,?)",
                        (pk, a["uti"], a.get("text"), a.get("needs"), a.get("dur", 0.0), npk, mpk, "att-%d" % pk))
    con.commit()
    if wal:
        return con
    con.close()
    return None


def m4a(text=None, **kw):
    d = {"uti": "com.apple.m4a-audio", "text": text}
    d.update(kw)
    return d


def sibling(dur=61.0):
    return {"uti": "public.mpeg-4-audio", "dur": dur}


class NotesBase(Env):
    def setUp(self):
        super().setUp()
        self.store_dir = os.path.join(self.root, "Group Containers", "notes dir")  # a space on purpose
        os.makedirs(self.store_dir)
        self.store = os.path.join(self.store_dir, "NoteStore.sqlite")
        self.set_calls(notes_db=self.store)

    def pull(self, *extra, source="notes"):
        return self.run_cli("pull", "--source", source, "--since", "0", *extra)


class TestNotes(NotesBase):
    def test_path_with_space_and_wal_rows(self):
        keep = build_notes(self.store, [{"ident": "N1", "atts": [m4a("hello " + SECRET_TEXT), sibling()]}], wal=True)
        self.addCleanup(keep.close)
        self.assertTrue(os.path.getsize(self.store + "-wal") > 0)
        code, out = self.pull("--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.files()), 1)
        with open(self.files()[0]) as fh:
            self.assertIn(SECRET_TEXT, fh.read())

    def test_snapshot_removed_after_success_and_error(self):
        build_notes(self.store, [])
        seen = []
        with C.notes_snapshot(self.store) as snap:
            seen.append(snap)
            self.assertTrue(os.path.exists(snap))
        self.assertFalse(os.path.exists(os.path.dirname(seen[0])))
        with self.assertRaises(RuntimeError):
            with C.notes_snapshot(self.store) as snap:
                seen.append(snap)
                raise RuntimeError("boom")
        self.assertFalse(os.path.exists(os.path.dirname(seen[1])))

    def test_store_never_written(self):
        keep = build_notes(self.store, [{"ident": "N1", "atts": [m4a("t"), sibling()]}], wal=True)
        self.addCleanup(keep.close)

        def digest():
            return [hashlib.sha256(open(p, "rb").read()).hexdigest() for p in (self.store, self.store + "-wal")]
        before = digest()
        self.pull("--yes")
        self.assertEqual(before, digest())

    def test_missing_folder_is_plain_message_exit_0(self):
        build_notes(self.store, [{"ident": "N1", "atts": [m4a("t")]}], folders=("Other",))
        code, out = self.pull("--yes")
        self.assertEqual(code, 0)
        self.assertIn("No folder named", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.files(), [])
        self.assertEqual(self.rows, [])

    def test_missing_store_is_plain_exit_0_when_notes_only_default(self):
        code, out = self.pull("--yes")   # no store file at all
        self.assertIn("Notes store not found", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(code, 0)

    def test_other_folder_ignored_and_override(self):
        build_notes(self.store, [
            {"ident": "A", "folder": "Call Recordings", "atts": [m4a("in default")]},
            {"ident": "B", "folder": "Voice", "atts": [m4a("in voice")]}], folders=("Call Recordings", "Voice"))
        self.pull("--yes")
        self.assertEqual(len(self.files()), 1)
        self.assertIn("_notes_A.txt", self.files()[0])
        code, out = self.run_cli("pull", "--source", "notes", "--since", "0", "--notes-folder", "voice", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.files()), 2)

    def test_config_folder_name(self):
        build_notes(self.store, [{"ident": "B", "folder": "Voice", "atts": [m4a("x")]}], folders=("Voice",))
        self.set_calls(notes_folder="Voice")
        self.pull("--yes")
        self.assertEqual(len(self.files()), 1)

    def test_locked_and_deleted_skipped_and_counted(self):
        build_notes(self.store, [
            {"ident": "L", "locked": True, "atts": [m4a(SECRET_TEXT)]},
            {"ident": "D", "deleted": True, "atts": [m4a(SECRET_TEXT)]},
            {"ident": "OK", "atts": [m4a("fine")]}])
        code, out = self.pull("--yes")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.files()), 1)
        self.assertRegex(out, r"locked 1")
        self.assertRegex(out, r"deleted 1")
        self.assertNotIn(SECRET_TEXT, out)
        for p in self.files():
            self.assertNotIn(SECRET_TEXT, open(p).read())

    def test_no_transcript_saved_with_marker_and_counted(self):
        build_notes(self.store, [{"ident": "NT", "atts": [m4a(None, needs=0), sibling(375)]}])
        code, out = self.pull("--yes")
        self.assertRegex(out, r"no transcript 1")
        self.assertEqual(len(self.files()), 1)
        body = open(self.files()[0]).read()
        self.assertIn("No transcript", body)
        self.assertEqual(len(self.rows), 1)

    def test_pending_not_saved_then_saved_when_ready(self):
        build_notes(self.store, [{"ident": "P", "atts": [m4a(None, needs=1)]}])
        code, out = self.pull("--yes")
        self.assertRegex(out, r"not ready 1")
        self.assertEqual(self.files(), [])
        os.remove(self.store)
        build_notes(self.store, [{"ident": "P", "atts": [m4a("now ready", needs=0)]}])
        self.pull("--yes")
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(len(self.rows), 1)

    def test_two_attachment_rows_make_one_call_with_duration(self):
        build_notes(self.store, [{"ident": "TWO", "atts": [m4a("words here"), sibling(125.0)]}])
        self.pull("--yes")
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(len(self.rows), 1)
        self.assertIn("duration: 125", open(self.files()[0]).read())

    def test_second_pull_adds_nothing_even_without_index(self):
        build_notes(self.store, [{"ident": "I", "atts": [m4a("same")]}, {"ident": "J", "atts": [m4a(None, needs=0)]}])
        self.pull("--yes")
        n_files, n_rows = len(self.files()), len(self.rows)
        self.assertEqual((n_files, n_rows), (2, 2))
        code, out = self.pull("--yes")
        self.assertEqual((len(self.files()), len(self.rows)), (2, 2))
        self.assertRegex(out, r"already saved 2")
        os.remove(os.path.join(self.data, "calls", "index.json"))
        self.pull("--yes")
        self.assertEqual((len(self.files()), len(self.rows)), (2, 2))

    def test_dry_run_writes_nothing(self):
        build_notes(self.store, [{"ident": "I", "atts": [m4a("same")]}])
        code, out = self.pull()
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertRegex(out, r"would save 1")
        self.assertFalse(os.path.exists(os.path.join(self.data, "calls")))
        self.assertEqual(self.rows, [])

    def test_late_transcript_upgrades_once(self):
        build_notes(self.store, [{"ident": "LT", "atts": [m4a(None, needs=0)]}])
        self.pull("--yes")
        os.remove(self.store)
        build_notes(self.store, [{"ident": "LT", "atts": [m4a("arrived " + SECRET_TEXT, needs=0)]}])
        self.pull("--yes")
        self.assertEqual(len(self.files()), 1)
        self.assertIn(SECRET_TEXT, open(self.files()[0]).read())
        self.assertEqual(len(self.rows), 2)
        self.pull("--yes")
        self.assertEqual(len(self.rows), 2)

    def test_file_name_uses_local_date(self):
        os.environ["TZ"] = "Pacific/Kiritimati"   # UTC+14 moves 1_790_000_000 (Sep 21 14:13 UTC) to Sep 22
        time.tzset()
        build_notes(self.store, [{"ident": "D1", "atts": [m4a("x")]}])
        self.pull("--yes")
        self.assertTrue(os.path.basename(self.files()[0]).startswith("2026-09-22_notes_D1"), self.files())

    def test_since_excludes_old(self):
        old = time.time() - 90 * 86400
        build_notes(self.store, [{"ident": "OLD", "created": old, "atts": [m4a("x")]},
                                 {"ident": "NEW", "created": time.time() - 3600, "atts": [m4a("y")]}])
        code, out = self.run_cli("pull", "--source", "notes", "--since", "30", "--yes")
        self.assertEqual([os.path.basename(f).split("_")[2] for f in self.files()], ["NEW.txt"])

    def test_schema_drift_is_plain(self):
        con = sqlite3.connect(self.store)
        con.execute("CREATE TABLE other (a)")
        con.close()
        code, out = self.pull("--yes")
        self.assertEqual(code, 1)
        self.assertIn("Notes", out)
        self.assertNotIn("Traceback", out)

    def test_dangerous_id_is_neutralised(self):
        build_notes(self.store, [{"ident": "../../evil", "atts": [m4a("x")]}])
        self.pull("--yes")
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(os.path.dirname(self.files()[0]), os.path.join(self.data, "calls"))


# ---- whisper ---------------------------------------------------------------------------
class WhisperServer:
    def __init__(self, status=200, reply=None):
        self.requests = []
        outer = self
        self.status = status
        self.reply = reply if reply is not None else {"text": "synthetic words come back"}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.requests.append((self.path, self.headers.get("Content-Type", ""), self.rfile.read(n)))
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(outer.reply).encode())

            def do_GET(self):
                outer.requests.append(("GET " + self.path, "", b""))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestWhisper(NotesBase):
    AUDIO = b"RIFFfake-audio-bytes-0123456789"

    def put_audio(self, ident="MED1", name="Call with X.m4a"):
        d = os.path.join(self.store_dir, "Accounts", "acct", "Media", ident, "1_GEN")
        os.makedirs(d)
        with open(os.path.join(d, name), "wb") as fh:
            fh.write(self.AUDIO)

    def server(self, **kw):
        s = WhisperServer(**kw)
        self.addCleanup(s.stop)
        self.set_calls(whisper_url=s.url)
        return s

    def note(self, **kw):
        n = {"ident": "W1", "atts": [m4a(None, needs=0, file="Call with X.m4a", media="MED1"), sibling()]}
        n.update(kw)
        return n

    def test_off_by_default(self):
        s = self.server()
        self.put_audio()
        build_notes(self.store, [self.note()])
        self.pull("--yes")
        self.assertEqual(s.requests, [])

    def test_words_come_back_and_request_shape(self):
        s = self.server()
        self.put_audio()
        build_notes(self.store, [self.note()])
        code, out = self.pull("--yes", "--whisper")
        self.assertEqual(code, 0, out)
        path, ctype, body = s.requests[0]
        self.assertEqual(path, "/v1/transcribe")
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        self.assertIn(self.AUDIO, body)
        self.assertIn(b'name="file"', body)
        text = open(self.files()[0]).read()
        self.assertIn("synthetic words come back", text)
        self.assertIn("whisper", text.split("\n\n")[0])
        self.assertRegex(out, r"whisper 1")

    def test_failure_keeps_no_transcript_and_exit_0(self):
        self.server(status=500)
        self.put_audio()
        build_notes(self.store, [self.note()])
        code, out = self.pull("--yes", "--whisper")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"whisper failed 1")
        self.assertIn("No transcript", open(self.files()[0]).read())

    def test_unreachable_server(self):
        self.set_calls(whisper_url="http://127.0.0.1:9")
        self.put_audio()
        build_notes(self.store, [self.note()])
        code, out = self.pull("--yes", "--whisper")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"whisper failed 1")

    def test_failed_whisper_retried_next_whisper_pull(self):
        s = self.server(status=500)
        self.put_audio()
        build_notes(self.store, [self.note()])
        self.pull("--yes", "--whisper")
        s.status = 200
        self.pull("--yes", "--whisper")
        self.assertIn("synthetic words", open(self.files()[0]).read())
        self.assertEqual(len(self.rows), 2)
        self.pull("--yes", "--whisper")
        self.assertEqual(len(s.requests), 2)    # a saved transcript is not sent again

    def test_no_url_configured_is_usage_error(self):
        self.put_audio()
        build_notes(self.store, [self.note()])
        code, out = self.pull("--yes", "--whisper")
        self.assertEqual(code, 2)
        self.assertIn("whisper_url", out)

    def test_non_http_url_refused(self):
        self.set_calls(whisper_url="file:///etc/passwd")
        build_notes(self.store, [self.note()])
        code, out = self.pull("--yes", "--whisper")
        self.assertEqual(code, 2)

    def test_locked_note_audio_never_sent(self):
        s = self.server()
        self.put_audio()
        build_notes(self.store, [self.note(locked=True)])
        self.pull("--yes", "--whisper")
        self.assertEqual(s.requests, [])

    def test_empty_answer_stays_no_transcript(self):
        self.server(reply={"text": "  "})
        self.put_audio()
        build_notes(self.store, [self.note()])
        code, out = self.pull("--yes", "--whisper")
        self.assertRegex(out, r"whisper failed 1")
        self.assertIn("No transcript", open(self.files()[0]).read())

    def test_missing_audio_file_counted(self):
        s = self.server()
        build_notes(self.store, [self.note()])
        code, out = self.pull("--yes", "--whisper")
        self.assertEqual(s.requests, [])
        self.assertRegex(out, r"audio missing 1")


# ---- Quo -------------------------------------------------------------------------------
def seg(content, start=0, ident="+15550100", actor=None):
    return {"content": content, "start": start, "end": start + 1, "identifier": ident, "actorId": actor}


def tr(status="completed", t="2026-10-30T10:00:00Z", dialogue=None, rid="CR1"):
    return {"recordingId": rid, "status": status, "startTime": t, "createdAt": t, "duration": 30,
            "dialogue": dialogue if dialogue is not None else []}


def call(cid, status="completed", created="2026-10-30T10:00:00Z", **kw):
    d = {"id": cid, "status": status, "createdAt": created, "direction": "incoming", "duration": 30,
         "answeredAt": created, "completedAt": created,
         "participants": [{"phoneNumber": "+15550100", "actorId": None}], "phoneNumberId": "PN1"}
    d.update(kw)
    return d


class FakeQuo:
    def __init__(self, pages=None, transcripts=None, fail=None):
        self.calls = []
        self.pages = pages if pages is not None else [{"data": [], "nextCursor": None}]
        self.transcripts = transcripts or {}
        self.fail = fail or {}

    def __call__(self, url, headers, timeout=30):
        self.calls.append((url, dict(headers)))
        path = url.split("api.quo.com", 1)[1]
        for needle, result in self.fail.items():
            if needle in path:
                if isinstance(result, Exception):
                    raise result
                return result, {"message": "x"}
        if path.startswith("/calls?"):
            after = None
            for part in path.split("?", 1)[1].split("&"):
                if part.startswith("after="):
                    after = part[6:]
            idx = int(after.split("-")[1]) if after else 0
            return 200, self.pages[idx]
        cid = path.split("/")[2]
        after = None
        if "after=" in path:
            after = path.split("after=")[1].split("&")[0]
        pages = self.transcripts.get(cid, [{"data": [], "nextCursor": None}])
        idx = int(after.split("-")[1]) if after else 0
        return 200, pages[idx]


class TestQuo(Env):
    def setUp(self):
        super().setUp()
        self.set_calls(quo_key_command=["fake-key-cmd"])
        self._old_key = C._run_key_cmd
        C._run_key_cmd = lambda cmd: KEY
        self._old_get = C.http_get
        self.addCleanup(self._restore_quo)

    def _restore_quo(self):
        C._run_key_cmd = self._old_key
        C.http_get = self._old_get

    def pull(self, fake, *extra):
        C.http_get = fake
        return self.run_cli("pull", "--source", "quo", "--since", "0", "--yes", *extra)

    def test_no_key_command_is_skipped_exit_0(self):
        self.cfg["calls"].pop("quo_key_command")
        self.write_cfg()
        fake = FakeQuo()
        code, out = self.pull(fake)
        self.assertEqual(code, 0)
        self.assertIn("Quo skipped", out)
        self.assertEqual(fake.calls, [])

    def test_key_command_failing_is_skipped_exit_0(self):
        def boom(cmd):
            raise C.ApiError("the key command failed (exit 1)")
        C._run_key_cmd = boom
        code, out = self.pull(FakeQuo())
        self.assertEqual(code, 0)
        self.assertIn("Quo skipped", out)

    def test_headers_have_no_bearer_and_key_never_printed(self):
        fake = FakeQuo(pages=[{"data": [call("AC1")], "nextCursor": None}],
                       transcripts={"AC1": [{"data": [tr(dialogue=[seg("hi")])], "nextCursor": None}]})
        code, out = self.pull(fake)
        for url, headers in fake.calls:
            self.assertEqual(headers["Authorization"], KEY)
            self.assertEqual(headers["Quo-Api-Version"], "2026-03-30")
        self.assertNotIn(KEY, out)
        self.assertTrue(fake.calls[0][0].startswith("https://api.quo.com/calls?"))
        self.assertIn("limit=50", fake.calls[0][0])

    def test_pagination_of_calls_and_transcripts(self):
        fake = FakeQuo(
            pages=[{"data": [call("AC1")], "nextCursor": "c-1"}, {"data": [call("AC2")], "nextCursor": None}],
            transcripts={
                "AC1": [{"data": [tr(dialogue=[seg("one")], t="2026-10-30T10:00:00Z")], "nextCursor": "t-1"},
                        {"data": [tr(dialogue=[seg("two")], t="2026-10-30T10:05:00Z", rid="CR2")], "nextCursor": None}],
                "AC2": [{"data": [tr(dialogue=[seg("other")])], "nextCursor": None}]})
        code, out = self.pull(fake)
        self.assertEqual(len(self.files()), 2)
        text = open([f for f in self.files() if "AC1" in f][0]).read()
        self.assertLess(text.index("one"), text.index("two"))

    def test_segments_sorted_by_start_time_and_speaker_labels(self):
        fake = FakeQuo(pages=[{"data": [call("AC1")], "nextCursor": None}],
                       transcripts={"AC1": [{"data": [
                           tr(dialogue=[seg("second part", ident="+15550100")], t="2026-10-30T10:05:00Z", rid="CR2"),
                           tr(dialogue=[seg("first part", ident=None, actor="US123")], t="2026-10-30T10:00:00Z")],
                           "nextCursor": None}]})
        self.pull(fake)
        text = open(self.files()[0]).read()
        self.assertLess(text.index("first part"), text.index("second part"))
        self.assertIn("US123", text)
        self.assertIn("+15550100", text)

    def test_in_progress_retried_next_pull(self):
        fake = FakeQuo(pages=[{"data": [call("AC1")], "nextCursor": None}],
                       transcripts={"AC1": [{"data": [tr(status="in-progress")], "nextCursor": None}]})
        code, out = self.pull(fake)
        self.assertRegex(out, r"not ready 1")
        self.assertEqual(self.files(), [])
        fake.transcripts["AC1"] = [{"data": [tr(dialogue=[seg("done now")])], "nextCursor": None}]
        self.pull(fake)
        self.assertEqual(len(self.files()), 1)
        self.pull(fake)
        self.assertEqual(len(self.rows), 1)

    def test_one_segment_in_progress_holds_whole_call(self):
        fake = FakeQuo(pages=[{"data": [call("AC1")], "nextCursor": None}],
                       transcripts={"AC1": [{"data": [tr(dialogue=[seg("a")]), tr(status="in-progress", rid="CR2",
                                                                                   t="2026-10-30T10:05:00Z")],
                                             "nextCursor": None}]})
        self.pull(fake)
        self.assertEqual(self.files(), [])

    def test_absent_and_failed_saved_as_no_transcript(self):
        fake = FakeQuo(pages=[{"data": [call("AC1"), call("AC2")], "nextCursor": None}],
                       transcripts={"AC1": [{"data": [tr(status="absent")], "nextCursor": None}],
                                    "AC2": [{"data": [tr(status="failed")], "nextCursor": None}]})
        code, out = self.pull(fake)
        self.assertRegex(out, r"no transcript 2")
        self.assertEqual(len(self.files()), 2)
        for f in self.files():
            self.assertIn("No transcript", open(f).read())

    def test_completed_call_with_no_transcript_rows_old_vs_new(self):
        old = call("AC1", created="2026-01-01T00:00:00Z")
        new_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))
        new = call("AC2", created=new_iso)
        fake = FakeQuo(pages=[{"data": [old, new], "nextCursor": None}])
        code, out = self.pull(fake)
        self.assertRegex(out, r"no transcript 1")
        self.assertRegex(out, r"not ready 1")

    def test_non_completed_calls_ignored(self):
        fake = FakeQuo(pages=[{"data": [call("AC1", status="missed", answeredAt=None),
                                        call("AC2", status="ringing", answeredAt=None, completedAt=None),
                                        call("AC3", status="in-progress", completedAt=None)],
                               "nextCursor": None}])
        code, out = self.pull(fake)
        self.assertEqual(self.files(), [])
        self.assertEqual(len(fake.calls), 1)      # no transcript request for them

    def test_a_finished_call_counts_by_its_times_not_one_status_word(self):
        # Quo's list lists both `answered` and `completed` and does not say which a finished call gets.
        for number, word in enumerate(("answered", "completed"), 1):
            cid = f"AC{number}"
            fake = FakeQuo(pages=[{"data": [call(cid, status=word)], "nextCursor": None}],
                           transcripts={cid: [{"data": [tr(dialogue=[seg("hello there")])], "nextCursor": None}]})
            self.pull(fake)
            self.assertEqual(len(self.files()), number, word)

    def test_401_stops_source_exit_1_no_key_in_output(self):
        fake = FakeQuo(fail={"/calls?": 401})
        code, out = self.pull(fake)
        self.assertEqual(code, 1)
        self.assertIn("401", out)
        self.assertNotIn(KEY, out)

    def test_network_error_exit_1_keeps_saved_items(self):
        fake = FakeQuo(pages=[{"data": [call("AC1"), call("AC2")], "nextCursor": None}],
                       transcripts={"AC1": [{"data": [tr(dialogue=[seg("ok")])], "nextCursor": None}]},
                       fail={"/calls/AC2/": C.ApiError("could not reach Quo")})
        code, out = self.pull(fake)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.files()), 1)
        self.assertNotIn(KEY, out)

    def test_other_source_still_runs_when_quo_fails(self):
        db = os.path.join(self.root, "lp.sqlite")
        make_litepipe(db, [meeting(1, "Phone", end="2026-10-30T11:00:00Z", segs=[("2026-10-30T10:00:01Z", "A", "hello")])])
        self.set_calls(litepipe_db=db)
        C.http_get = FakeQuo(fail={"/calls?": 401})
        code, out = self.run_cli("pull", "--source", "all", "--since", "0", "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.files()), 1)

    def test_only_get_and_bad_call_id_never_requested(self):
        fake = FakeQuo(pages=[{"data": [call("../x"), call("AC1")], "nextCursor": None}],
                       transcripts={"AC1": [{"data": [tr(dialogue=[seg("ok")])], "nextCursor": None}]})
        self.pull(fake)
        self.assertFalse(any(".." in url for url, _ in fake.calls))
        self.assertEqual(len(self.files()), 1)

    def test_since_becomes_createdAt_gte(self):
        fake = FakeQuo()
        C.http_get = fake
        self.run_cli("pull", "--source", "quo", "--since", "2", "--yes")
        self.assertIn("createdAt%5Bgte%5D=", fake.calls[0][0])

    def test_unexpected_json_shape_is_plain_error(self):
        C.http_get = lambda url, headers, timeout=30: (200, ["not", "an", "object"])
        code, out = self.run_cli("pull", "--source", "quo", "--since", "0", "--yes")
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", out)


# ---- litepipe --------------------------------------------------------------------------
def make_litepipe(path, meetings):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE meetings (id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_start TEXT NOT NULL, "
                "meeting_end TEXT, meeting_app TEXT NOT NULL, title TEXT)")
    con.execute("CREATE TABLE meeting_transcript_segments (id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id INTEGER,"
                " captured_at TEXT NOT NULL, speaker_name TEXT, transcript TEXT NOT NULL, speaker_id INTEGER)")
    for m in meetings:
        con.execute("INSERT INTO meetings VALUES (?,?,?,?,?)", (m["id"], m["start"], m["end"], m["app"], m["title"]))
        for cap, who, text, *sid in m["segs"]:
            con.execute("INSERT INTO meeting_transcript_segments (meeting_id,captured_at,speaker_name,transcript,"
                        "speaker_id) VALUES (?,?,?,?,?)", (m["id"], cap, who, text, sid[0] if sid else None))
    con.commit()
    con.close()


def meeting(mid, app, start="2026-10-30T10:00:00.000Z", end=None, title="T", segs=()):
    return {"id": mid, "app": app, "start": start, "end": end, "title": title, "segs": list(segs)}


class TestLitepipe(Env):
    def setUp(self):
        super().setUp()
        self.db = os.path.join(self.root, "lp dir.sqlite")

    def pull(self, *extra):
        return self.run_cli("pull", "--source", "litepipe", "--since", "0", "--yes", *extra)

    def test_empty_config_is_off(self):
        code, out = self.pull()
        self.assertEqual(code, 0)
        self.assertIn("litepipe skipped", out)

    def test_missing_db_is_plain(self):
        self.set_calls(litepipe_db=os.path.join(self.root, "nope.sqlite"))
        code, out = self.pull()
        self.assertIn("litepipe skipped", out)
        self.assertEqual(code, 0)

    def test_default_apps_facetime_phone_only(self):
        make_litepipe(self.db, [
            meeting(1, "FaceTime", end="2026-10-30T11:00:00Z", segs=[("2026-10-30T10:00:01Z", "A", "x")]),
            meeting(2, "Phone", end="2026-10-30T11:00:00Z", segs=[("2026-10-30T10:00:01Z", "A", "y")]),
            meeting(3, "Google Chrome", end="2026-10-30T11:00:00Z", segs=[("2026-10-30T10:00:01Z", "A", "z")])])
        self.set_calls(litepipe_db=self.db)
        self.pull()
        self.assertEqual(len(self.files()), 2)

    def test_apps_override_and_config(self):
        make_litepipe(self.db, [meeting(3, "Google Chrome", end="2026-10-30T11:00:00Z",
                                        segs=[("2026-10-30T10:00:01Z", "A", "z")])])
        self.set_calls(litepipe_db=self.db)
        self.pull("--apps", "Google Chrome")
        self.assertEqual(len(self.files()), 1)
        os.remove(self.files()[0])
        os.remove(os.path.join(self.data, "calls", "index.json"))
        self.set_calls(litepipe_apps=["Google Chrome"])
        self.pull()
        self.assertEqual(len(self.files()), 1)

    def test_running_meeting_not_saved_until_ended(self):
        make_litepipe(self.db, [meeting(1, "Phone", end=None, segs=[("2026-10-30T10:00:01Z", "A", "half")])])
        self.set_calls(litepipe_db=self.db)
        code, out = self.pull()
        self.assertRegex(out, r"not ready 1")
        self.assertEqual(self.files(), [])

    def test_no_segments_is_no_transcript(self):
        make_litepipe(self.db, [meeting(1, "Phone", end="2026-10-30T11:00:00Z")])
        self.set_calls(litepipe_db=self.db)
        code, out = self.pull()
        self.assertRegex(out, r"no transcript 1")
        self.assertIn("No transcript", open(self.files()[0]).read())

    def test_segments_in_time_order_with_speakers(self):
        make_litepipe(self.db, [meeting(1, "Phone", end="2026-10-30T11:00:00Z", segs=[
            ("2026-10-30T10:00:09Z", "Bob", "later line"),
            ("2026-10-30T10:00:01Z", None, "early line", 2)])])
        self.set_calls(litepipe_db=self.db)
        self.pull()
        text = open(self.files()[0]).read()
        self.assertLess(text.index("early line"), text.index("later line"))
        self.assertIn("Speaker 2:", text)
        self.assertIn("Bob:", text)

    def test_db_not_modified_and_idempotent(self):
        make_litepipe(self.db, [meeting(1, "Phone", end="2026-10-30T11:00:00Z",
                                        segs=[("2026-10-30T10:00:01Z", "A", "x")])])
        self.set_calls(litepipe_db=self.db)
        before = hashlib.sha256(open(self.db, "rb").read()).hexdigest()
        self.pull()
        self.pull()
        self.assertEqual(before, hashlib.sha256(open(self.db, "rb").read()).hexdigest())
        self.assertEqual((len(self.files()), len(self.rows)), (1, 1))

    def test_name_has_date_source_id(self):
        make_litepipe(self.db, [meeting(7, "Phone", start="2026-10-30T10:00:00.000Z", end="2026-10-30T11:00:00Z",
                                        segs=[("2026-10-30T10:00:01Z", "A", "x")])])
        self.set_calls(litepipe_db=self.db)
        self.pull()
        self.assertEqual(os.path.basename(self.files()[0]), "2026-10-30_litepipe_7.txt")


# ---- common ----------------------------------------------------------------------------
class TestCommon(Env):
    def seed(self):
        db = os.path.join(self.root, "lp.sqlite")
        make_litepipe(db, [meeting(i, "Phone", start="2026-10-%02dT10:00:00Z" % (10 + i), end="2026-10-30T11:00:00Z",
                                   segs=[("2026-10-30T10:00:01Z", "A", SECRET_TEXT + str(i))]) for i in (1, 2, 3)])
        self.set_calls(litepipe_db=db)

    def test_pull_output_has_counts_only(self):
        self.seed()
        code, out = self.run_cli("pull", "--source", "litepipe", "--since", "0", "--yes")
        self.assertNotIn(SECRET_TEXT, out)
        code, out = self.run_cli("list")
        self.assertNotIn(SECRET_TEXT, out)

    def test_archive_row_shape(self):
        self.seed()
        self.run_cli("pull", "--source", "litepipe", "--since", "0", "--yes")
        (args, kw) = self.rows[0]
        self.assertEqual(args[0], "calls")
        self.assertIs(kw["hook_covers"], False)
        self.assertIn(SECRET_TEXT, kw["body"])
        self.assertTrue(kw["files"][0].endswith(".txt"))
        self.assertEqual(kw["message_id"], "litepipe:1")

    def test_archive_failure_does_not_mark_done(self):
        self.seed()

        class Dead:
            @staticmethod
            def log_event(*a, **k):
                return False
        C._get_commslib = lambda: Dead
        code, out = self.run_cli("pull", "--source", "litepipe", "--since", "0", "--yes")
        self.assertEqual(code, 1)
        C._get_commslib = lambda: self.fake
        self.run_cli("pull", "--source", "litepipe", "--since", "0", "--yes")
        self.assertEqual(len(self.rows), 3)     # retried next pull, one row each

    def test_no_commslib_means_nothing_saved(self):
        self.seed()
        C._get_commslib = lambda: None
        code, out = self.run_cli("pull", "--source", "litepipe", "--since", "0", "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.files(), [])

    def test_permissions(self):
        self.seed()
        self.run_cli("pull", "--source", "litepipe", "--since", "0", "--yes")
        self.assertEqual(os.stat(os.path.join(self.data, "calls")).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.files()[0]).st_mode & 0o777, 0o600)

    def test_list_newest_first_and_limit(self):
        self.seed()
        self.run_cli("pull", "--source", "litepipe", "--since", "0", "--yes")
        code, out = self.run_cli("list", "--limit", "2")
        lines = [l for l in out.splitlines() if "litepipe" in l]
        self.assertEqual(len(lines), 2)
        self.assertIn("2026-10-13", lines[0])

    def test_show_by_id_name_and_refuses_paths(self):
        self.seed()
        self.run_cli("pull", "--source", "litepipe", "--since", "0", "--yes")
        code, out = self.run_cli("show", "litepipe_2")
        self.assertIn(SECRET_TEXT + "2", out)
        code, out = self.run_cli("show", "2026-10-12_litepipe_2")
        self.assertIn(SECRET_TEXT + "2", out)
        for bad in ("../../etc/passwd", "/etc/passwd", "nothing_here"):
            code, out = self.run_cli("show", bad)
            self.assertEqual(code, 1)
            self.assertNotIn("root:", out)

    def test_show_ambiguous_id(self):
        self.seed()
        self.run_cli("pull", "--source", "litepipe", "--since", "0", "--yes")
        open(os.path.join(self.data, "calls", "2026-10-01_notes_2.txt"), "w").write("x")
        code, out = self.run_cli("show", "2")
        self.assertEqual(code, 1)
        self.assertIn("more than one", out.lower())

    def test_doctor_reports_each_source_without_content(self):
        self.seed()
        code, out = self.run_cli("doctor")
        for word in ("notes", "quo", "litepipe", "whisper", "archive"):
            self.assertIn(word, out)
        self.assertNotIn(SECRET_TEXT, out)

    def test_pull_all_with_nothing_configured_is_exit_0(self):
        self.set_calls(notes_db=os.path.join(self.root, "none.sqlite"))
        code, out = self.run_cli("pull", "--source", "all", "--yes")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)

    def test_second_pull_running_at_once_does_nothing(self):
        import fcntl
        self.seed()
        os.makedirs(os.path.join(self.data, "calls"), mode=0o700)
        with open(os.path.join(self.data, "calls", ".lock"), "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            code, out = self.run_cli("pull", "--source", "litepipe", "--since", "0", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("another pull", out.lower())
        self.assertEqual(self.files(), [])

    def test_ssl_context_never_unverified(self):
        import ssl
        ctx = C._ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_ssl_falls_back_to_system_bundle_when_python_has_none(self):
        import ssl
        from unittest import mock
        fake = ssl.DefaultVerifyPaths(None, None, "X", "x", "X", "x")
        seen = []
        env = {k: v for k, v in os.environ.items() if k not in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(ssl, "get_default_verify_paths", lambda: fake), \
                mock.patch.object(C, "_load_fallback_bundle", lambda ctx: seen.append(1) or True):
            C._ssl_context()
        self.assertEqual(seen, [1])


class TestHookRuntime(unittest.TestCase):
    """The real commslib, a temp archive, and CLAUDECODE set: the row must still be written."""

    def test_row_written_inside_claude_code_session(self):
        core = C._comms_module("commslib")
        if core is None:
            self.skipTest("core commslib not found")
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "data")
            cfg = os.path.join(tmp, "c.json")
            db = os.path.join(tmp, "lp.sqlite")
            make_litepipe(db, [meeting(1, "Phone", end="2026-10-30T11:00:00Z",
                                       segs=[("2026-10-30T10:00:01Z", "A", SECRET_TEXT)])])
            json.dump({"data_dir": data, "calls": {"litepipe_db": db, "notes_db": os.path.join(tmp, "none")}},
                      open(cfg, "w"))
            env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
            env.update({"COMMS_KIT_CONFIG": cfg, "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s", "TZ": "UTC"})
            r = subprocess.run([sys.executable, os.path.join(HERE, "calls.py"), "pull", "--source", "litepipe",
                                "--since", "0", "--yes"], env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            rows = []
            for p in glob.glob(os.path.join(data, "store", "*.jsonl")):
                rows += [json.loads(l) for l in open(p) if l.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["channel"], "calls")
            self.assertIn(SECRET_TEXT, rows[0]["body"])
            r2 = subprocess.run([sys.executable, os.path.join(HERE, "calls.py"), "pull", "--source", "litepipe",
                                 "--since", "0", "--yes"], env=env, capture_output=True, text=True, timeout=120)
            rows2 = []
            for p in glob.glob(os.path.join(data, "store", "*.jsonl")):
                rows2 += [json.loads(l) for l in open(p) if l.strip()]
            self.assertEqual(len(rows2), 1)


class TestManifest(unittest.TestCase):
    def test_help_lists_exactly_the_manifest_verbs(self):
        man = json.load(open(os.path.join(HERE, "channel.json")))
        out = subprocess.run([sys.executable, os.path.join(HERE, "calls.py"), "--help"],
                             capture_output=True, text=True).stdout
        import re
        m = re.search(r"\{([a-z,\-]+)\}", out)
        verbs = set(m.group(1).split(","))
        self.assertEqual(verbs, set(man["verbs"]) | set(man.get("ignored", [])))
        self.assertEqual(man["name"], "calls")

    def test_every_config_key_in_example_and_manifest(self):
        man = json.load(open(os.path.join(HERE, "channel.json")))
        ex = json.load(open(os.path.join(HERE, "config.example.json")))
        src = open(os.path.join(HERE, "calls.py")).read()
        import re
        used = set(re.findall(r'kitconfig\.\w+\("(calls\.[a-z_]+)"|_cfg\("(calls\.[a-z_]+)"', src))
        used = {a or b for a, b in used}
        self.assertTrue(used)
        self.assertEqual(used, set(man["config_keys"]))
        for key in used:
            self.assertIn(key.split(".", 1)[1], ex["calls"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
