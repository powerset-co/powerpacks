# discover/messages/channels — `messages_imessage_extract`, `messages_whatsapp_extract`

Two declared nodes share this package, one per message source; both are
`(MessageChannel, Node)` subclasses whose whole step is `execute()`:
`messages_imessage_extract` (`IMessageChannel`, `i_message_channel.py`) and
`messages_whatsapp_extract` (`WhatsAppChannel`, `whats_app_channel.py`).

Declared in `pipeline/graph.py`; contract in `pipeline/contract.py`. Stage
overview: [discover/README.md](../../README.md) and
[messages/README.md](../README.md).

| node | reads | writes | manifest |
|---|---|---|---|
| `messages_imessage_extract` | `~/Library/Messages/chat.db` (external, optional) | `messages/imessage.contacts.csv` (full_rewrite) | none (`manifest = ""`) |
| `messages_whatsapp_extract` | `messages/wacli/wacli.db` (external, optional) | `messages/whatsapp.contacts.csv` (full_rewrite) | none (`manifest = ""`) |

Paths are under `.powerpacks/`; neither channel writes a manifest of its own —
they return their payload and the store composes the stage manifest.

## Manifest / status

Payloads: `MessageChannelExtracted` (status `completed`), `MessageChannelBlocked`
(status `blocked_user_action`, carries the message + continue command), and
`MessageChannelFailed` (status `failed`, carries `step_id`). The blocked/failed
payload short-circuits the store's run loop.

## Control

Free; metadata only — the extractors never select message body columns.

## Invariant

The chat.db / wacli.db inputs are `required=False` on purpose: under a macOS Full
Disk Access denial the file still exists and `os.access` returns true while TCC
refuses at open, so a `required` input would not catch the real failure and would
replace the actionable `blocked_user_action` payload with a bare `not_ready`.
The gate is the extractor's own check, not the declaration.
