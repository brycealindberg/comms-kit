**Mock only. This code has never run against a real account.**

# Telegram channel

Read and send on Telegram from a user account. It uses the Telethon library (MTProto). It is not a bot.

## What the tests prove, and what they do not

The tests use a fake client. They prove the kit's rules: dry run by default, `--since`, exit codes, the money lock, one archive row per send.

They do not prove that Telethon behaves as the wrapper assumes. The `TelethonClient` class in `telegram.py` is written from Telethon's documentation (context7, Telethon v1 docs) and has never been run. The calls it uses are `get_dialogs`, `get_messages` (with `search`), `send_message` (with `reply_to`), `send_read_acknowledge`, `connect` and `is_user_authorized`.

Nothing has talked to Telegram. Treat every verb as untested.

## What the owner must do to go live

About 20 to 30 minutes. Only the owner can do these steps.

1. Install Telethon: `pip install telethon`. The kit never ships it.
2. Open https://my.telegram.org, log in with the account's phone number, and choose "API development tools". Create an app. Copy `api_id` and `api_hash`.
3. Put both values in a secret store. Set `telegram.api_id_command` and `telegram.api_hash_command` to commands that print them. Set `telegram.session_path`.
4. Log in once by hand. Run `python3 telegram.py login` and follow the printed steps. Telegram sends a code to the Telegram app. It may also ask for the two-step password.
5. Run `python3 telegram.py status`, then `python3 telegram.py chats`.
6. Try one `send` to your own "Saved Messages" chat first. Use the dry run, then `--yes`.

The session file is a login. Keep it private (mode 600). Do not commit it.

## Risk (terms of service)

Telegram can limit or ban an account for automated use. This is true even through its own API. Use an account you can afford to lose. Keep volume low. Do not send first messages to strangers. Do not send in bulk. A `FloodWait` answer means stop and wait.

## Commands

```
python3 telegram.py chats [-n N]
python3 telegram.py unread [-n N]
python3 telegram.py history CHAT [-n N]
python3 telegram.py find TEXT [--chat CHAT] [-n N]
python3 telegram.py send CHAT "text" --since TIME [--yes] [--money-ok]
python3 telegram.py reply CHAT MESSAGE_ID "text" --since TIME [--yes] [--money-ok]
python3 telegram.py mark-read CHAT [--yes]
python3 telegram.py login      # prints the steps only
python3 telegram.py status     # no network
```

## Chat names

The tool never guesses who a chat is. `CHAT` must be one of:

- `+12065550100`: a phone number (with the `+`).
- `id:12345` or `-100123`: a chat id, exactly as `chats` prints it. Copy it from there.
- `@username`, or `me` (your own Saved Messages).

Bare digits such as `12065550100` are refused (exit 2). They could be a phone number or a chat id, and a wrong guess would send to the wrong person.

Before a send or a mark-read, the tool looks the chat up. If Telegram does not know the chat, the run stops with `NOT SENT:` (exit 1). That is not an `UNCERTAIN` result, because nothing left.

## Rules

- A write is a dry run unless `--yes`.
- `--yes` without `--since` exits 2.
- A newer message than `--since` exits 5 and prints `NOT SENT:`. Any newer message counts, including a reply to an old message.
- An unknown send result (timeout, drop) exits 4 and prints `UNCERTAIN`. The tool never retries.
- Money words exit 6 until `--money-ok`.
- A real send writes one archive row with `hook_covers=False`.
- When Telethon is missing, the tool prints the install line and exits 2.

## Exit codes

0 ok. 1 refused or failed. 2 usage or setup problem. 4 UNCERTAIN send. 5 newer message. 6 money lock. 7 outbound gate refused.

## Settings

See `config.example.json`. Keys: `telegram.api_id_command`, `telegram.api_hash_command`, `telegram.session_path`.
