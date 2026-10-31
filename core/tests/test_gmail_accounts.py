#!/usr/bin/env python3
"""Tests for connecting more than one Gmail account (leaf 1.4).

Every test runs in a THROWAWAY HOME with a FAKE `gws` first on PATH. The real gws, the real
~/.config/gws login and the real archive are never reached: PATH is only the fake bin dir plus
/usr/bin:/bin, and the sandbox gets its own copy of the comms code.

Ways it can fail (each has a test below):
  * a typo or hostile EMAIL walks out of the accounts folder (../gws, /abs, newline)
  * gws-as runs for an account nobody added, or for one that was never logged in (gws then
    falls through to gcloud's own credentials = a different identity)
  * the file-backend variable reaches the MAIN folder (gws deletes a login it cannot decrypt)
  * `rm` removes the main folder, a symlink target, or runs without --yes
  * a send from an added account is archived without its account, or a main send gains one
  * one dead account stops the main scan, or warns twice, or not at all
  * with no added account the output or the gws calls differ from the old code

Run: python3 test_gmail_accounts.py
"""
import glob
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # kit: code path


def _first_existing(*paths):
    for path in paths:
        if os.path.exists(path):
            return path
    raise unittest.SkipTest("not found: " + paths[0])


# the gws-as wrapper and the capture hook, at their live path or their repo path
REAL_GWS_AS = _first_existing(os.path.join(REAL, "bin", "gws-as"),
                              os.path.expanduser("~/.local/bin/gws-as"))
HOOK = _first_existing(os.path.join(REAL, "..", "hooks", "comms-log.py"),
                       os.path.join(REAL, "..", "integrations", "claude-code", "comms-log.py"))
ORIG_COMMS = "/tmp/gmail-orig/comms"
ORIG_INBOX = "/tmp/gmail-orig/inbox"   # the pre-change script, for the "no accounts" proof

FAKE_GWS = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
d = os.environ.get("GOOGLE_WORKSPACE_CLI_CONFIG_DIR")
home = os.path.expanduser("~")
cfg = d or os.path.join(home, ".config", "gws")
with open(os.environ["FAKE_GWS_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": a, "config_dir": d,
                         "backend": os.environ.get("GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"),
                         "token": os.environ.get("GOOGLE_WORKSPACE_CLI_TOKEN"),
                         "credfile": os.environ.get("GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"),
                         "user_env": os.environ.get("USER")}) + "\n")
name = os.path.basename(cfg.rstrip("/")) if d else "main"
if os.path.exists(os.path.join(cfg, "dead")) and a[:1] != ["auth"]:
    sys.stderr.write("Error: 401 invalid_grant: Token has been expired or revoked.\n")
    sys.exit(1)
if a[:2] == ["auth", "status"]:
    user = "main@example.com"
    uf = os.path.join(cfg, "user")
    if d and os.path.exists(uf):
        user = open(uf).read().strip()
    dead = os.path.exists(os.path.join(cfg, "dead"))
    sys.stderr.write("Using keyring backend: %s\n" % (os.environ.get("GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND") or "keyring"))
    print(json.dumps({"user": user, "token_valid": not dead,
        "encrypted_credentials_exists": os.path.exists(os.path.join(cfg, "credentials.enc")),
        "scopes": ["email", "profile", "https://www.googleapis.com/auth/calendar",
                   "https://www.googleapis.com/auth/gmail.modify",
                   "https://www.googleapis.com/auth/userinfo.email", "openid"]}, indent=2))
    sys.exit(0)
if a[:2] == ["auth", "login"]:
    sys.stderr.write("FAKE LOGIN\n")
    sys.exit(0)
if a[:4] == ["gmail", "users", "messages", "list"]:
    q = json.loads(a[a.index("--params") + 1]).get("q", "")
    if "is:unread" in q:
        print(json.dumps({"messages": [{"id": "o-" + name, "threadId": "oth-" + name}]}))
    elif "in:sent" in q:
        print(json.dumps({"messages": [{"id": "sent-1"}]}))
    else:
        print(json.dumps({"messages": [{"id": "id-" + name}]}))
    sys.exit(0)
if a[:2] == ["gmail", "+read"]:
    mid = a[a.index("--id") + 1]
    print(json.dumps({"from": "client@acme.example.com", "subject": "Subj " + mid,
                      "date": "Fri, 30 Oct 2026 10:00:00 -0700"}))
    sys.exit(0)
if a[:4] == ["gmail", "users", "threads", "list"]:
    q = json.loads(a[a.index("--params") + 1]).get("q", "")
    print(json.dumps({"threads": [{"id": ("old-" if "-from:me" in q else "th-") + name}]}))
    sys.exit(0)
if a[:4] == ["gmail", "users", "threads", "get"]:
    p = json.loads(a[a.index("--params") + 1])
    print(json.dumps({"id": p["id"], "messages": [{"id": "msg-" + p["id"],
        "internalDate": str(int(__import__("time").time() * 1000) - 3600000), "snippet": "hello there", "labelIds": ["INBOX"],
        "payload": {"headers": [{"name": "From", "value": "Client <client@acme.example.com>"},
                                {"name": "To", "value": "me@x.example.com"},
                                {"name": "Subject", "value": "Question " + p["id"]}]}}]}))
    sys.exit(0)
