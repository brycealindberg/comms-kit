#!/usr/bin/env python3
"""Live read-only proof against the `personal` workspace. Never passes --yes, never POSTs.

Steps: unread (n<=5), find (n<=5), reply dry run on a real received row, no-POST assertion.
Prints step names and counts only: no names, addresses or message text.
Ends with exactly `RESULT: PASS` when every step passed. Needs the `personal` key in config.
"""
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import instantly as I  # noqa: E402

WS = "personal"
calls = []
_real = I.http_request


def spy(method, path, key, params=None, body=None, timeout=30):
    calls.append(method)
    return _real(method, path, key, params=params, body=body, timeout=timeout)


I.http_request = spy
results = []


def step(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" ({detail})" if detail else ""))


def cli(*argv):
    assert "--yes" not in argv
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        try:
            I.main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue()


def fail_closed():
    print("FAIL a read stopped early")
    print("RESULT: FAIL")
    sys.exit(1)


try:
    key = I.resolve_key(WS)
except SystemExit:
    print("FAIL key lookup")
    print("RESULT: FAIL")
    sys.exit(1)

# Step 1: unread
code, out = cli("unread", "--workspace", WS, "-n", "5")
n_unread = out.count("[RECEIVED]")
step("unread", code == 0 and n_unread <= 5 and "[SENT]" not in out, f"{n_unread} rows")

# Step 2: find. Uses a lead address from the newest received row, read straight from the API.
try:
    rows = I.list_emails(key, {"email_type": "received", "sort_order": "desc"}, 5)
except SystemExit:
    fail_closed()
rows = [r for r in rows if I.is_received(r)]
if rows:
    code, out = cli("find", rows[0]["from_address_email"], "--workspace", WS, "-n", "5")
    n_found = out.count("[RECEIVED]") + out.count("[SENT]")
    step("find", code == 0 and 1 <= n_found <= 5, f"{n_found} rows")
else:
    code, out = cli("find", "zzz-no-match-zzz", "--workspace", WS, "-n", "5")
    step("find (no received rows, empty search)", code == 0, "0 rows")

# Step 3: thread read on a real row
if rows:
    code, out = cli("thread", str(rows[0]["thread_id"]), "--workspace", WS)
    step("thread", code == 0 and "[RECEIVED]" in out, f"{out.count('[')} msgs")

# Step 4: reply dry run on a real received row. --since = that thread's newest time.
if rows:
    try:
        thread = I.fetch_thread(key, str(rows[0]["thread_id"]))
    except SystemExit:
        fail_closed()
    newest = max((I.email_ts(e) or 0) for e in thread)
    target = max((e for e in thread if I.is_received(e)), key=lambda e: I.email_ts(e) or 0)
    code, out = cli("reply", target["id"], "Thanks, a short test line.", "--workspace", WS,
                    "--since", str(newest))
    step("reply dry run", code == 0 and "DRY RUN" in out and "NOT SENT" not in out)
else:
    step("reply dry run (no received row to try)", True, "skipped, no row")

# Step 5: no POST ran
step("no POST ran", "POST" not in calls, f"{len(calls)} GET calls")

print("RESULT: PASS" if all(results) else "RESULT: FAIL")
sys.exit(0 if all(results) else 1)
