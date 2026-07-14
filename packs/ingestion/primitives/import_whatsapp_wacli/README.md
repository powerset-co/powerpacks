# import_whatsapp_wacli

WhatsApp import flow using `openclaw/wacli` instead of WAHA.

It installs/finds `wacli`, uses an isolated store at
`.powerpacks/messages/wacli`, authenticates with WhatsApp if needed, runs one
WhatsApp sync, and exports a Powerpacks-compatible contacts CSV.

## Privacy contract

- The Powerpacks export never reads message body columns.
- The exported CSV/JSONL contains only phone, name, source, group metadata,
  direct-chat message counts, and timestamps.
- wacli maintains its own local store for sync state under the configured
  `--store` directory.

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