if a[:4] == ["gmail", "users", "messages", "get"]:
    print(json.dumps({"internalDate": str(int(__import__("time").time() * 1000) - 3600000),
                      "snippet": "see you", "payload": {"headers": [
        {"name": "To", "value": "client@acme.example.com"}, {"name": "From", "value": "Pal <pal@friend.example.com>"},
        {"name": "Subject", "value": "Lunch"}]}}))
    sys.exit(0)
print(json.dumps({"ok": True, "args": a}))
'''

ACCT_A = "alpha@example.org"
ACCT_B = "beta@example.org"


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class Sandbox(unittest.TestCase):
    """A throwaway HOME holding: a fake main gws folder, a copy of the comms code, a fake gws."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="gmailacct-"))
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "fakebin")
        self.log = os.path.join(self.tmp, "gws.log")
        os.makedirs(self.bin)
        self.main = os.path.join(self.home, ".config", "gws")
        os.makedirs(self.main)
        os.chmod(self.main, 0o700)
        with open(os.path.join(self.main, "client_secret.json"), "w") as fh:
            fh.write('{"installed": {"client_id": "x.apps.googleusercontent.com"}}')
        os.chmod(os.path.join(self.main, "client_secret.json"), 0o600)
        with open(os.path.join(self.main, "credentials.enc"), "wb") as fh:
            fh.write(b"MAIN-CREDENTIALS-DO-NOT-TOUCH")
        self.main_hash = sha(os.path.join(self.main, "credentials.enc"))
        gws = os.path.join(self.bin, "gws")
        with open(gws, "w") as fh:
            fh.write(FAKE_GWS)
        os.chmod(gws, 0o755)
        self.comm = os.path.join(self.home, ".claude", "communications")
        os.makedirs(os.path.join(self.comm, "bin"))
        for f in glob.glob(REAL + "/*.py"):
            shutil.copy(f, self.comm)
        shutil.copy(REAL + "/bin/comms", self.comm + "/bin/comms")
        if os.path.exists(REAL + "/bin/inbox"):   # the inbox script is private, not part of the kit
            shutil.copy(REAL + "/bin/inbox", self.comm + "/bin/inbox")
        os.makedirs(os.path.join(self.home, ".local", "bin"))
        self.gws_as = os.path.join(self.home, ".local", "bin", "gws-as")
        shutil.copy(REAL_GWS_AS, self.gws_as)
        # the sandbox archive lives in its own copy of the code folder, whatever HOME holds
        self.config = os.path.join(self.tmp, "kit-config.json")
        with open(self.config, "w") as fh:
            json.dump({"data_dir": self.comm}, fh)
        self.env = {"HOME": self.home, "USER": "tester", "LOGNAME": "tester",
                    "COMMS_KIT_CONFIG": self.config,
                    # the sandbox gws-as must load the sandbox code, never the owner's real home
                    "COMMS_KIT_CORE": self.comm,
                    "PATH": self.bin + ":/usr/bin:/bin", "FAKE_GWS_LOG": self.log,
                    "LANG": "en_US.UTF-8"}
        self.root = os.path.join(self.home, ".config", "gws-accounts")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers
    def run_cmd(self, argv, env=None, cwd=None, inp=None):
        e = dict(self.env)
        e.update(env or {})
        return subprocess.run(argv, capture_output=True, text=True, env=e, cwd=cwd,
                              input=inp, timeout=60)

    def comms(self, *args, cwd=None, script=None):
        return self.run_cmd([sys.executable, script or self.comm + "/bin/comms"] + list(args), cwd=cwd)

    def gws_calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [json.loads(x) for x in fh.read().splitlines() if x.strip()]

    def add_account(self, email, logged_in=True, dead=False, user=None):
        d = os.path.join(self.root, email)
        os.makedirs(d, mode=0o700, exist_ok=True)
        shutil.copy(os.path.join(self.main, "client_secret.json"), d)
        if logged_in:
            with open(os.path.join(d, "credentials.enc"), "wb") as fh:
                fh.write(b"ACCT-" + email.encode())
        if dead:
            open(os.path.join(d, "dead"), "w").close()
        with open(os.path.join(d, "user"), "w") as fh:
            fh.write(user or email)
        return d

    def project(self):
        """One client with one address, so `comms unread` runs a Gmail search."""
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo, exist_ok=True)
        for name, obj in (("project_repos.json", {repo: "acme"}),
                          ("routes.json", {"client@acme.example.com": {"project": "acme", "label": "Acme"}}),
                          ("threads.json", {})):
            with open(self.comm + "/" + name, "w") as fh:
                json.dump(obj, fh)
        return repo


# ------------------------------------------------------------------ validation / gws-as

HOSTILE = ["../gws", "..", "/Users/you/.config/gws", "a/b@example.org", "x@example.org/../../gws",
           "", "a@b", "a b@example.org", "-x@example.org", ".hidden@example.org",
           "a@example.org\n", "a@example.org\x00", "$(id)@example.org", "a@example.org;ls",
           "x" * 300 + "@example.org", "main", "gws", "~", "a@@example.org"]


class Validation(Sandbox):
    def test_hostile_emails_are_refused_by_the_validator(self):
        import importlib
        sys.path.insert(0, REAL)
        GA = importlib.import_module("gmail_accounts")
        for bad in HOSTILE:
            with self.assertRaises(GA.AccountError, msg=repr(bad)):
                GA.valid_email(bad)

    def test_good_email_is_lowercased(self):
        sys.path.insert(0, REAL)
        import gmail_accounts as GA
        self.assertEqual(GA.valid_email("Some.One+tag@Example.ORG"), "some.one+tag@example.org")


class GwsAsRefuses(Sandbox):
    def test_unknown_account_refuses_and_never_calls_gws(self):
        r = self.run_cmd([self.gws_as, "nobody@example.org", "gmail", "users", "messages", "list"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nobody@example.org", r.stderr)
        self.assertEqual(self.gws_calls(), [])

    def test_hostile_emails_refuse_and_never_call_gws(self):
        self.add_account(ACCT_A)
        for bad in [h for h in HOSTILE if "\x00" not in h]:
            r = self.run_cmd([self.gws_as, bad, "gmail", "users", "messages", "list"])
            self.assertNotEqual(r.returncode, 0, msg=repr(bad))
        self.assertEqual(self.gws_calls(), [])
        self.assertEqual(sha(self.main + "/credentials.enc"), self.main_hash)

    def test_missing_arguments_refuse(self):
        self.assertNotEqual(self.run_cmd([self.gws_as]).returncode, 0)
        self.add_account(ACCT_A)
        self.assertNotEqual(self.run_cmd([self.gws_as, ACCT_A]).returncode, 0)
        self.assertEqual(self.gws_calls(), [])

    def test_account_never_logged_in_refuses_a_data_call(self):
        # without credentials gws would fall back to gcloud's own login = another identity
        self.add_account(ACCT_A, logged_in=False)
        r = self.run_cmd([self.gws_as, ACCT_A, "gmail", "users", "messages", "list"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no login yet", r.stderr)
        self.assertEqual(self.gws_calls(), [])

    def test_login_is_allowed_for_a_fresh_account(self):
        self.add_account(ACCT_A, logged_in=False)
        r = self.run_cmd([self.gws_as, ACCT_A, "auth", "login", "--scopes", "x"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.gws_calls()[0]["config_dir"], self.root + "/" + ACCT_A)

    def test_account_folder_that_is_a_symlink_refuses(self):
        os.makedirs(self.root)
        os.symlink(self.main, os.path.join(self.root, ACCT_A))
        r = self.run_cmd([self.gws_as, ACCT_A, "gmail", "users", "messages", "list"])
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.gws_calls(), [])

    def test_accounts_root_override_inside_main_refuses(self):
        os.makedirs(self.main + "/inner/" + ACCT_A)
        for root in (self.main, self.main + "/inner", os.path.dirname(self.main) + "/gws/../gws"):
            r = self.run_cmd([self.gws_as, ACCT_A, "gmail", "x"], env={"GWS_ACCOUNTS_ROOT": root})
            self.assertNotEqual(r.returncode, 0, msg=root)
        self.assertEqual(self.gws_calls(), [])


class GwsAsRuns(Sandbox):
    def test_runs_gws_in_the_account_folder_with_the_file_backend(self):
        self.add_account(ACCT_A)
        r = self.run_cmd([self.gws_as, ACCT_A.upper(), "gmail", "users", "messages", "list",
                          "--params", '{"userId":"me"}'],
                         env={"GOOGLE_WORKSPACE_CLI_TOKEN": "leak", "GOOGLE_APPLICATION_CREDENTIALS": "/x",
                              "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE": "/y"})
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self.gws_calls()[0]
        self.assertEqual(c["config_dir"], self.root + "/" + ACCT_A)
        self.assertEqual(c["backend"], "file")
        self.assertEqual(c["argv"], ["gmail", "users", "messages", "list", "--params", '{"userId":"me"}'])
        # a leaked variable must not swap the identity
        self.assertIsNone(c["token"])
        self.assertIsNone(c["credfile"])
        # full env kept: gws needs USER to find its key, and a stripped env makes it delete a login
        self.assertEqual(c["user_env"], "tester")

    def test_exit_code_passes_through(self):
        self.add_account(ACCT_A, dead=True)
        r = self.run_cmd([self.gws_as, ACCT_A, "gmail", "users", "messages", "list"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("invalid_grant", r.stderr)

    def test_main_folder_is_untouched(self):
        self.add_account(ACCT_A)
        self.run_cmd([self.gws_as, ACCT_A, "auth", "status"])
        self.assertEqual(sha(self.main + "/credentials.enc"), self.main_hash)
        self.assertEqual(sorted(os.listdir(self.main)), ["client_secret.json", "credentials.enc"])


# ------------------------------------------------------------------ comms gmail-account

class AccountCommands(Sandbox):
    def test_add_creates_the_folder_and_prints_the_login_line(self):
        r = self.comms("gmail-account", "add", "New.Person@Example.org")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = self.root + "/new.person@example.org"
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual(open(d + "/client_secret.json").read(),
                         open(self.main + "/client_secret.json").read())
        self.assertEqual(stat.S_IMODE(os.stat(d + "/client_secret.json").st_mode), 0o600)
        self.assertIn("! gws-as new.person@example.org auth login --scopes ", r.stdout)
        # the scope list of the main login, without the bare identity names gws adds itself
        line = [x for x in r.stdout.splitlines() if x.startswith("! gws-as")][0]
        self.assertIn("https://www.googleapis.com/auth/gmail.modify", line)
        self.assertIn("https://www.googleapis.com/auth/calendar", line)
        self.assertNotIn("userinfo", line)
        self.assertNotIn(",email", line)
        self.assertIn("Testing", r.stdout)            # the test-user line
        self.assertIn("test-user", r.stdout)
        # no browser, no login: only a status read of the MAIN account for the scope list
        for c in self.gws_calls():
            self.assertNotEqual(c["argv"][:2], ["auth", "login"])
        self.assertEqual(sha(self.main + "/credentials.enc"), self.main_hash)

    def test_add_marks_the_address_as_ours(self):
        self.comms("gmail-account", "add", ACCT_A)
        self.assertIn(ACCT_A, json.load(open(self.comm + "/self.json"))["emails"])

    def test_add_refuses_hostile_and_main_address(self):
        for bad in ("../gws", "a/b@example.org", "x@y"):
            r = self.comms("gmail-account", "add", bad)
            self.assertNotEqual(r.returncode, 0, msg=bad)
        r = self.comms("gmail-account", "add", "main@example.com")      # = the main login's user
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("main", r.stderr + r.stdout)
        self.assertFalse(os.path.exists(self.root + "/main@example.com"))

    def test_add_twice_keeps_the_existing_login(self):
        d = self.add_account(ACCT_A)
        r = self.comms("gmail-account", "add", ACCT_A)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(open(d + "/credentials.enc", "rb").read(), b"ACCT-" + ACCT_A.encode())

    def test_list_with_none_shows_only_main(self):
        r = self.comms("gmail-account", "list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("main", r.stdout)
        self.assertNotIn("example.org", r.stdout)

    def test_list_checks_each_token_live(self):
        self.add_account(ACCT_A)
        self.add_account(ACCT_B, dead=True)
        out = self.comms("gmail-account", "list").stdout
        a = [x for x in out.splitlines() if ACCT_A in x][0]
        b = [x for x in out.splitlines() if ACCT_B in x][0]
        self.assertIn("token_valid=true", a)
        self.assertIn("token_valid=false", b)
        self.assertEqual(sha(self.main + "/credentials.enc"), self.main_hash)

    def test_list_flags_a_login_for_the_wrong_address(self):
        # the owner clicked another Google account in the browser
        self.add_account(ACCT_A, user="someone-else@example.org")
        out = self.comms("gmail-account", "list").stdout
        self.assertIn("WRONG ACCOUNT", [x for x in out.splitlines() if ACCT_A in x][0])

    def test_list_flags_a_not_yet_connected_account(self):
        self.add_account(ACCT_A, logged_in=False)
        line = [x for x in self.comms("gmail-account", "list").stdout.splitlines() if ACCT_A in x][0]
        self.assertIn("not connected", line)

    def test_rm_needs_yes(self):
        d = self.add_account(ACCT_A)
        r = self.comms("gmail-account", "rm", ACCT_A)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--yes", r.stdout + r.stderr)
        self.assertTrue(os.path.isdir(d))

    def test_rm_removes_only_that_account(self):
        a = self.add_account(ACCT_A)
        b = self.add_account(ACCT_B)
        r = self.comms("gmail-account", "rm", ACCT_A, "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(a))
        self.assertTrue(os.path.isdir(b))
        self.assertEqual(sha(self.main + "/credentials.enc"), self.main_hash)

    def test_rm_cannot_reach_the_main_folder(self):
        self.add_account(ACCT_A)
        for bad in ("../gws", "main", "gws", "main@example.com", "/", self.main, "..", "."):
            r = self.comms("gmail-account", "rm", bad, "--yes")
            self.assertNotEqual(r.returncode, 0, msg=bad)
        self.assertTrue(os.path.isdir(self.main))
        self.assertEqual(sha(self.main + "/credentials.enc"), self.main_hash)
        self.assertTrue(os.path.isdir(self.root + "/" + ACCT_A))

    def test_rm_of_a_symlinked_folder_never_follows_it(self):
        os.makedirs(self.root, mode=0o700)
        os.symlink(self.main, os.path.join(self.root, ACCT_A))
        r = self.comms("gmail-account", "rm", ACCT_A, "--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(sha(self.main + "/credentials.enc"), self.main_hash)
        self.assertTrue(os.path.exists(self.main + "/client_secret.json"))

    def test_doctor_checks_the_wrapper_and_that_the_hook_sees_its_sends(self):
        self.add_account(ACCT_A)
        out = self.comms("doctor").stdout
        line = [x for x in out.splitlines() if x.startswith("gmail-acct")]
        self.assertEqual(len(line), 1, msg=out)
        self.assertIn(" ok ", line[0])
        self.assertIn("1 added account", line[0])

    def test_rm_unknown_account_is_an_error(self):
        r = self.comms("gmail-account", "rm", "ghost@example.org", "--yes")
        self.assertNotEqual(r.returncode, 0)


# ------------------------------------------------------------------ capture

def hook_payload(command, stdout='{"id": "18c0ffee", "threadId": "18c0ffee"}'):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp",
            "session_id": "abcd1234efgh",
            "tool_response": {"stdout": stdout, "stderr": ""}}


class Capture(Sandbox):
    def setUp(self):
        super().setUp()
        os.makedirs(self.home + "/.claude/hooks")
        shutil.copy(HOOK, self.home + "/.claude/hooks/comms-log.py")

    def capture(self, command):
        """Run the REAL hook on a command in the sandbox; return the rows it archived."""
        for f in glob.glob(self.comm + "/store/*.jsonl"):
            os.unlink(f)
        r = self.run_cmd([sys.executable, self.home + "/.claude/hooks/comms-log.py"],
                         inp=json.dumps(hook_payload(command)))
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = []
        for f in glob.glob(self.comm + "/store/*.jsonl"):
            rows += [json.loads(x) for x in open(f).read().splitlines() if x.strip()]
        return rows

    SEND = "gmail +send --to client@acme.example.com --subject Hi --body 'hello there'"

    def test_gws_as_form_stamps_the_account(self):
        rows = self.capture("gws-as %s %s" % (ACCT_A, self.SEND))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel"], "gmail")
        self.assertEqual(rows[0]["dir"], "send")
        self.assertEqual(rows[0]["account"], ACCT_A)
        self.assertEqual(rows[0]["target"], "client@acme.example.com")

    def test_env_form_stamps_the_account(self):
        rows = self.capture("GOOGLE_WORKSPACE_CLI_CONFIG_DIR=%s/%s GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file "
                            "gws %s" % (self.root, ACCT_B, self.SEND))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["account"], ACCT_B)

    def test_env_form_with_tilde_and_quotes(self):
        rows = self.capture("GOOGLE_WORKSPACE_CLI_CONFIG_DIR=\"$HOME/.config/gws-accounts/%s\" gws %s"
                            % (ACCT_B, self.SEND))
        self.assertEqual(rows[0]["account"], ACCT_B)

    def test_wrappers_do_not_hide_the_account(self):
        rows = self.capture("timeout 30 gws-as %s %s" % (ACCT_A, self.SEND))
        self.assertEqual(rows[0]["account"], ACCT_A)

    def test_main_row_has_no_account_field(self):
        rows = self.capture("gws %s" % self.SEND)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("account", rows[0])

    def test_env_form_pointing_at_the_main_folder_is_main(self):
        rows = self.capture("GOOGLE_WORKSPACE_CLI_CONFIG_DIR=%s gws %s" % (self.main, self.SEND))
        self.assertEqual(len(rows), 1)
        self.assertNotIn("account", rows[0])

    def test_unresolvable_account_is_marked_not_called_main(self):
        rows = self.capture('gws-as "$ACCT" %s' % self.SEND)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["account"], "unresolved")

    def test_reads_from_an_added_account_are_stamped_too(self):
        rows = self.capture("gws-as %s gmail +read --id 18c0ffee" % ACCT_A)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["account"], ACCT_A)
        self.assertEqual(rows[0]["dir"], "read")

    def test_a_variable_exported_earlier_in_the_line_is_not_called_main(self):
        # `export VAR=...; gws gmail ...` runs the send as the account, but the segment shows no
        # trace of it: archive it as unresolved, never as the main account
        rows = self.capture("export GOOGLE_WORKSPACE_CLI_CONFIG_DIR=%s/%s; gws %s" % (self.root, ACCT_B, self.SEND))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["account"], "unresolved")

    def test_help_and_other_channels_are_unchanged(self):
        self.assertEqual(self.capture("gws-as %s gmail --help" % ACCT_A), [])
        self.assertEqual(self.capture("gws-as %s auth status" % ACCT_A), [])
        self.assertEqual(self.capture("echo gws-as %s gmail +send" % ACCT_A), [])

    def test_a_second_account_address_counts_as_ours_when_filing_a_reply(self):
        # +reply names only a message id: the correspondent is read from the headers, and the
        # account's own address must not be mistaken for the other party
        self.add_account(ACCT_A)
        out = ('{"headers": {"From": "%s", "To": "client@acme.example.com", "Subject": "Re: Hi"}}' % ACCT_A)
        for f in glob.glob(self.comm + "/store/*.jsonl"):
            os.unlink(f)
        payload = hook_payload("gws-as %s gmail +reply --message-id 18c0ffee --body ok" % ACCT_A, out)
        self.run_cmd([sys.executable, self.home + "/.claude/hooks/comms-log.py"], inp=json.dumps(payload))
        rows = []
        for f in glob.glob(self.comm + "/store/*.jsonl"):
            rows += [json.loads(x) for x in open(f).read().splitlines() if x.strip()]
        self.assertTrue(rows)
        self.assertNotEqual(rows[0]["target"], ACCT_A)


class CommandParsing(unittest.TestCase):
    """Pure parsing of the command string (no sandbox needed)."""

    def setUp(self):
        sys.path.insert(0, REAL)
        import commslib
        self.C = commslib

    def test_channel_of_sees_gws_as(self):
        argv = self.C.exec_argv("gws-as a@example.org gmail +send --to x@y.example.com")
        self.assertEqual(self.C.channel_of(argv), "gmail")

    def test_channel_of_needs_gmail_after_the_email(self):
        self.assertIsNone(self.C.channel_of(self.C.exec_argv("gws-as a@example.org drive files list")))
        self.assertIsNone(self.C.channel_of(self.C.exec_argv("gws-as a@example.org")))

    def test_verb_is_parsed_after_gws_as(self):
        info = self.C.parse("gmail", "gws-as a@example.org gmail +reply --message-id abc --body hi")
        self.assertEqual(info["verb"], "+reply")

    def test_plain_gws_is_unchanged(self):
        self.assertEqual(self.C.channel_of(self.C.exec_argv("gws gmail +send --to x@y.example.com")), "gmail")
        self.assertIsNone(self.C.channel_of(self.C.exec_argv("gws drive files list")))


# ------------------------------------------------------------------ reading

class CommsUnread(Sandbox):
    def unread(self, *extra, script=None):
        repo = self.project()
        return self.comms("unread", "--days", "30", *extra, cwd=repo, script=script)

    LIST = ["gmail", "users", "messages", "list", "--params",
            '{"userId": "me", "q": "from:(client@acme.example.com) newer_than:30d", "maxResults": 6}',
            "--format", "json"]
    READ = ["gmail", "+read", "--id", "id-main", "--headers", "--format", "json"]

    def test_no_accounts_output_and_gws_calls_are_the_old_ones(self):
        """Golden, written from the pre-change script's real output (2026-10-31)."""
        line = r"\[\d{4}-\d\d-\d\d \d\d:\d\d \w+\] "
        want = {(): r"^client: acme   \(last 30 days, gmail search\)\n\ngmail \. acme\n  " + line
                    + r"client@acme\.example\.com - Subj id-main\n$",
                ("--merged",): r"^client: acme   \(last 30 days, gmail search\)\n  " + line
                               + r"gmail/client@acme\.example\.com: Subj id-main\n$"}
        for extra, pattern in want.items():
            if os.path.exists(self.log):
                os.unlink(self.log)
            new = self.unread(*extra)
            self.assertEqual(new.returncode, 0, new.stderr)
            self.assertRegex(new.stdout, pattern)
            self.assertEqual([c["argv"] for c in self.gws_calls()], [self.LIST, self.READ])
            self.assertTrue(all(c["config_dir"] is None and c["backend"] is None for c in self.gws_calls()))

    @unittest.skipUnless(os.path.exists(ORIG_COMMS), "pre-change copy of comms not kept")
    def test_no_accounts_output_equals_the_pre_change_script(self):
        for extra in ((), ("--merged",)):
            if os.path.exists(self.log):
                os.unlink(self.log)
            new = self.unread(*extra)
            new_calls = self.gws_calls()
            if os.path.exists(self.log):
                os.unlink(self.log)
            old = self.unread(*extra, script=ORIG_COMMS)
            self.assertEqual(new.stdout, old.stdout, msg=str(extra))
            self.assertEqual(new_calls, self.gws_calls(), msg=str(extra))

    def test_added_account_is_read_and_labelled(self):
        self.add_account(ACCT_A)
        for extra in ((), ("--merged",)):
            out = self.unread(*extra).stdout
            self.assertIn("Subj id-main", out)
            self.assertIn("Subj id-" + ACCT_A, out)
            main_line = [x for x in out.splitlines() if "id-main" in x][0]
            a_line = [x for x in out.splitlines() if "id-" + ACCT_A in x][0]
            self.assertIn("account: main", main_line)
            self.assertIn("account: " + ACCT_A, a_line)

    def test_a_dead_account_warns_once_and_main_still_scans(self):
        self.add_account(ACCT_A)
        self.add_account(ACCT_B, dead=True)
        for extra in ((), ("--merged",)):
            r = self.unread(*extra)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Subj id-main", r.stdout)
            self.assertIn("Subj id-" + ACCT_A, r.stdout)
            warn = [x for x in r.stdout.splitlines() if ACCT_B in x]
            self.assertEqual(len(warn), 1, msg=r.stdout)          # ONE warning line naming it
            self.assertIn("comms gmail-account add " + ACCT_B, warn[0])

    def test_an_account_never_logged_in_warns_and_main_still_scans(self):
        self.add_account(ACCT_A, logged_in=False)
        r = self.unread()
        self.assertIn("Subj id-main", r.stdout)
        self.assertEqual(len([x for x in r.stdout.splitlines() if ACCT_A in x]), 1, msg=r.stdout)

    def test_a_dead_main_login_is_still_reported_when_an_account_has_mail(self):
        self.add_account(ACCT_A)
        open(self.main + "/dead", "w").close()
        out = self.unread().stdout
        self.assertIn("Subj id-" + ACCT_A, out)
        self.assertIn("gmail unavailable", out)          # the main failure is not swallowed
        self.assertIn("401", out)

    def test_find_shows_the_account_of_an_archived_row(self):
        rows = [{"ts": 1790000000.0, "et": "2026-10-31T09:00:00 PDT", "channel": "gmail", "verb": "+send",
                 "dir": "send", "target": "client@acme.example.com", "text": "hello unique-needle", "body": "",
                 "account": ACCT_A},
                {"ts": 1790000100.0, "et": "2026-10-31T09:01:00 PDT", "channel": "gmail", "verb": "+send",
                 "dir": "send", "target": "client@acme.example.com", "text": "second unique-needle", "body": ""}]
        os.makedirs(self.comm + "/store", exist_ok=True)
        with open(self.comm + "/store/2026-10.jsonl", "w") as fh:
            fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
        out = self.comms("find", "unique-needle").stdout
        a = [x for x in out.splitlines() if "send" in x and "09:00" in x][0]
        b = [x for x in out.splitlines() if "send" in x and "09:01" in x][0]
        self.assertIn("account: " + ACCT_A, a)
        self.assertNotIn("account", b)

    def test_the_main_folder_is_never_touched_by_a_scan(self):
        self.add_account(ACCT_A)
        self.add_account(ACCT_B, dead=True)
        self.unread()
        self.assertEqual(sha(self.main + "/credentials.enc"), self.main_hash)
        for c in self.gws_calls():
            if c["config_dir"] is None:
                self.assertIsNone(c["backend"])      # main calls never see the file backend

    BACKFILL_DRIVER = r'''
