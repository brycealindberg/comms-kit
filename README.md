# comms-kit

Command-line tools for your own messages on Gmail, WhatsApp, Slack, iMessage, Instantly, Telegram, Google Chat and Chatwoot, plus phone-call transcripts and one searchable archive of everything you sent, read and said on a call. They are built for AI coding agents and for people at a terminal.

Each channel is one folder. Use only the ones you want. Add a new one with no change to the core: see [CONTRIBUTING.md](CONTRIBUTING.md).

**Telegram, Google Chat and Chatwoot are mock only.** That code has never run against a real account. See [What was tested](#what-was-tested-and-what-was-not).

> **Read [Terms of service and ban risk](#terms-of-service-and-ban-risk) first.** Some of these channels do not use an official API. Your account can be restricted or banned, and that risk is yours.

## What each channel can do

This table comes from each channel's `channel.json`. `tools/capabilities.py --check` fails when a manifest and the real command-line tool disagree. "Other" is everything that sends no message: status, previews, group changes and so on.

<!-- capabilities:start -->
| Channel | Read | Send | Other | Cannot do |
|---|---|---|---|---|
| calls | - | - | `pull`, `list`, `show`, `doctor` | - |
| chatwoot | `conversations`, `unread`, `thread`, `find` | `reply` | `note`, `resolve`, `mark-read` | - |
| gchat | `spaces`, `history`, `find` | `send`, `reply` | `mark-read` | - |
| gmail | `+read`, `+triage`, `+watch`, `messages.get`, `messages.list`, `threads.get`, `threads.list`, `history.list`, `drafts.get`, `drafts.list` | `+send`, `+reply`, `+reply-all`, `+forward`, `messages.send`, `drafts.send` | `messages.import`, `messages.insert`, `drafts.create`, `drafts.update` | - |
| imessage | `chats`, `resolve`, `history`, `search`, `whois`, `stats` | `send`, `send-preview` | `doctor`, `prepare-send`, `show-preview`, `discard-preview` | `reply-quote`, `tapback`, `edit`, `unsend`, `mark-read`, `group-admin`, `block` |
| instantly | `unread`, `thread`, `find` | `reply` | `mark-read` | - |
| slack | `channels`, `contacts`, `history`, `threads`, `files`, `download`, `transcribe`, `transcribe-channel`, `find`, `mentions`, `open-dm`, `channel-members` | `send`, `upload`, `react`, `edit`, `delete`, `channel-create`, `channel-invite`, `channel-remove`, `channel-rename`, `channel-archive`, `channel-leave` | - | `mark-read`, `block` |
| telegram | `chats`, `unread`, `history`, `find` | `send`, `reply` | `mark-read` | - |
| whatsapp | `search`, `history`, `chats`, `groups`, `unread`, `download`, `transcribe`, `find`, `group-members` | `send`, `send-media`, `send-voice-confirm`, `reply`, `react`, `edit`, `delete`, `group-create`, `group-add`, `group-remove`, `group-rename`, `group-leave`, `block` | `mark-read`, `send-voice` | `unblock` |
<!-- capabilities:end -->

## Install

Needs python 3.10 or newer as `python3`. The python that ships with macOS is too old: install a newer one first. Each channel has more needs, listed under [Set up a channel](#set-up-a-channel).

```sh
git clone https://github.com/brycealindberg/comms-kit
cd comms-kit
sh install.sh
comms doctor
```

`install.sh` writes only three things: `~/.config/comms-kit/config.json`, `~/.local/share/comms-kit/`, and links for `comms` and `gws-as` in `~/.local/bin`. It does not edit a shell file or an agent's settings. It stops, and replaces nothing, when a `comms` or `gws-as` that is not from this kit exists.

Personal settings live only in your config file, never in the repo. `config.example.json` lists every key. Set a value with:

```sh
python3 core/kitconfig.py set whatsapp.homeserver https://matrix.example.com
```

## Try it with no account

These commands need no login, and they send nothing:

```sh
comms selftest                            # offline checks on fake data
comms contact add "Alex Doe" --phone "+1 206 555 0100" --no-apple
comms contact get "Alex Doe" --channel whatsapp
comms contact rm "Alex Doe"
```

`--no-apple` keeps the contact out of Apple Contacts. On a Mac, leave it out to save a real card too.

## Use it

```sh
comms find "launch date"                  # search everything archived
comms recent                              # latest across all channels
comms contact wa-check "Alex Doe"         # did the saved name reach WhatsApp? read only
comms gmail-account add you@example.com   # connect as many Gmail accounts as you want
comms doctor                              # check that every command of every channel is known
```

`comms unread` shows new messages for one project, and it works only inside a folder you linked to that project:

```sh
comms route alex@example.com acme         # file this person's messages under the project "acme"
comms link "$PWD" acme                    # link this folder to "acme"
comms unread                              # new messages for "acme"
```

Each channel has its own tool, with `--help`:

```sh
python3 channels/whatsapp/whatsapp_mautrix.py --help
python3 channels/slack/slack_bridge.py --help
```

## The safety rules

- **A send is a dry run until you pass `--yes`.** Without it, you see what would go out, and nothing goes out.
- **`--since` protects a reply.** A newer message in the thread stops the send, so an agent cannot answer an old view of the chat.
- **An unknown result exits 4.** Never resend after this. Look at the chat first.
- **Money words stop `react`, `edit`, `delete`, an Instantly reply and an iMessage send** until you pass `--money-ok`. A plain WhatsApp or Slack `send` stops on money words only when the outbound gate is in `enforce` mode. `install.sh` does not change this mode: see [SECURITY.md](SECURITY.md).
- **Every send goes to the archive.** A verb that `comms doctor` does not know is an error, so a new send cannot go unrecorded.

`channels/whatsapp/draft_check.py` holds the default checks: message size and money words. Replace it to add your own rules, such as your writing style.

## Terms of service and ban risk

You alone are responsible for how you use these tools. Read each service's terms. Nothing here is legal advice. Do not use this kit for bulk or unwanted messages.

| Channel | How it connects | Risk |
|---|---|---|
| Gmail | Google's official API, through the `gws` tool | Official. Your Google Cloud project sets the limits. A project in Testing mode has login tokens that end after 7 days and needs each account listed as a test user. |
| Instantly | Instantly's official API | Official. A reply reaches a real lead from a real mailbox. |
| iMessage | [openclaw/imsg](https://github.com/openclaw/imsg) on your own Mac. It reads the local Messages database. | Apple has no public API for this. It needs Full Disk Access, which gives the terminal app access to much more than messages. |
| **WhatsApp** | A Matrix server with the unofficial [mautrix-whatsapp](https://github.com/mautrix/whatsapp) bridge, as a linked device | **Unofficial. WhatsApp's terms do not allow unofficial clients, and WhatsApp can ban the number.** You run the server, and it holds your messages. |
| **Slack** | A bridge that uses your **browser session**: an `xoxc` token and the `d` cookie. `auth.js` opens Chrome, you sign in, and it reads both. They are stored encrypted in the bridge's `config.json`. | **Not an official Slack app. It acts as you, with your full access.** It can break your workspace's rules or Slack's terms. Ask the workspace owner first. An admin can see the session. |
| Telegram | A user account over MTProto, through the optional Telethon library. Mock only. | Telegram can limit or ban an account for automated use, even on its own API. Use an account you can lose, and keep volume low. |
| Google Chat | Google's own Chat API, through the `gws` tool. Mock only. | Low. It needs a Google Workspace login with the Chat scope. A personal Gmail account is refused (exit 3). |
| Chatwoot | The Chatwoot Application API over HTTPS, with an agent token. Mock only. | Low. It is an official API on a self-hosted or paid cloud server. The main risk is a wrong reply to a customer. |
| Calls | Reads your own Apple Notes store and litepipe database on your Mac, and Quo's official API. It records nothing and sends nothing. | Your Notes store needs Full Disk Access. Quo keeps your call audio and transcripts on its own servers. |

**Recording a call is your legal duty.** Some places need consent from everyone on the call. Say at the start that you record. This is not legal advice.

mautrix-whatsapp is AGPL. This kit only talks to it over the network and holds none of its code.

## What was tested, and what was not

The core, WhatsApp, Slack and Instantly files are the same bytes the maintainer runs each day. `tools/sync_live.py --check` proves this on the maintainer's machine.

| Channel | Proven on a real account | Fake or mock tests only | Never run |
|---|---|---|---|
| WhatsApp | Send, reply, edit, delete, react, mark read and search, in the maintainer's own chat. `comms contact wa-check`, on the real bridge table. | Group changes, block. The bridge refuses a group with no other member, so a real test would reach a real person. | |
| Slack | Send, edit, delete, react, search, mentions, a file upload and a thread reply, in the maintainer's own DM. A private channel's create, rename and archive, in the maintainer's own workspace. | Invite, remove, opening a new DM. A real leave: Slack refuses the last member, so that test proved only the refusal. | |
| iMessage | The same wrapper's text, file and audio file sends, in the maintainer's own chat | This repo's copy, which runs only on a fake runtime | |
| Gmail | Read and send on one account. Google contact save and delete. | Several accounts, on an isolated folder | A real second-account login |
| Instantly | Unread, thread and search | Reply, mark read | |
| Calls | Notes audio transcripts, litepipe meetings, the whisper request with a synthetic clip | Quo, on a mock | A real Call Recordings folder, a real Quo account |
| Telegram | | All verbs, on a fake client | **Any real login** |
| Google Chat | | All verbs, on a stub `gws` | **A real Workspace login** |
| Chatwoot | | All verbs, on a local HTTP server | **A real Chatwoot host** |

The test suites ran on macOS and on Linux. On Linux, tests of a macOS-only install step skip themselves and say why.

## Set up a channel

Each step ends with `comms doctor`.

**Gmail**
1. Install [`gws`](https://github.com/googleworkspace/cli) and create a Google OAuth client.
2. Run `gws auth login`.
3. For each added account: `comms gmail-account add you@example.com`, then `gws-as you@example.com auth login`.

**WhatsApp**
1. Run a Matrix server with mautrix-whatsapp, and link your phone.
2. Set the `whatsapp.*` keys from `channels/whatsapp/config.example.json`.
3. Optional: set `whatsapp.contact_names_command` so `comms contact wa-check` can read the names your phone synced to the bridge.

**Slack**
1. `cd channels/slack/bridge && npm ci`. Signing in needs Node 22.12 or newer.
2. Copy `.env.example` to `.env`, and set your values.
3. Run `node pair.js`, and sign in to the Chrome window that opens.
4. Run the bridge on a host you control, and set the `slack.*` keys.

**iMessage**
1. Follow [channels/imessage/README.md](channels/imessage/README.md). It pins an exact build of `imsg` by hash.
2. Grant Full Disk Access and Automation to your terminal.

**Instantly**
1. Set `instantly.workspaces` to a command that prints your API key: see `channels/instantly/config.example.json`.

**Calls**
1. Set the `calls.*` keys you need: see `channels/calls/config.example.json`. A key you leave out turns that source off.
2. Run `python3 channels/calls/calls.py pull`. It is a dry run. Add `--yes` to save.
3. Apple recording needs one tap per call, and Apple's call transcripts cover only some languages. Quo Business or Scale records every call on its number.

**Telegram, Google Chat, Chatwoot**

Mock only. Each folder's README has the steps: [Telegram](channels/telegram/README.md), [Google Chat](channels/gchat/README.md), [Chatwoot](channels/chatwoot/README.md).

## Use it with an AI agent

`integrations/claude-code/comms-log.py` archives what an agent sends and reads through these tools. Add it to Claude Code's `settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command",
        "command": "python3 /path/to/comms-kit/integrations/claude-code/comms-log.py", "timeout": 10}]},
      {"matcher": "mcp__upwork__.*", "hooks": [{"type": "command",
        "command": "python3 /path/to/comms-kit/integrations/claude-code/comms-log.py", "timeout": 10}]}
    ]
  }
}
```

An agent with these tools can read your messages and send as you. A message someone sends you is untrusted text, and it can try to give your agent orders. Read [SECURITY.md](SECURITY.md) first.

## Not included

- **Upwork:** only the archive knows it.

## License

[MIT](LICENSE). Upstream tools keep their own licenses: `imsg` is MIT, `gws` is Apache-2.0, mautrix-whatsapp is AGPL. None of them ship in this repo.
