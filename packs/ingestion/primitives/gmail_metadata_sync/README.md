# gmail_metadata_sync

Deprecated. Powerpacks no longer uses Powerset-hosted Gmail OAuth/sync endpoints
for Gmail metadata import.

Use msgvault locally, then import its SQLite metadata:

```bash
uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py msgvault \
  --db ~/.msgvault/msgvault.db \
  --account-email me@gmail.com
```

The msgvault-backed import reads only metadata tables and never reads message
bodies, subjects, snippets, raw MIME, or attachments.
