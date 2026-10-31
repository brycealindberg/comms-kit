---
name: calls
description: Use to pull phone-call transcripts (Apple Notes call recordings, Quo, litepipe) into the comms archive, or to find, list or read a saved call transcript.
---

# Calls

One CLI: `python3 calls.py` in this skill folder. Read-only: it sends nothing to anyone and changes nothing on any service.

Each call becomes one text file under `<archive>/calls/` named `YYYY-MM-DD_<source>_<id>.txt`, plus one archive row, so `comms find "words from the call"` reaches it.

## Verbs

- `pull [--source notes|quo|litepipe|all] [--since DAYS] [--yes]`: dry run unless `--yes`. `--since 0` means everything (default 30). A second pull adds nothing. A call that is not ready (still transcribing, still running) is tried again next pull.
  - `--notes-folder NAME`: Notes folder to read (default config `calls.notes_folder`, then `Call Recordings`). A missing folder is a plain message and exit 0.
  - `--apps "A,B"`: litepipe apps (default config `calls.litepipe_apps`, then `FaceTime,Phone`).
  - `--whisper`: send recordings that have no transcript to the whisper service in `calls.whisper_url`. Off by default. Never transcribes on this machine.
- `list [--limit N]`: saved calls, newest first. Names, sizes and status only.
- `show ID`: print one transcript. `ID` is the id, `source_id`, or the file name from `list`.
- `doctor`: one line per source.

## Sources

- Notes: reads a read-only copy of the Notes store. The transcript is the searchable text on the recording's audio attachment. Locked notes are skipped and counted. A recording with no transcript (a language Apple does not transcribe) is saved as "no transcript".
- Quo: `GET /calls` and `GET /calls/{id}/transcripts`, API version 2026-03-30. No webhooks. The key comes from the command in `calls.quo_key_command`; no key means Quo is skipped.
- litepipe: reads the local meeting database read-only. A meeting that has not ended is not ready.

## Rules

- Never paste a transcript into a report. Counts and lengths only.
- Run it from launchd with `/usr/local/bin/python3` (Full Disk Access for Notes). It exits 0 with no Quo key and no Notes folder.
- Config keys: `calls.notes_db`, `calls.notes_folder`, `calls.quo_key_command`, `calls.litepipe_db`, `calls.litepipe_apps`, `calls.whisper_url`.