import json, sys
sys.path.insert(0, %(comm)r)
src = open(%(comm)r + "/bin/comms").read().split("\na = p.parse_args()")[0]
ns = {"__name__": "comms_cli", "__file__": %(comm)r + "/bin/comms"}
exec(compile(src, "comms", "exec"), ns)
pl, gone = ns["_get_gmail"]("m1", 10, account=%(acct)r)
print(json.dumps({"payload": pl is not None, "gone": gone}))
'''

    def test_backfill_reads_an_added_accounts_message_with_that_account(self):
        # a row sent from an added account must be fetched back through gws-as, not through main
        # (main would answer 404 and the row would be marked gone for ever)
        self.add_account(ACCT_A)
        for acct, want in ((ACCT_A, self.root + "/" + ACCT_A), (None, None)):
            if os.path.exists(self.log):
                os.unlink(self.log)
            r = self.run_cmd([sys.executable, "-c", self.BACKFILL_DRIVER % {"comm": self.comm, "acct": acct}])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(json.loads(r.stdout.strip().splitlines()[-1])["payload"])
            self.assertEqual(self.gws_calls()[0]["config_dir"], want)

    def test_backfill_of_a_removed_account_is_not_gone(self):
        # the account folder was removed: gws-as refuses, which must stay retryable, not "gone"
        r = self.run_cmd([sys.executable, "-c", self.BACKFILL_DRIVER % {"comm": self.comm, "acct": "ghost@example.org"}])
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(got, {"payload": False, "gone": False})


@unittest.skipUnless(os.path.exists(REAL + "/bin/inbox"), "bin/inbox is private, not in the kit")
class InboxScan(Sandbox):
    DRIVER = r'''
