---
name: telegram
description: Use when reading or sending Telegram messages from a user account (chats, unread, history, search, send, reply, mark read) through the comms-kit telegram plug-in. Mock only until the owner has logged in once.
---

# Telegram

One CLI: `python3 telegram.py` in this folder. It uses a Telegram user account through the Telethon library.

Status: mock only. The code has never run against a real account. Do not tell the owner it works until the owner has done the login steps and a first read has succeeded.

A send reaches a real person. Show the owner the final text first, voice it with the owner's chat voice skill, and send only after a clear yes.

## Verbs

- `chats [-n N]`: chats with unread counts.
- `unread [-n N]`: unread incoming messages, per chat.
- `history CHAT [-n N]`: one chat, oldest first.
- `find TEXT [--chat CHAT] [-n N]`: search one chat, or all chats.
- `send CHAT "text" --since TS [--yes] [--money-ok]`: dry run unless `--yes`.
- `reply CHAT MESSAGE_ID "text" --since TS [--yes] [--money-ok]`: dry run unless `--yes`.
- `mark-read CHAT [--yes]`: dry run unless `--yes`.
- `login`: prints the setup steps only. It logs in to nothing.
- `status`: shows what is set up. No network.

`CHAT` is `+12065550100` (a phone number), `id:12345` or `-100123` (copy it from `chats`), `@username`, or `me`. Bare digits are refused (exit 2). An unknown chat stops with `NOT SENT:` (exit 1), never `UNCERTAIN`.

## Safety

- `--yes` needs `--since` (the time of the newest message you read). A newer message exits 5 with `NOT SENT:`.
- Money words exit 6 until `--money-ok`, after a fresh yes.
- Exit 4 is `UNCERTAIN`: never resend. Open the chat and look first.
- Exit 2 with an install line means Telethon is missing: `pip install telethon`.
- Secrets come from key commands in the config. Nothing stores them.
- Telegram can limit or ban an account used for automation. Keep volume low.
