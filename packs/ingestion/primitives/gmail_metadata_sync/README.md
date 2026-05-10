# gmail_metadata_sync

Local wrapper for a server-side Gmail metadata sync endpoint.

This primitive does **not** read Gmail bodies/subjects locally. It uses the local Powerset bearer token and calls a backend endpoint, defaulting to:

`POST https://search-api-7wk4uhe77q-uw.a.run.app/v2/integrations/gmail-sync`

The backend endpoint still needs to exist/own OAuth refresh-token access and metadata-only ingestion.

```bash
uv run --project . python packs/ingestion/primitives/gmail_metadata_sync/gmail_metadata_sync.py run
uv run --project . python packs/ingestion/primitives/gmail_metadata_sync/gmail_metadata_sync.py approve
uv run --project . python packs/ingestion/primitives/gmail_metadata_sync/gmail_metadata_sync.py continue
```

The sync trigger is approval-gated.
