---
name: import-network
description: Orchestrate local LinkedIn CSV, msgvault email, existing messages, and Twitter artifacts into merged network contacts plus DuckDB. Use for $import-network.
---

# import-network

Use this skill for `$import-network` or end-to-end local network ingestion testing.

## User-facing tone

Be clear and calm. The user should hear what is happening in product terms, not
pipeline jargon. Do not normally say fan-out, fan-in, ledgers, artifacts,
sub-agents, or DuckDB. Use those terms only in internal execution notes or if the
user asks for technical details.

Use this style instead:

```text
I found these connected sources:
- Gmail: 2 accounts
- LinkedIn: Connections.csv
- Messages: contacts.csv

I’m going to import each source in parallel where possible, then combine the
results into one local network and prepare it for local search.

I won’t upload anything automatically. I’ll only stop if I need a browser login,
a QR/device link, an overwrite approval, or approval for a paid provider step.
```

For progress updates, report user-visible progress and counts:

```text
Gmail import is running for 2 accounts. LinkedIn import is also running. After
those finish, I’ll combine the results into one local network.
```

```text
Import finished. I combined the connected sources into the local network files.
Next I’ll prepare the local search index.
```

If a provider/spend step blocks, explain the choice plainly:

```text
I found contacts that need paid LinkedIn/profile enrichment before I can improve
their profiles. I won’t run that automatically. Do you want me to approve this
step, skip it for now, or continue with only the local data?
```

## After Onboarding

If `$onboard` has linked sources, propose the concrete import command and ask
for one confirmation before long sync/import work:

```text
Your sources are connected. I can now import them, combine the results into one
local network, and prepare the files needed for local search. Large mailboxes or
large networks can take a while. I won’t upload anything automatically, and I’ll
only ask again if a login, QR/device link, overwrite, or paid provider step needs
approval. Continue?
```

After confirmation, run the pipeline until it completes or reaches a real
approval gate. Do not ask again for routine local metadata import work.

## Inputs

- `--linkedin-csv <Connections.csv>` and `--linkedin-source-user <label>` for LinkedIn.
- `--gmail-account-email <email>` / repeated `--gmail-account-emails <email>` and optional `--msgvault-db <path>` for email/msgvault.
  The Gmail import worker owns `msgvault sync-full` for each selected account
  before reading the local msgvault DB. Do not run this sync from onboarding.
  Use `--skip-msgvault-sync` only for tests or known pre-synced local DBs.
- `--from-accounts .powerpacks/ingestion/accounts.json` or `--from-setup .powerpacks/setup/setup-run.json` to consume link-only state from `$setup` / `$onboard`.
- `--include-existing-artifacts` to include already-generated messages and Twitter people artifacts.

## Command

```bash
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run \
  --from-accounts .powerpacks/ingestion/accounts.json
```

`run --dry-run --from-accounts ...` reports source worker jobs with
`parallelizable: true/false`. LinkedIn, Gmail/msgvault accounts, approved
Twitter imports, and existing messages/iMessage/WhatsApp artifacts have no
cross-source dependency, so `$setup` may dispatch them in parallel sub-agents.
The fan-in merge and network DuckDB phases must wait for all selected source
workers to complete or block on an approval gate.

That paragraph is for execution planning. Do not repeat it to the user. Say:

```text
These sources can be imported at the same time, so I’ll run them in parallel and
then combine the results when they finish.
```

For manual worker fan-out, run source workers with `--only-source` and isolated
ledgers/run ids, then run the normal command (or `--fan-in-only`) for merge and
DuckDB after all source workers finish. Do not approve RapidAPI/Parallel/OpenAI
gates inside workers; return those gates to the main thread.

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
