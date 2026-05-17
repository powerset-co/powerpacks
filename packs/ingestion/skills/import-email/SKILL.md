---
name: import-email
description: Import Gmail/email network metadata from local msgvault into Powerpacks network artifacts and DuckDB. Use for $import-email or Gmail/msgvault import testing.
---

# import-email

Use this skill for `$import-email`, Gmail, email, or msgvault network import testing.

## Contract

Email import is msgvault-only. Do not call Powerset Gmail OAuth or backend gmail-sync endpoints.
Powerpacks reads local msgvault SQLite metadata only and never reads message bodies, subjects, snippets, raw MIME, or attachments.

## Default command

```bash
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run \
  --gmail-account-email <email>
```

If the user provides a DB path:

```bash
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run \
  --msgvault-db <path/to/msgvault.db> \
  --gmail-account-email <email>
```

For Arthur smoke testing:

```bash
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run \
  --msgvault-db ~/.msgvault/msgvault.db \
  --gmail-account-email arthur@powerset.co \
  --run-id arthur-email-smoke \
  --force
```

## Optional LinkedIn resolution/enrichment

The msgvault import writes `linkedin_resolution_queue.csv`. To recover the email
pipeline shape (email metadata -> LinkedIn URL resolution -> RapidAPI profile
enrichment), use the orchestrator bridge:

```bash
# no spend/network: prepare harness prompts
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run \
  --gmail-account-email <email> \
  --gmail-linkedin-provider harness

# after a linkedin_resolutions.csv exists, apply it and delegate to enrich_people
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run \
  --gmail-account-email <email> \
  --gmail-resolutions-csv <linkedin_resolutions.csv>
```

`--gmail-linkedin-provider parallel` is spend-bearing and must stop for explicit
approval. `enrich_people.py` then approval-gates RapidAPI LinkedIn cache misses.

## Expected outputs

- `.powerpacks/network-import/gmail/<run-id>-gmail/people.csv`
- `.powerpacks/network-import/gmail/<run-id>-gmail/linkedin_resolution_queue.csv`
- `.powerpacks/network-import/network-runs/<run-id>/merged/people.csv`
- `.powerpacks/network-import/network-runs/<run-id>/merged/network_contacts.csv`
- `.powerpacks/network-import/network-runs/<run-id>/merged/network_contact_sources.csv`
- `.powerpacks/network-import/network-runs/<run-id>/merged/network_companies.csv`
- `.powerpacks/network-import/network-runs/<run-id>/duckdb/network.<run-id>.duckdb`

Report counts only; do not print contact rows or email datasets.
