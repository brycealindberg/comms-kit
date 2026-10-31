#!/usr/bin/env python3
"""E2E: the REAL gws against a throwaway account folder, proving an added Gmail account cannot
touch the main login.

What it proves, in order (each step prints PASS or FAIL):
  1  baseline of the MAIN login: credentials.enc hash + mtime, client_secret.json hash, the file
     list, and the keyring item's modified-date (attributes only; no secret is read)
  2  `gws-as` reaches the THROWAWAY folder: auth status names that folder and the file backend
  3  a data call for an account with no login refuses before gws runs
  4  a stale/undecryptable credentials.enc in the throwaway folder is handled INSIDE that folder:
     the new key is written there, the stale file is removed there, nothing leaks to the main folder
  5  the main folder is byte-identical (credentials.enc hash + mtime) after steps 2-4
  6  the main login still works: auth status says encryption_valid + token_valid, and a REAL
     Gmail read (users messages list, 1 message, read-only) returns a result
  7  the keyring item is unchanged, and the main folder still holds the same files
The throwaway folder is removed at the end. No mail is sent; no second login happens.

Run: python3 e2e_gmail_isolated_dir.py
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HOME = os.path.expanduser("~")
MAIN = os.path.join(HOME, ".config", "gws")
_CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # kit: code path
GWS_AS = next((p for p in (os.path.join(_CODE, "bin", "gws-as"),
                           os.path.join(HOME, ".local", "bin", "gws-as")) if os.path.exists(p)),
              os.path.join(HOME, ".local", "bin", "gws-as"))
EMAIL = "e2e-throwaway@example.org"
RESULTS = []


def step(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + detail) if detail and not ok else ""))
    return ok


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main_snapshot():
    cred = os.path.join(MAIN, "credentials.enc")
    st = os.stat(cred)
    return {"hash": sha(cred), "mtime_ns": st.st_mtime_ns, "size": st.st_size,
            "secret": sha(os.path.join(MAIN, "client_secret.json")),
            "files": sorted(set(os.listdir(MAIN)) - {"token_cache.json"})}


def keyring_mdat():
    """The keyring item's modified-date (attributes only: no -g, no -w, no secret)."""
    p = subprocess.run(["security", "find-generic-password", "-s", "gws-cli"],
                       capture_output=True, text=True)
    m = re.search(r'"mdat"<timedate>=\S+\s+"([^"]+)"', p.stdout)
    return m.group(1) if m else "(unreadable)"


def run(argv, env=None, timeout=90):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(argv, capture_output=True, text=True, env=e, timeout=timeout)


def json_of(text):
    i = text.find("{")
    try:
        return json.JSONDecoder().raw_decode(text[i:])[0] if i >= 0 else None
    except Exception:
        return None


def main():
    if not os.path.isfile(os.path.join(MAIN, "credentials.enc")):
        print("FAIL  main login missing: %s" % MAIN)
        print("RESULT: FAIL")
        return 1
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="gws-e2e-"))
    try:
        root = os.path.join(tmp, "gws-accounts")
        acct = os.path.join(root, EMAIL)
        os.makedirs(acct, mode=0o700)
        os.chmod(root, 0o700)
        shutil.copyfile(os.path.join(MAIN, "client_secret.json"), os.path.join(acct, "client_secret.json"))
        os.chmod(os.path.join(acct, "client_secret.json"), 0o600)
        env = {"GWS_ACCOUNTS_ROOT": root}

        before = main_snapshot()
        mdat_before = keyring_mdat()
        step("1 baseline of the main login taken", True)

        # 2. gws-as reaches the throwaway folder
        r = run([GWS_AS, EMAIL, "auth", "status"], env)
        d = json_of(r.stdout) or {}
        ok = (r.returncode == 0 and os.path.realpath(os.path.dirname(d.get("encrypted_credentials", ""))) == acct
              and d.get("keyring_backend") == "file"
              and d.get("encrypted_credentials_exists") is False
              and os.path.realpath(os.path.dirname(d.get("client_config", ""))) == acct)
        step("2 gws-as reaches the throwaway folder, file backend", ok,
             "rc=%s creds=%s backend=%s" % (r.returncode, d.get("encrypted_credentials"), d.get("keyring_backend")))

        # 3. no login -> refuse before gws
        r = run([GWS_AS, EMAIL, "gmail", "users", "messages", "list", "--params", '{"userId":"me"}'], env)
        step("3 a data call with no login refuses", r.returncode != 0 and "no login yet" in r.stderr,
             "rc=%s err=%s" % (r.returncode, r.stderr[:120]))

        # 4. a stale credentials.enc stays a problem of the throwaway folder only
        adc = os.path.join(HOME, ".config", "gcloud", "application_default_credentials.json")
        if os.path.exists(adc):
            step("4 stale-file containment (skipped: gcloud ADC exists, gws would use it)", True)
        else:
            with open(os.path.join(acct, "credentials.enc"), "wb") as fh:
                fh.write(b"not a real encrypted login " * 4)
            r = run([GWS_AS, EMAIL, "gmail", "users", "messages", "list", "--params", '{"userId":"me","maxResults":1}'], env)
            leaked = [f for f in (".encryption_key",) if os.path.exists(os.path.join(MAIN, f))]
            ok = (r.returncode != 0
                  and os.path.exists(os.path.join(acct, ".encryption_key"))
                  and not os.path.exists(os.path.join(acct, "credentials.enc"))
                  and not leaked)
            step("4 a stale login is dealt with inside the throwaway folder only", ok,
                 "rc=%s key_in_acct=%s stale_left=%s leaked_to_main=%s" % (
                     r.returncode, os.path.exists(os.path.join(acct, ".encryption_key")),
                     os.path.exists(os.path.join(acct, "credentials.enc")), leaked))

        # 5. main untouched after the account work
        mid = main_snapshot()
        step("5 main credentials.enc hash + mtime unchanged",
             mid["hash"] == before["hash"] and mid["mtime_ns"] == before["mtime_ns"]
             and mid["size"] == before["size"] and mid["secret"] == before["secret"],
             "before=%s after=%s" % (before["hash"][:12], mid["hash"][:12]))

        # 6. the main login still works, with a real read
        r = run(["gws", "auth", "status"])
        d = json_of(r.stdout) or {}
        step("6a main auth status: encryption_valid and token_valid",
             d.get("encryption_valid") is True and d.get("token_valid") is True,
             "enc=%s token=%s" % (d.get("encryption_valid"), d.get("token_valid")))
        r = run(["gws", "gmail", "users", "messages", "list", "--params",
                 json.dumps({"userId": "me", "maxResults": 1}), "--format", "json"])
        d = json_of(r.stdout) or {}
        step("6b a real main-account Gmail read returns a result",
             r.returncode == 0 and ("messages" in d or "resultSizeEstimate" in d) and "error" not in d,
             "rc=%s keys=%s" % (r.returncode, sorted(d)[:5]))

        # 7. after the main read too
        after = main_snapshot()
        step("7a main credentials.enc still byte-identical after the main read",
             after["hash"] == before["hash"] and after["mtime_ns"] == before["mtime_ns"])
        step("7b main folder holds the same files, no stray .encryption_key",
             after["files"] == before["files"], "before=%s after=%s" % (before["files"], after["files"]))
        step("7c keyring item unchanged", keyring_mdat() == mdat_before,
             "before=%s after=%s" % (mdat_before, keyring_mdat()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        step("8 throwaway folder removed", not os.path.exists(tmp))
    ok = all(RESULTS)
    print("RESULT: PASS" if ok else "RESULT: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
