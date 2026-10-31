**Mock only. This code has never run against a real account.**

# Google Chat channel

Read and send on Google Chat through the `gws` command-line tool. `gws` is Google's Workspace CLI. This channel does not call Google directly.

## What the tests prove, and what they do not

The tests use a stub script in place of `gws`. The real `gws` is never run by the tests. They prove the kit's rules: dry run by default, `--since`, exit codes, the money lock, one archive row per send, and the HOME guard.

They do not prove that `gws` accepts the flags used here, or that Google answers in the shape assumed.

Checked in `gws` 0.22.5, with `--help` only:

- `gws chat spaces list`, `spaces messages list`, `spaces messages create`, `spaces messages get` exist and take `--params <JSON>` and `--json <JSON>`.
- `gws chat users spaces updateSpaceReadState` exists. This is why `mark-read` is built and not blocked.
- `gws chat +send` exists, but this channel uses `spaces messages create`, because it needs the reply fields and the returned message name.

Checked in Google's Chat API discovery document (`https://chat.googleapis.com/$discovery/rest?version=v1`), read once, not run:

- `spaces.list`: `pageSize`, `pageToken`, answer `spaces[]` and `nextPageToken`.
- `spaces.messages.list`: path `spaces/{space}`, `filter` on `create_time` and `thread.name`, `pageSize`, `pageToken`, `orderBy`. The document says the values are `ASC` and `DESC` and that the default is `create_time ASC`. The tool asks for `create_time DESC` (newest first), reads up to 20 pages, then sorts for display. That way a busy space still shows the real newest messages and a cut answer drops only the oldest. The exact spelling of the `orderBy` value is NOT verified. If Google rejects it, the tool fails with Google's message. If Google ignores it, the tool notices a cut answer that is not newest first and stops (exit 1).
- `spaces.messages.create`: body `text` and `thread.name`; query `messageReplyOption`. The tool uses `REPLY_MESSAGE_OR_FAIL`, so a reply never silently starts a new thread.
- `users.spaces.updateSpaceReadState`: name `users/me/spaces/{space}/spaceReadState`, `updateMask=last_read_time`, scope `chat.users.readstate`.

Not checked: the `orderBy` spelling (above); how `gws` names its path parameters for these calls (the tool passes `parent` and `name` in `--params`, as the discovery document names them); the exact text of `gws` error output; whether `messages.list` returns thread replies in the flat list (the tool assumes it does, as `threadReply` suggests).

## What the owner must do to go live

About 30 to 60 minutes the first time, mostly in Google's admin screens.

1. Use a Google Workspace account. A personal Gmail account cannot use the Chat API.
2. A Workspace admin may need to allow the Chat API for the project, and allow your account to use it.
3. Log in to `gws` with the Chat scopes: `gws auth login`, and include the Chat scopes (messages, spaces, and `chat.users.readstate` for `mark-read`). Read `gws auth login --help` first.
4. Run `python3 gchat.py spaces`. Copy a `spaces/...` name.
5. Run `python3 gchat.py history SPACE`. Try one `send` to a test space. Use the dry run, then `--yes`.

To use a second Google login that was added with `comms gmail-account add`, set `gchat.gws_account` to that email. The tool then runs `gws` through `gws-as`. Leave it empty for the main login.

## The HOME guard

Running the real `gws` with a stripped environment or under a fake HOME deletes the owner's real Google login. So:

- The tool passes the full environment to `gws`. It never builds a smaller one.
- Without `GCHAT_GWS_BIN`, if `HOME` is not the user's real home, the tool prints `REFUSED`, exits 2, and starts no subprocess.
- `GCHAT_GWS_BIN` (and `GCHAT_GWS_AS_BIN` for the account wrapper) exist for tests. Point them only at a stub.

## Risk (terms of service)

Low. This uses Google's own API through Google's own CLI. The main limit is that the Chat API needs a Workspace account. A personal Gmail account gets a permission error, and the tool says so in one line (exit 3). Normal Chat quotas apply. Do not send in bulk.

## Commands

```
python3 gchat.py spaces [-n N]
python3 gchat.py history SPACE [-n N] [--days D]
python3 gchat.py find TEXT [--space SPACE] [-n N] [--days D] [--max-spaces M]
python3 gchat.py send SPACE "text" --since CREATETIME [--yes] [--money-ok]
python3 gchat.py reply MESSAGE_NAME "text" --since CREATETIME [--yes] [--money-ok]
python3 gchat.py mark-read SPACE [--yes]
```

`find` reads the messages of the last `--days` days and searches them here. Google is not asked to search. Without `--space` it pages through the space list and searches the first `--max-spaces`. When any part was skipped or cut, the last line says `PARTIAL` and how many spaces were not searched, so "0 found" is never read as a full "no". The exit code stays 0.

## Rules

- A write is a dry run unless `--yes`.
- `--yes` without `--since` exits 2.
- Any message newer than `--since` in the space exits 5 and prints `NOT SENT:`. A thread reply counts. Pass the `createTime` exactly as `history` printed it.
- An unknown result exits 4 and prints `UNCERTAIN`. The tool never retries. Only a numeric 400, 404, 422 or 429 counts as "not sent" (401 and 403 print the Workspace line). Every other code is unknown: 408, 409, any 5xx, and a code that is not a number such as `ABORTED`.
- Money words exit 6 until `--money-ok`.
- A real send writes one archive row with `hook_covers=False`.
- A permission error (HTTP 401 or 403) prints the Workspace line and exits 3.

## Exit codes

0 ok. 1 refused or failed. 2 usage, setup, or fake HOME. 3 the login cannot use the Chat API. 4 UNCERTAIN send. 5 newer message. 6 money lock. 7 outbound gate refused.

## Settings

See `config.example.json`. Optional key: `gchat.gws_account`.
