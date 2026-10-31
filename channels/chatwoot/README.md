**Mock only. This code has never run against a real account.**

# Chatwoot channel

Read and reply to customer conversations in Chatwoot, through its Application API. Python standard library only.

## What the tests prove, and what they do not

The tests start a real HTTP server on 127.0.0.1 (random port) and run the tool against it. So the real HTTP code runs: the `api_access_token` header, a 401, a 500, a timeout, a dropped connection, a redirect, and paging.

The server is written by us. It proves the tool's rules and its HTTP handling. It does not prove that a real Chatwoot answers in exactly this shape.

## API facts

Source: Chatwoot's published OpenAPI file, `swagger/tag_groups/application_swagger.json` on the `develop` branch of github.com/chatwoot/chatwoot. It was read through the context7 tool and by downloading the file. It was read once, not run.

Checked in that file:

- `GET /api/v1/accounts/{account_id}/conversations`: query `status` (all, open, resolved, pending, snoozed), `assignee_type` (me, unassigned, all, assigned), `q` (search text in messages), `inbox_id`, `team_id`, `labels`, `page`. The answer holds `data.payload[]` and `data.meta`. Fields used: `id`, `status`, `unread_count`, `inbox_id`, `meta.sender`, `messages`, `last_non_activity_message`.
- `GET .../conversations/{id}`: `can_reply`, `meta.sender` (name, email, phone_number), `meta.channel`.
- `GET .../conversations/{id}/messages`: query `before` (up to 20 messages before that id, oldest first) and `after`. The answer holds `payload[]`. Fields used: `id`, `content`, `message_type` (0 incoming, 1 outgoing, 2 activity, 3 template), `created_at` (integer), `private`, `content_attributes.in_reply_to`, `sender.name`.
- `POST .../conversations/{id}/messages`: body `content`, `message_type` ("outgoing"), `private` (true is a private note). The answer holds the new message `id`.
- `POST .../conversations/{id}/toggle_status`: body `status`.
- `POST .../conversations/{id}/update_last_seen`.
- Security: `api_access_token` header, which the file calls supported but "will be deprecated in a future release". It also lists Bearer authentication (Chatwoot 4.19.0 and later). This tool sends `api_access_token`, as the owner asked. If a newer Chatwoot drops it, change one line in `http_request`.

Could not check:

- How many conversations one `page` returns. The file does not say. The tool pages until an empty page, up to 20 pages.
- Whether `GET .../messages` without `before` returns the latest 20 messages. The file describes only `before` and `after`. The tool assumes the latest page, oldest first. The `--since` check depends on this.
- Rate limits, and the exact text of error bodies.
- Whether a `q` search also covers contact names. The tool treats it as message search only.
- Mark-read: `update_last_seen` is documented as "updates the last seen of the conversation". Whether this clears the unread badge for an API token user was not checked.

## What the owner must do to go live

About 15 minutes.

1. In Chatwoot, open Profile Settings and copy your access token. Use a token for an agent who can see the inboxes you want.
2. Put the token in a secret store. Set `chatwoot.token_command` to a command that prints it.
3. Set `chatwoot.base_url` (for example `https://chatwoot.example.com`) and `chatwoot.account_id` (the number in the Chatwoot URL after `/accounts/`).
4. Run `python3 chatwoot.py conversations`.
5. Try `thread ID`, then a `note` on a test conversation, then one `reply` on a test conversation. Use the dry run, then `--yes`.

Plain `http://` is accepted only for 127.0.0.1 and localhost. A real host must use `https://`.

## Risk (terms of service)

Low. This is Chatwoot's own API. Chatwoot is self-hosted, or a paid cloud service. On a self-hosted server there is no third party to ban you. On Chatwoot Cloud, normal API use by an agent token is allowed. The real risk is a wrong reply to a real customer, which is why every reply is a dry run first.

## Commands

```
python3 chatwoot.py conversations [--status S] [--assignee A] [-n N]
python3 chatwoot.py unread [-n N]
python3 chatwoot.py thread ID [--all]
python3 chatwoot.py find TEXT [-n N]
python3 chatwoot.py reply ID "text" --since TIME [--yes] [--money-ok]
python3 chatwoot.py note ID "text" [--yes]
python3 chatwoot.py resolve ID [--yes]
python3 chatwoot.py mark-read ID [--yes]
```

## Rules

- A write is a dry run unless `--yes`. A dry run calls no write endpoint.
- `reply --yes` without `--since` exits 2.
- A message newer than `--since` exits 5 and prints `NOT SENT:`. A customer message, a reply to an old message and a teammate's **private note** all count. A private note blocks on purpose: a teammate can write "do not reply" in one. Chatwoot's own activity rows (`message_type` 2: assignment, label and status changes) do not block, because nobody wrote them. If the latest page holds only activity rows, the check passes.
- An unknown result exits 4 and prints `UNCERTAIN`. That means a timeout, a dropped connection, or an HTTP 3xx or 5xx answer to a write. The tool never retries. Only a clear 4xx answer counts as "not done".
- Money words exit 6 until `--money-ok`.
- `note`, `resolve` and `mark-read` load the settings and read the conversation before they print the dry-run line. With bad settings, a refused token or an unknown conversation they stop with an error and never say "Would ...".
- A real reply writes one archive row with `hook_covers=False`. The row's target is the contact's email, else phone, else `chatwoot:ACCOUNT:ID`.
- `note`, `resolve` and `mark-read` write no archive row. They send nothing to a customer.
- Redirects are never followed, so the token cannot reach another host.
- A `reply` to a conversation with `can_reply: false` is refused.

## Exit codes

0 ok. 1 refused or failed. 2 usage or setup problem, including a refused token. 4 UNCERTAIN write. 5 newer message. 6 money lock. 7 outbound gate refused.

## Settings

See `config.example.json`. Keys: `chatwoot.base_url`, `chatwoot.account_id`, `chatwoot.token_command`.
