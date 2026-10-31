#!/usr/bin/env python3
"""More than one Gmail account for the comms tools (leaf 1.4).

The MAIN account is the one `gws` uses with no wrapper: ~/.config/gws. It is never moved,
copied or rewritten here. Every ADDED account lives in its own folder,
~/.config/gws-accounts/<email>/ (mode 700), with its own copy of client_secret.json, and is
reached only through `gws-as EMAIL <gws args>`, which sets

    GOOGLE_WORKSPACE_CLI_CONFIG_DIR=<that folder>
    GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file

WHY the file backend: gws keeps the key that encrypts credentials.enc in the OS keyring under ONE
entry (service "gws-cli", user $USER) shared by every config folder. A second login in the
keyring mode would reuse or overwrite that entry. With KEYRING_BACKEND=file the key sits in
<folder>/.encryption_key and the keyring is never touched (gws credential_store.rs, resolve_key).

THE DANGER this module guards: if the file backend ever reaches the MAIN folder, gws cannot
decrypt the main credentials.enc, decides it is stale and DELETES it. So the two variables are
only ever set together, only for a folder that is a direct, non-symlink child of the accounts
root, and never by putting them in os.environ (account calls go through a `gws-as` argv prefix;
main calls stay plain `gws`).

Stdlib only; Python 3.9 compatible (macOS system python3). Importing this never raises.
"""
import json
import os
import re
import shutil
import subprocess
import sys

CONFIG_VAR = "GOOGLE_WORKSPACE_CLI_CONFIG_DIR"
BACKEND_VAR = "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"
# Variables that make gws pick a login other than the folder's own. gws-as removes them.
IDENTITY_VARS = ("GOOGLE_WORKSPACE_CLI_TOKEN", "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE",
                 "GOOGLE_APPLICATION_CREDENTIALS")

EMAIL_RE = re.compile(
    r"[a-z0-9][a-z0-9._%+\-]{0,63}@"
    r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)*\.[a-z]{2,24}")

# What the main login holds, used when `gws auth status` cannot be read. gws adds openid,
# userinfo.email and userinfo.profile itself on every login.
FALLBACK_SCOPES = (
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/tasks",
)


class AccountError(Exception):
    """A refusal with a message fit to show the owner."""


# ---------------------------------------------------------------- paths

def home():
    return os.path.expanduser("~")


def main_dir():
    return os.path.join(home(), ".config", "gws")


def accounts_root():
    """~/.config/gws-accounts, or GWS_ACCOUNTS_ROOT (tests, the e2e run). Either way it may not
    be, contain or sit inside the main folder."""
    root = os.environ.get("GWS_ACCOUNTS_ROOT") or os.path.join(home(), ".config", "gws-accounts")
    r, m = os.path.realpath(root), os.path.realpath(main_dir())
    if r == m or r.startswith(m + os.sep) or m.startswith(r + os.sep):
        raise AccountError("accounts folder %s overlaps the main gws folder %s: refused" % (root, main_dir()))
    return root


def valid_email(email):
    """The lowercased address, or AccountError. The ONLY door between a typed string and a path."""
    if not isinstance(email, str) or len(email) > 254 or not EMAIL_RE.fullmatch(email.lower()):
        raise AccountError("%r is not a plain email address" % (email if isinstance(email, str) else "?"))
    return email.lower()


def account_dir(email, must_exist=True):
    """The folder of one added account. Refuses a hostile name, a symlink, or any folder that is
    not a direct child of the accounts root."""
    e = valid_email(email)
    root = accounts_root()
    d = os.path.join(root, e)
    if os.path.islink(d):
        raise AccountError("%s is a symlink, not an account folder: refused" % d)
    if must_exist and not os.path.isdir(d):
        raise AccountError("%s is not a connected Gmail account. Add it: comms gmail-account add %s" % (e, e))
    if os.path.lexists(d) and os.path.realpath(d) != os.path.join(os.path.realpath(root), e):
        raise AccountError("%s does not resolve inside the accounts folder: refused" % d)
    m = os.path.realpath(main_dir())
    if os.path.realpath(d) == m or os.path.realpath(d).startswith(m + os.sep):
        raise AccountError("%s resolves into the main gws folder: refused" % d)
    return d


def list_added():
    """Emails of the added accounts, sorted. [] when there are none (or the folder is missing)."""
    try:
        root = accounts_root()
        names = sorted(os.listdir(root))
    except Exception:
        return []
    out = []
    for n in names:
        p = os.path.join(root, n)
        try:
            if n == valid_email(n) and os.path.isdir(p) and not os.path.islink(p):
                out.append(n)
        except AccountError:
            continue
    return out


def is_connected(email):
    try:
        return os.path.isfile(os.path.join(account_dir(email), "credentials.enc"))
    except AccountError:
        return False


# ---------------------------------------------------------------- running gws

