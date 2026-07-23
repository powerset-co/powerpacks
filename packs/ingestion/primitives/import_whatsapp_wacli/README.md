# import_whatsapp_wacli

WhatsApp import flow using `openclaw/wacli` instead of WAHA.

It installs/finds `wacli`, uses an isolated store at
`.powerpacks/messages/wacli`, authenticates with WhatsApp if needed, runs one
WhatsApp sync, and exports a Powerpacks-compatible contacts CSV. The primitive
chooses the sync strategy from the local store: an empty store gets an
unbounded account sync; a populated store gets an incremental sync.

Every run then deepens recent shallow DMs automatically. On the first depth
pass, it selects every DM with at most 20 stored rows whose actual
`MAX(messages.ts)` is within the last three years. Later runs snapshot each
DM's `(COUNT(*), MAX(messages.ts))` immediately before sync and target only
recent shallow chats that changed, plus unfinished targets from the prior pass.
Requests run strictly sequentially, with pacing between native requests and
chats, exponential backoff for transient failures, and a stop after two
successful no-growth attempts.

The manifest keeps one privacy-safe digest of direct-chat counts and latest
timestamps. If a sync is interrupted before its changed chats are seeded, or a
targeted request returns rows for another chat, the next invocation detects the
drift and performs a catch-up bootstrap. No per-chat snapshot file is needed.

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
