---
name: chatwoot
description: Use when reading or replying to customer conversations in Chatwoot (list, unread, a thread, search, reply, private note, resolve, mark read) through the comms-kit chatwoot plug-in. Mock only until the owner has run it against a real Chatwoot.
---

# Chatwoot

One CLI: `python3 chatwoot.py` in this folder. It uses Chatwoot's Application API over HTTPS with the Python standard library.

Status: mock only. The code has never run against a real Chatwoot. Do not tell the owner it works until a first read has succeeded.

A `reply` reaches a real customer. Show the owner the final text first, voice it with the owner's voice skill for that channel, and send only after a clear yes. A `note` is private and reaches nobody outside the team.

## Verbs

- `conversations [--status S] [--assignee A] [-n N]`: list. `S` is open, resolved, pending, snoozed or all. `A` is me, unassigned, assigned or all.
- `unread [-n N]`: open conversations with unread messages.
- `thread ID [--all]`: one conversation, oldest first. Each line shows `time=` (use it for `--since`). `--all` pages back through the whole conversation.
- `find TEXT [-n N]`: Chatwoot searches message text.
- `reply ID "text" --since TIME [--yes] [--money-ok]`: dry run unless `--yes`.
- `note ID "text" [--yes]`: private note. Dry run unless `--yes`.
- `resolve ID [--yes]`: mark resolved. Dry run unless `--yes`.
- `mark-read ID [--yes]`: dry run unless `--yes`.

## Safety

- `--yes` on a reply needs `--since` (the `time=` of the newest message you read). Any newer message, including a private note, exits 5 with `NOT SENT:`. Chatwoot's own activity rows (assignments, labels) do not block.
- Money words exit 6 until `--money-ok`, after a fresh yes.
- Exit 4 is `UNCERTAIN`: never resend. Open the conversation in Chatwoot and look first. A 5xx answer to a write counts as uncertain.
- The token comes from `chatwoot.token_command`. Nothing stores it. Plain http works only for 127.0.0.1.
