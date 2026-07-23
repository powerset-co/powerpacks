# import_whatsapp_wacli

WhatsApp import flow using `openclaw/wacli` instead of WAHA.

It installs/finds `wacli`, uses an isolated store at
`.powerpacks/messages/wacli`, authenticates with WhatsApp if needed, runs one
WhatsApp sync, and exports a Powerpacks-compatible contacts CSV.

An explicit `--sync-mode full` also deepens recent shallow DMs after the normal
sync. It selects current-year DMs with at most 20 stored rows using
`MAX(messages.ts)`, then runs target-specific history requests strictly
sequentially. Requests are paced within each chat, chats are paced from one
another, transient failures use exponential backoff, and two real no-growth
backfill attempts stop that chat.

The resumable metadata-only stage outputs are fixed:

```text
.powerpacks/messages/history-depth/
├── results.csv
├── progress.jsonl
└── manifest.json
```

## Privacy contract

- The Powerpacks export never reads message body columns.
- The exported CSV/JSONL contains only phone, name, source, group metadata,
  direct-chat message counts, and timestamps.
- wacli maintains its own local store for sync state under the configured
  `--store` directory.
- History-depth outputs contain only hashed chat references, counters, and
  outcome enums. They never persist names, phones, JIDs, message IDs, commands,
  stdout, stderr, or message bodies.
- Returned history is decrypted and persisted locally by wacli. No LLM, paid
  provider, or Powerset upload is involved.

## Usage

```bash
uv run --project . python packs/ingestion/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py run
```

Check install/auth/store state:

```bash
uv run --project . python packs/ingestion/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py status
```

Export from an existing store without syncing:

```bash
uv run --project . python packs/ingestion/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py export
```
