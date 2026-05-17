# import_network_pipeline

One local orchestration command for network ingestion inputs.

## Inputs

- LinkedIn CSV: LinkedIn `Connections.csv`, handled by `linkedin_network_import`.
- Gmail: local msgvault SQLite (`~/.msgvault/msgvault.db`), handled by `gmail_network_import msgvault`.
- Messages: existing `.powerpacks/messages/contacts.csv`, produced by `$import-contacts`; include with `--include-existing-artifacts`.
- Twitter/X: existing `.powerpacks/network-import/twitter/*/people.csv`, produced by `twitter_network_import`; include with `--include-existing-artifacts`.

## Run

```bash
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run \
  --linkedin-csv ~/Downloads/Connections.csv \
  --linkedin-source-user casper \
  --gmail-account-email arthur@powerset.co
```

The pipeline writes:

- per-source artifacts under `.powerpacks/network-import/{linkedin,gmail,...}`
- merged CSVs under `.powerpacks/network-import/network-runs/<run-id>/merged/` (`people.csv`, `network_contacts.csv`, `network_contact_sources.csv`, `network_companies.csv`)
- DuckDB under `.powerpacks/network-import/network-runs/<run-id>/duckdb/`

## Approval behavior

The orchestrator does not bypass child gates. If LinkedIn enrichment needs paid
RapidAPI calls, it returns `blocked_approval`; use:

```bash
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py approve
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py continue
```

Gmail msgvault import, merge, and DuckDB loading are local-only.
