# extract_whatsapp_contacts

Pull WhatsApp contact metadata from a local WAHA session into CSV/JSONL.
Stdlib-only.

This primitive does not start Docker, does not start the container, and does
not handle QR auth. Use:

1. `waha_runtime up` — pulls/starts the WAHA container
2. `waha_session start --open --wait` — creates the session, shows the QR
3. `extract_whatsapp_contacts extract` — produces the contact CSV

## Privacy contract

- Never reads or stores message content
- Only collects: `phone, name, source, is_in_group_chats, group_names,
  message_count, last_message`
- Output schema matches `normalize_message_contacts` and the canonical
  `message-contact.schema.json`

## Usage

```bash
# Verify the session is authenticated.
python packs/messages/primitives/extract_whatsapp_contacts/extract_whatsapp_contacts.py check

# Pull contacts exhaustively. Writes CSV/JSONL, manifest, and progress JSONL.
# Large histories can take up to an hour; keep message counts enabled.
python packs/messages/primitives/extract_whatsapp_contacts/extract_whatsapp_contacts.py extract \
  --output-csv .powerpacks/messages/whatsapp.contacts.csv \
  --output-jsonl .powerpacks/messages/whatsapp.contacts.jsonl

# Debug/last-resort only: skip per-chat message-count pagination.
python packs/messages/primitives/extract_whatsapp_contacts/extract_whatsapp_contacts.py extract \
  --skip-message-counts
```

## Progress / heartbeat

While counting messages, the primitive emits JSON progress events to stderr and
writes the same events to `<manifest>.progress.jsonl` (or `--progress-jsonl`).
Harnesses should monitor those heartbeats and allow long exhaustive syncs to run
rather than killing the process or retrying with `--skip-message-counts`.

The import-contacts orchestrator rescans live WhatsApp on fresh runs. The
incremental cache is per-chat message counts: unchanged chats reuse cached
counts, while new or changed chats are counted from WAHA.

## Failure mode

If the session is not `WORKING`, the primitive writes an empty CSV plus a
manifest containing the WAHA session state and exits non-zero. The harness can
inspect the manifest, ask the user to re-run `waha_session start`, and replay.