def gws_as_path():
    """gws-as in ~/.local/bin, then beside this kit (core/bin), then whatever PATH finds."""
    own = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "gws-as")  # kit: code path
    for p in (os.path.join(home(), ".local", "bin", "gws-as"), own):
        if os.path.exists(p):
            return p
    return "gws-as"


def _archive_dir():
    """The archive folder, as the kit config says (the same one commslib.ROOT uses)."""
    here = os.path.dirname(os.path.abspath(__file__))  # kit: code path
    if here not in sys.path:  # kit: code path
        sys.path.insert(0, here)
    import kitconfig
    return kitconfig.archive_dir()


def gws_argv(account, args):
    """argv for one gws call. account None = the main login, exactly `gws ...` as before."""
    if not account:
        return ["gws"] + list(args)
    return [gws_as_path(), account] + list(args)


def child_env(email):
    """The environment gws-as hands to gws: the caller's FULL env (a stripped env makes gws delete
    its login) plus the two variables, minus anything that would swap the identity."""
    d = account_dir(email)
    env = dict(os.environ)
    for k in IDENTITY_VARS:
        env.pop(k, None)
    env[CONFIG_VAR] = d
    env[BACKEND_VAR] = "file"
    return env


def _json_in(text):
    i = (text or "").find("{")
    if i < 0:
        return None
    try:
        return json.loads(text[i:])
    except Exception:
        try:
            return json.JSONDecoder().raw_decode(text[i:])[0]
        except Exception:
            return None


def _status(argv, timeout=40):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - a status read never raises
        return None, str(e)[:100]
    d = _json_in(p.stdout)
    if not isinstance(d, dict):
        msg = [x for x in (p.stderr or p.stdout or "").strip().splitlines() if x.strip()]
        return None, (msg[-1][:100] if msg else "exit %d" % p.returncode)
    return d, ""


def main_status():
    return _status(["gws", "auth", "status"])


def main_scopes():
    """(scopes, user): the API scopes the main login holds, from `gws auth status`."""
    d, _ = main_status()
    scopes = []
    if d:
        scopes = [s for s in (d.get("scopes") or [])
                  if isinstance(s, str) and s.startswith("https://www.googleapis.com/auth/")
                  and "userinfo" not in s]
    return (scopes or list(FALLBACK_SCOPES)), (d or {}).get("user")


def login_line(email, scopes=None):
    scopes = scopes or main_scopes()[0]
    return "! gws-as %s auth login --scopes %s" % (valid_email(email), ",".join(scopes))


def token_status(email):
    """Live check of one added account. -> dict(email, connected, token_valid, user, wrong_account, note)."""
    e = valid_email(email)
    out = {"email": e, "connected": False, "token_valid": False, "user": None,
           "wrong_account": False, "note": ""}
    try:
        account_dir(e)
    except AccountError as err:
        out["note"] = str(err)
        return out
    if not is_connected(e):
        out["note"] = "not connected yet"
        return out
    out["connected"] = True
    d, why = _status(gws_argv(e, ["auth", "status"]))
    if d is None:
        out["note"] = why or "status unreadable"
        return out
    out["token_valid"] = bool(d.get("token_valid"))
    out["user"] = d.get("user")
    if out["user"] and str(out["user"]).lower() != e:
        out["wrong_account"] = True
        out["note"] = "logged in as %s" % out["user"]
    return out


_PREFLIGHT = {}


def preflight(email):
    """None when the account can be read now; otherwise ONE short reason. Run before a scan so a
    dead token, a login that is not the folder's address (the owner clicked the wrong Google account,
    which would scan the main mailbox twice) or a half-added account is skipped with a warning.
    Remembered per process: one scan asks once, however many reads follow."""
    e = valid_email(email)
    if e in _PREFLIGHT:
        return _PREFLIGHT[e]
    s = token_status(e)
    if not s["connected"]:
        why = s["note"] or "not connected yet"
    elif s["wrong_account"]:
        why = "that login is %s, not %s" % (s["user"], e)
    elif not s["token_valid"]:
        why = s["note"] or "token expired or revoked"
    else:
        why = None
    _PREFLIGHT[e] = why
    return why


# ---------------------------------------------------------------- the three commands

def _write_self_email(email):
    """Teach commslib that this address is OURS, so a reply filed from this account does not
    treat our own address as the correspondent. Atomic; keeps every other key in self.json."""
    root = _archive_dir()
    path = os.path.join(root, "self.json")
    try:
        cur = json.load(open(path))
        if not isinstance(cur, dict):
            cur = {}
    except Exception:
        cur = {}
    emails = cur.get("emails") if isinstance(cur.get("emails"), list) else []
    if email in [str(x).lower() for x in emails]:
        return
    cur["emails"] = emails + [email]
    os.makedirs(root, exist_ok=True)
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w") as fh:
        json.dump(cur, fh, indent=1)
    os.replace(tmp, path)