import importlib.machinery, importlib.util, json, sys
sys.path.insert(0, %(comm)r)
ld = importlib.machinery.SourceFileLoader("inbox_cli", %(script)r)
spec = importlib.util.spec_from_loader("inbox_cli", ld)
m = importlib.util.module_from_spec(spec)
ld.exec_module(m)
sels = [("acme", "Acme", "gmail", "email", "client@acme.example.com", None)]
out, tids = m.scan_gmail(sels, 30, {"acme": "Acme"})
notes = list(getattr(m, "GMAIL_NOTES", []) or [])
print(json.dumps({"keys": [o["key"] for o in out], "accounts": [o.get("account") for o in out],
                  "labels": [o["label"] for o in out], "tids": sorted(tids), "notes": notes}))
'''

    def scan(self, script=None):
        self.project()
        r = self.run_cmd([sys.executable, "-c", self.DRIVER % {"comm": self.comm,
                                                                "script": script or self.comm + "/bin/inbox"}])
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_no_accounts_scan_is_the_old_scan(self):
        got = self.scan()
        self.assertEqual(got["keys"], ["gmail:th-main"])
        self.assertEqual(got["accounts"], [None])
        self.assertEqual(got["labels"], ["Acme"])
        self.assertEqual(got["notes"], [])
        self.assertTrue(all(c["config_dir"] is None for c in self.gws_calls()))

    def test_no_accounts_scan_makes_the_old_gws_calls(self):
        self.scan()
        self.assertEqual([c["argv"][:4] for c in self.gws_calls()],
                         [["gmail", "users", "threads", "list"], ["gmail", "users", "threads", "get"]])
        self.assertTrue(all(c["config_dir"] is None and c["backend"] is None for c in self.gws_calls()))

    @unittest.skipUnless(os.path.exists(ORIG_INBOX), "pre-change copy of inbox not kept")
    def test_no_accounts_scan_makes_the_same_gws_calls_as_the_old_script(self):
        new = self.scan()
        new_calls = self.gws_calls()
        os.unlink(self.log)
        old = self.scan(script=ORIG_INBOX)
        self.assertEqual(new["keys"], old["keys"])
        self.assertEqual(new["tids"], old["tids"])
        self.assertEqual(new_calls, self.gws_calls())

    def test_added_account_threads_carry_the_account(self):
        self.add_account(ACCT_A)
        got = self.scan()
        self.assertEqual(sorted(got["keys"]), sorted(["gmail:th-main", "gmail:%s:th-%s" % (ACCT_A, ACCT_A)]))
        by_key = dict(zip(got["keys"], got["accounts"]))
        self.assertIsNone(by_key["gmail:th-main"])
        self.assertEqual(by_key["gmail:%s:th-%s" % (ACCT_A, ACCT_A)], ACCT_A)

    def test_a_dead_account_is_a_note_and_main_still_scans(self):
        self.add_account(ACCT_A, dead=True)
        got = self.scan()
        self.assertEqual(got["keys"], ["gmail:th-main"])
        self.assertEqual(len(got["notes"]), 1)
        self.assertIn(ACCT_A, got["notes"][0])


@unittest.skipUnless(os.path.exists(REAL + "/bin/inbox"), "bin/inbox is private, not in the kit")
class InboxOthersAndSend(Sandbox):
    DRIVER = r'''
