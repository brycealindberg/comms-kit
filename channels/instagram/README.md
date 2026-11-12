# Instagram channel

Read and send Instagram DMs through a Matrix server that runs the **mautrix-instagram** bridge. The bridge
logs in to Instagram as you and puts each Instagram chat in a Matrix room (a "portal"). This tool talks only
to your Matrix server. It does not use Meta's official API.

mautrix-instagram lives in [mautrix/meta](https://github.com/mautrix/meta) (`cmd/mautrix-instagram`). Since
v26.08 the `meta` binary there is Messenger only.

## What the tests prove, and what they do not

`test_instagram.py` runs every verb against a fake Matrix server. It proves the kit's rules: dry run by
default, `--since`, the exit codes, the money lock, the gate, one archive row per send, the bridge health
check, the send hold, the daily cap and the gap.

It does not prove what the real bridge sends back. The tool reads three things from the bridge, all written
from the bridge's own code (mautrix-go bridgev2):

- the bot's `list-logins` answer: `* `<login id>` (<name>) - `<STATE>``,
- the send status event `com.beeper.message_send_status` (`SUCCESS`, `FAIL_RETRIABLE`, `FAIL_PERMANENT`),
- the portal marker: an `m.bridge` state event with `protocol.id` `instagram`.

## Risk (terms of service)

**Instagram's terms do not allow unofficial clients. Instagram can warn, restrict or close the account.**
No tool can promise "no ban". This one lowers the risk:

- Every write with `--yes` first asks the bridge bot for the login state. Anything but `CONNECTED` or
  `BACKFILLING` stops the write (exit 3). A security check or a restriction shows as `BAD_CREDENTIALS`.
- When Instagram refuses a send with words that point to a limit (`feedback_required`, `try again later`,
  `spam`, a challenge ...), the tool sets a **send hold** for 24 hours. Every write stops. Open the
  Instagram app, clear any warning, then run `clear-hold --yes`.
- A daily cap (`instagram.daily_send_cap`, default 40 writes in 24 hours) and a minimum gap between writes
  (`instagram.min_send_gap_seconds`, default 20 seconds).
- Reads send no read receipt and no typing notice. Only `mark-read --yes` shows "Seen".

Also do this on the server side:

- Log the bridge in from an IP address the account already uses, for example a SOCKS5 proxy on a home
  machine (`network.proxy` in the bridge config, with `proxy_media` and `proxy_other` on). A data center IP
  is the most common reason for a security check.
- Use the `android` login flow. Do not log in and out often.
- Do not send first messages to strangers, and do not send in bulk.

## Set up

1. Check the bridge before you run it: `python3 tools/bridge_scan.py --help` (from the repo root). It checks
   the build info against the source tag, the module checksums against Go's checksum database, and known
   vulnerabilities.
2. Run mautrix-instagram next to your Matrix server and register it as an appservice. Pin the image by
   digest.
3. Set the keys:
   ```
   python3 core/kitconfig.py set instagram.homeserver https://matrix.example.com
   python3 core/kitconfig.py set instagram.bot_user @instagrambot:matrix.example.com
   ```
4. Put your Matrix access token in a file (mode 600), then:
   `python3 instagram.py setup --token-file /path/to/token`
   It saves `~/.config/instagram-mautrix/session.json` (mode 600) and opens a room with the bridge bot.
5. Log in. Only you type these answers:
   `python3 instagram.py login`
   The password prompt is hidden. The bridge deletes the message that holds it. If Instagram asks for a
   code, the tool asks you for it. If Instagram wants a check in a browser, approve the login in the
   Instagram app on your phone, then run `login` again.
6. `python3 instagram.py status`, then `chats`.

## Commands

```
python3 instagram.py chats [-n N] [--json]
python3 instagram.py unread [-n N]
python3 instagram.py history CHAT [-n N] [--json]
python3 instagram.py find TEXT [--chat CHAT] [-n N]
python3 instagram.py download CHAT EVENT_ID [--out DIR]
python3 instagram.py send CHAT "text" --since TIME [--yes] [--money-ok]
python3 instagram.py reply CHAT EVENT_ID "text" --since TIME [--yes] [--money-ok]
python3 instagram.py send-media CHAT FILE [--caption TEXT] --since TIME [--yes] [--money-ok]
python3 instagram.py react CHAT EVENT_ID EMOJI [--yes] [--money-ok]
python3 instagram.py edit CHAT EVENT_ID "new text" [--yes] [--money-ok]
python3 instagram.py delete CHAT EVENT_ID [--yes]
python3 instagram.py mark-read CHAT [--yes]
python3 instagram.py status | login | setup --token-file FILE | clear-hold [--yes]
```

`--json` on `chats` and `history` prints machine-readable rows for scripts (times in epoch ms).

Pacing: the gap between writes applies across chats. Up to 3 quick bubbles to the same chat within 60 s count as one reply and do not wait for it. The daily cap counts every write.

`CHAT` is a room id (`!abc:example.com`, copy it from `chats`) or a part of a chat name. A name that matches
two chats stops with exit 2 and lists both. A room that is not an Instagram portal stops with exit 1.

## Exit codes

0 ok, 1 refused or failed (nothing sent), 2 usage or setup, 3 the bridge or a guard is not ready (nothing
sent), 4 UNCERTAIN (do not resend; look at the chat), 5 a newer message than `--since`, 6 money lock,
7 the outbound gate refused.
