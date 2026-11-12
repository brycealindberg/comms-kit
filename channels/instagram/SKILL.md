---
name: instagram
description: Use when reading or sending Instagram DMs (chats, unread, history, search, download, send, reply, photo, react, edit, unsend, mark read) through the comms-kit instagram plug-in and the mautrix-instagram Matrix bridge.
---

# Instagram

One CLI: `python3 instagram.py` in this folder. It talks to a Matrix server that runs the
mautrix-instagram bridge. It does not use Meta's API.

A send reaches a real person. Show the owner the final text first, voice it with the owner's chat voice
skill, and send only after a clear yes for that exact person.

## Verbs

- `chats [-n N] [--json]`, `unread [-n N]`, `history CHAT [-n N] [--json]`, `find TEXT [--chat CHAT]`: reads. No read receipt.
- `download CHAT EVENT_ID [--out DIR]`: save a photo, video, voice note or file.
- `send CHAT "text" --since TS [--yes]`, `reply CHAT EVENT_ID "text" --since TS [--yes]`.
- `send-media CHAT FILE [--caption T] --since TS [--yes]`.
- `react CHAT EVENT_ID EMOJI [--yes]`, `edit CHAT EVENT_ID "text" [--yes]`, `delete CHAT EVENT_ID [--yes]`.
- `mark-read CHAT [--yes]`: Instagram then shows "Seen".
- `status`: settings, send hold, writes today, bridge login state.
- `login`: interactive. Only the owner types it, in their own terminal. Never type a password for them.
- `clear-hold [--yes]`: only after the owner checked the Instagram app.

`CHAT` is a room id from `chats` or a part of the chat name. Two matches exit 2.

## Safety

- Every write is a dry run until `--yes`. Text sends need `--since` (`history` prints it). Exit 5 = a newer
  message: read it again.
- Exit 3 = the bridge login is not healthy, a send hold is on, or the cap or gap stops it. Nothing was sent.
  Do not work around it. Tell the owner what it says.
- Exit 4 = UNCERTAIN. Never resend. Read the chat first.
- Exit 6 = money lock: show the exact text, get a fresh yes, then add `--money-ok`.
- Instagram can restrict or close the account. Keep volume low. No first messages to strangers, no bulk.