import importlib.machinery, importlib.util, json, sys
sys.path.insert(0, %(comm)r)
ld = importlib.machinery.SourceFileLoader("inbox_cli", %(comm)r + "/bin/inbox")
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("inbox_cli", ld))
ld.exec_module(m)
sels = [("acme", "Acme", "gmail", "email", "client@acme.example.com", None)]
out, tids = m.scan_gmail(sels, 30, {"acme": "Acme"})
others = m.gmail_others(tids)
others_old = m.gmail_others_old(tids, 30)
print(json.dumps({"others": [(o["key"], o.get("account"), o["text"][:40]) for o in others],
                  "old": [(o["key"], o.get("account")) for o in others_old],
                  "notes": list(m.GMAIL_NOTES)}))
'''

    SEND_DRIVER = r'''
import importlib.machinery, importlib.util, json, sys
sys.path.insert(0, %(comm)r)
ld = importlib.machinery.SourceFileLoader("inbox_cli", %(comm)r + "/bin/inbox")
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("inbox_cli", ld))
ld.exec_module(m)
res = m.gmail_send("msg-1", "Thanks, got it.", preview=True, account=%(acct)r)
print(json.dumps({"code": res[0][2]}))
body = m.gmail_body("msg-1", %(acct)r)
msgs = m.fresh_messages({"channel": "gmail", "key": "k", "thread_id": "th-1", "account": %(acct)r})
print(json.dumps({"fresh": len(msgs)}))
'''

    def drive(self, src, **kw):
        self.project()
        r = self.run_cmd([sys.executable, "-c", src % dict({"comm": self.comm}, **kw)])
        self.assertEqual(r.returncode, 0, r.stderr)
        return [json.loads(x) for x in r.stdout.strip().splitlines() if x.startswith("{")]

    def test_others_cover_the_account_and_warn_once_for_a_dead_one(self):
        self.add_account(ACCT_A)
        self.add_account(ACCT_B, dead=True)
        got = self.drive(self.DRIVER)[-1]
        keys = dict((k, acct) for k, acct, _t in got["others"])
        self.assertIn("gmail:oth-main", keys)
        self.assertIsNone(keys["gmail:oth-main"])
        self.assertEqual(keys["gmail:%s:oth-%s" % (ACCT_A, ACCT_A)], ACCT_A)
        self.assertTrue(any(t.startswith("[via %s]" % ACCT_A) for _k, _a, t in got["others"]))
        self.assertTrue(any(k.startswith("gmail:%s:" % ACCT_A) for k, _a in got["old"]))
        # scan + others + old others ask about the dead account three times; ONE warning
        self.assertEqual(len([n for n in got["notes"] if ACCT_B in n]), 1, msg=got["notes"])
        self.assertFalse(any(ACCT_B in k for k, _a, _t in got["others"]))

    def test_a_login_for_the_wrong_address_is_skipped_not_scanned_twice(self):
        self.add_account(ACCT_A, user="main@example.com")      # clicked the main Google account
        got = self.drive(self.DRIVER)[-1]
        self.assertFalse(any(k.startswith("gmail:%s" % ACCT_A) for k, _a, _t in got["others"]))
        warn = [n for n in got["notes"] if ACCT_A in n]
        self.assertEqual(len(warn), 1)
        self.assertIn("main@example.com", warn[0])

    def test_a_reply_goes_out_through_the_threads_own_account(self):
        self.add_account(ACCT_A)
        got = self.drive(self.SEND_DRIVER, acct=ACCT_A)
        self.assertEqual(got[0]["code"], 0)
        calls = [c for c in self.gws_calls() if c["argv"][:2] == ["gmail", "+reply-all"]]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["config_dir"], self.root + "/" + ACCT_A)
        self.assertIn("--dry-run", calls[0]["argv"])
        # the body read and the live re-read of the thread use the account too
        for c in self.gws_calls():
            self.assertEqual(c["config_dir"], self.root + "/" + ACCT_A, msg=c["argv"])

    def test_a_reply_on_a_main_thread_stays_plain_gws(self):
        got = self.drive(self.SEND_DRIVER, acct=None)
        self.assertEqual(got[0]["code"], 0)
        for c in self.gws_calls():
            self.assertIsNone(c["config_dir"])
            self.assertIsNone(c["backend"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
