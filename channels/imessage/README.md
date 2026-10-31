# iMessage channel

A safe, on-demand command line over [openclaw/imsg](https://github.com/openclaw/imsg) (MIT
licence). It reads iMessage, SMS and RCS history and sends text or files. It is a tool you call;
nothing watches Messages or pushes an arriving message to you.

Safety properties you can rely on (each one has a test in `test_imessage.py` or
`test_imessage_kit.py`):

- A command allow-list in the `imessage` shell shim. Legacy upstream commands and the old
  `send RECIPIENT TEXT` shape are refused with exit 64 before anything runs.
- The wrapper runs only the exact upstream build it pins: version `0.14.1`, SHA-256
  `5377aa4e8ae89a8650ec664af96893aaf3d577a646bb3873871d999960b816c1`. A different hash, a symlink,
  a group-writable file or a wrong version is refused before it runs.
- Every send is read back from the chat. A message the chat never shows is reported as
  `UNCERTAIN SEND`, never as delivered.
- No GUI fallback, no watcher, and no feature that needs System Integrity Protection off.

## Install the upstream binary

You need macOS, Python 3 at `/usr/bin/python3`, and Full Disk Access for the terminal that runs
the wrapper (it reads the Messages database).

```
mkdir -p ~/.local/share/comms-kit/imessage/vendor/imsg/v0.14.1
gh release download v0.14.1 -R openclaw/imsg -p imsg-macos.zip -D /tmp/imsg-dl
unzip -q /tmp/imsg-dl/imsg-macos.zip -d ~/.local/share/comms-kit/imessage/vendor/imsg/v0.14.1
shasum -a 256 ~/.local/share/comms-kit/imessage/vendor/imsg/v0.14.1/imsg
chmod 755 ~/.local/share/comms-kit/imessage/vendor/imsg/v0.14.1/imsg
```

The `shasum` line must print the hash above. The repo does not ship the binary. The zip also holds
`imsg-bridge-helper.dylib`; this wrapper never loads it.

To keep the binary somewhere else, set `imessage.binary_path` in your kit config (see
`config.example.json`). The path comes from config only, never from an environment variable.

## Use

```
./imessage doctor                       # checks the pinned binary and read permission, sends nothing
./imessage chats --limit 10
./imessage history --chat-id 5 --limit 20
./imessage send --chat-id 5 --text-file note.txt     # headless and verified
```

A send first passes the computed draft gate (`imessage.draft_checker`, default
`../whatsapp/draft_check.py`) and the shared outbound gate. A missing checker refuses every send.

## What is not supported

See `blocked` in `channel.json`: quoted replies, tapbacks, edit, unsend, mark-read and group
administration need SIP off plus a private-API helper, and blocking has no way on macOS.

## Tests

```
python3 -m unittest discover -s . -p "test_*.py"
```

They use a fake `imsg` runtime. They never send a message and never read the real Messages
database. The full suite takes a few minutes.
