---
name: gchat
description: Use when reading or sending Google Chat messages through the gws CLI (list spaces, read history, search, send, reply into a thread, mark a space read) with the comms-kit gchat plug-in. Mock only until the owner has a Workspace login with the Chat scope.
---

# Google Chat

One CLI: `python3 gchat.py` in this folder. It runs `gws chat ...` for you.

Status: mock only. The code has never run against a real account. Do not tell the owner it works until a first read has succeeded on a Workspace login.

A send reaches a real person. Show the owner the final text first, voice it with the owner's chat voice skill, and send only after a clear yes.

## Never do this

Never run the real `gws` with a stripped environment or under a fake HOME. It deletes the owner's real Google login. The tool refuses (exit 2) when HOME is not the real home. Tests use a stub through `GCHAT_GWS_BIN`. Do not set that variable to the real gws.

## Verbs

- `spaces [-n N]`: list spaces. Copy the `spaces/...` name.
- `history SPACE [-n N] [--days D]`: one space, oldest first. Prints each message name and exact `createTime`.
- `find TEXT [--space SPACE] [-n N] [--days D] [--max-spaces M]`: client-side text search.
- `send SPACE "text" --since CREATETIME [--yes] [--money-ok]`: dry run unless `--yes`.
- `reply MESSAGE_NAME "text" --since CREATETIME [--yes] [--money-ok]`: into that message's thread. Fails if the thread is gone.
- `mark-read SPACE [--yes]`: dry run unless `--yes`.

## Safety

- `--yes` needs `--since`: the `createTime` of the newest message you read, exactly as printed. Any newer message in the space, including a thread reply, exits 5 with `NOT SENT:`.
- Money words exit 6 until `--money-ok`, after a fresh yes.
- Exit 4 is `UNCERTAIN`: never resend. Open the space and look first.
- Exit 3: the login cannot use the Chat API. A personal Gmail account cannot. A Workspace login with the Chat scope is needed.
- Optional key `gchat.gws_account`: an email added with `comms gmail-account add`. The tool then runs gws through `gws-as`.
