# Contributing

## Add a channel

You do not need to change `core/`. A channel is one folder.

```
channels/mychannel/
  mychannel.py          the command-line tool
  channel.json          what it can do
  config.example.json   the settings it reads, placeholder values only
  test_mychannel.py     tests that run with no network and no account
```

**1. Write `channel.json`.**

```json
{
  "name": "mychannel",
  "entry": "mychannel.py",
  "kind": "python-cli",
  "verbs": {
    "unread": {"direction": "read"},
    "reply": {"direction": "send", "dry_run_unless_yes": true},
    "mark-read": {"direction": "none", "dry_run_unless_yes": true}
  },
  "ignored": ["status"],
  "blocked": {"block": "the service has no call for this"},
  "config_keys": ["mychannel.account"],
  "requires": ["python3"]
}
```

| Field | Meaning |
|---|---|
| `name` | Must match the folder name. |
| `kind` | `python-cli`, `node` or `external`. For `python-cli`, the checks run `--help` and read the real subcommands. |
| `verbs` | Each verb, with a `direction` of `send`, `read` or `none`. A `send` writes an outbound row to the archive. |
| `dry_run_unless_yes` | Required on every verb that changes something. Without `--yes`, it prints what it would do and changes nothing. |
| `ignored` | Subcommands that carry no message, such as `status`. |
| `blocked` | Verbs the channel cannot do, each with the reason. Print the reason and exit non-zero, so that a person or an agent is not left guessing. |

**2. Archive your own sends.** The capture hook does not parse a plug-in channel. After a real send, your tool calls:

```python
commslib.log_event(..., hook_covers=False)
```

Find core in this order: `COMMS_KIT_CORE`, `<your file>/../../core`, then `~/.claude/communications`. `core/channel_support.py` holds the money lock and the gate helpers, so a new channel needs no copy of them. `channels/telegram/telegram.py` shows the short bootstrap that loads it. It uses the standard library only.

**3. Read settings with `kitconfig`.** Use a literal dotted key, so that the key check can see it: `kitconfig.get("mychannel.account")`. A safety check that compares against a value uses `kitconfig.require`, so it stops when the value is missing. Add every key to your `config.example.json`, and also to the root `config.example.json`.

**4. Follow the send rules.**

- No write without `--yes`.
- A text-bearing send takes `--since` and exits 5 when a newer message exists.
- A send with an unknown result exits 4 and prints `UNCERTAIN`. It never retries by itself.
- A test never reaches a real account. Mark a live test `e2e_*.py`, keep it in your own self-chat, and make it refuse any other chat.

**5. Check it.**

```sh
python3 tools/capabilities.py --check --only mychannel
python3 tools/check_config_keys.py
python3 channels/mychannel/test_mychannel.py
comms doctor
```

`comms doctor` finds your folder by itself under `channels/`. For a folder outside the repo, add it to `channel_dirs` in your config.

## Before a pull request

- Run `python3 tools/clean_clone_test.py --worktree`. Every suite must pass with an empty HOME.
- Do not add a real name, number, email, host or token. Use `example.com`, `+1 206 555 0100` to `0199`, and names like Alex Doe.
- Do not copy code from an upstream project such as a bridge. Depend on it, and name it in `requires`. `tools/check_licenses.py` refuses vendored binaries and packages with a license outside its allow list.
- Write the test first: list the ways the code can fail, watch the test fail, and then write the code.
- State the terms-of-service risk for a new channel in the README table.

## The maintainer's live copy

Some files in this repo mirror files a maintainer runs at other paths. `tools/sync_live.py --check` reports any difference, and it reads a private map. Contributors do not need it.