def cmd_add(email, out=print):
    e = valid_email(email)
    scopes, main_user = main_scopes()
    if main_user and str(main_user).lower() == e:
        raise AccountError("%s is the MAIN account (plain `gws`). Nothing to add." % e)
    secret = os.path.join(main_dir(), "client_secret.json")
    if not os.path.isfile(secret):
        raise AccountError("the main gws folder has no client_secret.json to copy: %s" % secret)
    root = accounts_root()
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    d = os.path.join(root, e)
    if os.path.islink(d):
        raise AccountError("%s is a symlink: refused" % d)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    account_dir(e)                              # re-check the folder we just made
    target = os.path.join(d, "client_secret.json")
    if not os.path.exists(target):
        shutil.copyfile(secret, target)
    os.chmod(target, 0o600)
    _write_self_email(e)
    if is_connected(e):
        out("%s is already connected. Check it: comms gmail-account list" % e)
        out("To log in again anyway:")
    else:
        out("Added %s. It is NOT logged in yet. Run this yourself (a browser opens; pick %s there):" % (e, e))
    out(login_line(e, scopes))
    out("If the Google app is in Testing mode, %s must be on the app's test-user list first "
        "(Google Cloud console > Google Auth Platform > Audience > Test users)." % e)
    return 0


def cmd_list(out=print):
    out("%-34s %-18s %s" % ("account", "token_valid", "note"))
    d, why = main_status()
    if d is None:
        out("%-34s %-18s %s" % ("main (plain gws)", "unknown", why))
    else:
        out("%-34s %-18s %s" % ("main (plain gws)", "token_valid=%s" % str(bool(d.get("token_valid"))).lower(),
                                d.get("user") or ""))
    for e in list_added():
        s = token_status(e)
        if not s["connected"]:
            out("%-34s %-18s %s" % (e, "-", "not connected yet: comms gmail-account add %s" % e))
            continue
        note = ("WRONG ACCOUNT: %s. Remove and log in again as %s" % (s["note"], e)) if s["wrong_account"] else s["note"]
        out("%-34s %-18s %s" % (e, "token_valid=%s" % str(s["token_valid"]).lower(), note))
    return 0


def cmd_rm(email, yes, out=print):
    e = valid_email(email)
    d = account_dir(e)                          # refuses main, symlinks, strangers
    if not yes:
        raise AccountError("this deletes %s and its saved login. Run again with --yes." % d)
    shutil.rmtree(d)
    out("Removed %s. (To also revoke Google's side: myaccount.google.com > Security > third-party access.)" % e)
    return 0


# ---------------------------------------------------------------- reading across accounts

MAIN_LABEL = "main"


def warning(email, detail):
    """ONE line naming the dead account and the command that prints its fix."""
    return ("(gmail account %s not reachable: %s. Reconnect: comms gmail-account add %s)"
            % (email, " ".join(str(detail or "no answer").split())[:100], email))


# ---------------------------------------------------------------- capture

_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)


def _tokens(text):
    import shlex
    try:
        return shlex.split(text or "", posix=True)
    except Exception:
        return (text or "").split()


def _expand(path):
    h = home()
    for v in ("${HOME}", "$HOME"):
        path = path.replace(v, h)
    if path == "~" or path.startswith("~/"):
        path = h + path[1:]
    return path


def account_of_command(segment):
    """Which account one `gws gmail` command line ran as.

    None  = the main login (plain `gws`, or a CONFIG_DIR that is the main folder). Callers add NO
            field, so a main row stays exactly as it was before.
    email = `gws-as EMAIL gmail ...`, or GOOGLE_WORKSPACE_CLI_CONFIG_DIR=<.../gws-accounts/EMAIL> gws gmail ...
    "unresolved" = the command named an account through a shell variable or a folder we cannot
            read; honest, instead of calling it the main account.
    Never raises."""
    try:
        toks = _tokens(segment)
        cfg = None
        for i, t in enumerate(toks):
            m = _ASSIGN.match(t)
            if m:
                if m.group(1) == CONFIG_VAR:
                    cfg = m.group(2)
                continue
            base = os.path.basename(t)
            if base == "gws-as":
                nxt = toks[i + 1] if i + 1 < len(toks) else ""
                try:
                    return valid_email(nxt)
                except AccountError:
                    return "unresolved"
            if base == "gws":
                break
        if cfg is None or cfg == "":
            return None
        cfg = _expand(cfg)
        if "$" in cfg or "`" in cfg:
            return "unresolved"
        path = os.path.normpath(cfg)
        main = os.path.normpath(main_dir())
        if path == main or os.path.realpath(path) == os.path.realpath(main):
            return None
        name = os.path.basename(path)
        try:
            return valid_email(name)
        except AccountError:
            return "unresolved"
    except Exception:  # noqa: BLE001 - capture must never fail
        return None
