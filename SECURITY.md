# Security

comms-kit handles your private messages and the keys to your accounts. It runs only on your own machine and has no server of its own. The kit sends no telemetry. Two parts send data only when you turn them on: the Slack bridge sends errors to Sentry when you set `SENTRY_DSN_VPS`, and a WhatsApp voice note sends its text to ElevenLabs when you use `send-voice`.

## Report a problem

Open a private security advisory on this repo's GitHub page. Do not put a real token, phone number or message in a public issue.

## What the kit stores

| What | Where | Note |
|---|---|---|
| Your settings | `~/.config/comms-kit/config.json` | Mode 600. Holds handles and hosts. Do not put a secret here. |
| The message archive | `data_dir` in your config | Plain text JSON lines. Not encrypted. Anyone who can read your home folder can read it. Use disk encryption. |
| Gmail logins | The `gws` tool's own folder, one per added account | Encrypted by `gws`. |
| API keys | Your own secret store | `instantly.workspaces` holds a command that prints the key. The kit never stores the key. |
| Slack session tokens | The bridge's own config on the bridge host | They act as the signed-in user. Treat them as a password. |
| Call transcripts | `calls/` in your `data_dir` | Plain text. Not encrypted. They hold what other people said. Protect them as you protect your messages. |

## The guards on a send

- A send or a write is a dry run until you pass `--yes`.
- A text-bearing send takes `--since`. A newer message in the thread stops the send, so an agent cannot reply to an old view of the chat.
- A send whose result is unknown exits 4. Do not resend it. Look at the chat first.
- Money words stop `react`, `edit`, `delete`, an Instantly reply and an iMessage send until you pass `--money-ok`. The check reads words only, so it can miss a case.
- The outbound gate has two modes. `report` is the default: it records what it finds in `gate.jsonl` and stops nothing. `enforce` stops a send on any finding and exits 7. A plain WhatsApp or Slack `send` with money words goes out in `report` mode. To turn it on, set `GATE_MODE=enforce`, or put the word `enforce` in a file named `gate_mode` in your data folder.
- Replace `channels/whatsapp/draft_check.py` to add your own rules.
- `OUTBOUND_GUARDS=1` with no guard folder configured refuses the send. It does not pass silently.

These guards reduce mistakes. They are not a security boundary against a hostile program on your machine.

## An AI agent with these tools

An agent that runs these commands can read your messages and, with `--yes`, send as you. A message that someone sends you is untrusted input: its text can try to give your agent orders. Keep a person in the loop for every send to another person. Do not let an agent browse the web and hold `--yes` at the same time, unless you accept that risk.

## Keep personal data out of the repo

`tools/scrub_check.py` scans tracked files and git history against a deny list that you keep outside the repo, at `~/.config/comms-kit/scrub-deny.txt`. It prints only a rule id, so its output is safe to share. It exits 2 when it has no list to read, because a scan of nothing is not a pass.
