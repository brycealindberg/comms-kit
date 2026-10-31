---
name: instantly
description: Use when reading or replying to cold-email lead replies on Instantly (the Unibox): unread replies, a thread, a search, a reply, or marking a thread read.
---

# Instantly

One CLI: `python3 instantly.py` in this skill folder.

A reply reaches a real lead from a client's inbox. Show the owner the final text first and voice it with the owner's email voice skill. Send only after a clear yes.

## Verbs

- `workspaces`: list configured names.
- `unread --workspace NAME [-n N]`: newest unread lead replies.
- `thread THREAD_ID --workspace NAME`: one thread, oldest first.
- `find "text-or-address" --workspace NAME [-n N]`: search.
- `reply EMAIL_ID "text" --workspace NAME --since TS [--yes] [--money-ok]`: dry run unless `--yes`. `EMAIL_ID` is a received email's `id`. `--since` is the time of the newest message you read.
- `mark-read THREAD_ID --workspace NAME [--yes]`: dry run unless `--yes`.

## Safety

- `--yes` needs `--since`. A newer message in the thread exits 5.
- Replies go only to a received lead email. Money terms exit 6 until `--money-ok` after a fresh yes.
- Exit 4 is UNCERTAIN: never resend, check the thread in Instantly.
- Keys come from `instantly.workspaces` in the comms-kit config (a key command, no shell). Nothing stores a key.
