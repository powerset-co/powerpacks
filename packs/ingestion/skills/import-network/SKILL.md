---
name: import-network
description: Orchestrate local LinkedIn CSV, msgvault email, existing messages, and Twitter artifacts into merged network contacts plus DuckDB. Use for $import-network.
---

# import-network

Use this skill for `$import-network` or end-to-end local network ingestion testing.

## After Onboarding

If `$onboard` has linked sources, propose the concrete import command and ask
for one confirmation before long sync/import work:

```text
We can now import Gmail and LinkedIn metadata, resolve LinkedIn matches, enrich profiles after approval, merge sources, and build a local DuckDB. Large mailboxes or large networks can take a few hours. Continue?
```

After confirmation, run the pipeline until it completes or reaches a real
approval gate. Do not ask again for routine local metadata import work.

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

## Bootstrap Prior Checkpoints

When existing operator export/checkpoint CSVs are available, first generate a
Powerpacks bootstrap bundle:

```bash
uv run --project . python packs/ingestion/primitives/bootstrap_network_from_exports/bootstrap_network_from_exports.py generate \
  --operator-mapping <operator_mapping.json> \
  --source-dir <existing-export-csv-dir> \
  --operators <operator-slug> \
  --linkedin-csv <Connections.csv> \
  --gmail-account-email <email> \
  --seed-profile-cache \
  --force
```

Then run the command printed in
`.powerpacks/network-bootstrap/operators/<operator-slug>/outputs/commands.txt`.

Resume after a child approval gate:

```bash
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py approve
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py continue
```

## Long Runs

Run the command in a visible shell and keep `[import-network]` /
`[enrich-people]` progress lines visible. For large mailboxes or profile sets,
use the status command every few minutes instead of treating silence as failure:

```bash
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py status \
  --ledger .powerpacks/network-import/import-network-run.json
```

If the pipeline blocks on paid RapidAPI or another spend-bearing step, show the
count and ask for approval before running `approve`.

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
