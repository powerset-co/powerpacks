---
name: import-network
description: Orchestrate local LinkedIn CSV, msgvault email, existing messages, and Twitter artifacts into merged network contacts plus DuckDB. Use for $import-network.
---

# import-network

Use this skill for `$import-network` or end-to-end local network ingestion testing.

## Inputs

- `--linkedin-csv <Connections.csv>` and `--linkedin-source-user <label>` for LinkedIn.
- `--gmail-account-email <email>` and optional `--msgvault-db <path>` for email/msgvault.
- `--include-existing-artifacts` to include already-generated messages and Twitter people artifacts.

## Command

```bash
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run \
  --linkedin-csv <Connections.csv> \
  --linkedin-source-user <label> \
  --gmail-account-email <email>
```

Resume after a child approval gate:

```bash
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py approve
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py continue
```

## Outputs

The orchestrator writes merged CSVs and DuckDB under:

```text
.powerpacks/network-import/network-runs/<run-id>/
```

Key artifacts:

- `merged/people.csv`
- `merged/network_contacts.csv`
- `merged/network_contact_sources.csv`
- `merged/network_companies.csv`
- `duckdb/network.<run-id>.duckdb`

Do not upload automatically. Report artifact paths and counts only.
