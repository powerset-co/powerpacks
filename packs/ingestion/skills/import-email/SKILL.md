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
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run \
  --gmail-account-email <email>
```

If the user provides a DB path:

```bash
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run \
  --msgvault-db <path/to/msgvault.db> \
  --gmail-account-email <email>
```

For Arthur smoke testing:

```bash
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run \
  --msgvault-db ~/.msgvault/msgvault.db \
  --gmail-account-email arthur@powerset.co \
  --force
```

## Optional LinkedIn resolution/enrichment

The msgvault import writes `linkedin_resolution_queue.csv`. To recover the email
pipeline shape (email metadata -> LinkedIn URL resolution -> RapidAPI profile
enrichment), use the orchestrator bridge. It first applies the canonical
`.powerpacks/network-import/directory.csv` checkpoint restored from operator
bootstrap or updated by successful stage outputs. Only rows still unresolved by
the directory are prepared for a LinkedIn-resolution provider.

```bash
# no spend/network: prepare harness prompts
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run \
  --gmail-account-email <email> \
  --gmail-linkedin-provider harness

# after a linkedin_resolutions.csv exists, apply it and delegate to enrich_people
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run \
  --gmail-account-email <email> \
  --gmail-resolutions-csv <linkedin_resolutions.csv>
```

`--gmail-linkedin-provider parallel` is spend-bearing and must stop for explicit
approval. `enrich_people.py` then hydrates resolved LinkedIn rows through the
RapidAPI cache/fetch path; cache misses run immediately when a RapidAPI key is
configured. Resolved directory/provider rows are combined per Gmail account
before `enrich_people.py` runs.

## Expected outputs

- `.powerpacks/network-import/directory.csv`
- `.powerpacks/network-import/discover/gmail/<account>/people.csv`
- `.powerpacks/network-import/discover/gmail/<account>/linkedin_resolution_queue.csv`
- `.powerpacks/network-import/final/merged/people.csv`
- `.powerpacks/network-import/final/merged/network_contacts.csv`
- `.powerpacks/network-import/final/merged/network_contact_sources.csv`
- `.powerpacks/network-import/final/merged/network_companies.csv`
- `.powerpacks/network-import/final/duckdb/network.local.duckdb`

Report counts only; do not print contact rows or email datasets.
