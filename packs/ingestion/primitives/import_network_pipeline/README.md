# import_network_pipeline

One local orchestration command for network ingestion inputs.

Skills are user-facing handlers; this primitive is the deterministic runtime
handler they call. In other words: `$import-email` / `$import-network` route the
agent to a `SKILL.md`, and that skill calls this script.

## Routing table

| User command / skill | This orchestrator role | Source primitive/script |
| --- | --- | --- |
| `$import-email` | Calls this script with `--gmail-account-email`; this script imports msgvault, merges, loads DuckDB | `gmail_network_import.py msgvault` |
| `$import-network` | Calls this script for end-to-end local network ingestion | `linkedin_network_import.py`, `gmail_network_import.py msgvault`, `merge_network_sources.py`, `build_network_duckdb.py` |
| `$import-twitter` | Runs Twitter primitive first, then use this script with `--include-existing-artifacts` to merge/load DuckDB | `twitter_network_import.py` |
| `$import-contacts` | Produces message artifacts first, then use this script with `--include-existing-artifacts` | messages pack primitives |

## Inputs

- LinkedIn CSV: LinkedIn `Connections.csv`, handled by `linkedin_network_import`.
- Gmail: local msgvault SQLite (`~/.msgvault/msgvault.db`), handled by `gmail_network_import msgvault`; multiple selected accounts from onboarding are imported with isolated Gmail child run ids.
- Setup/account state: `--from-accounts .powerpacks/ingestion/accounts.json` or `--from-setup .powerpacks/setup/setup-run.json` fills in LinkedIn CSV/source label, msgvault DB, selected Gmail accounts, Twitter handle, and message contacts artifacts unless explicit CLI flags override them.
- Messages: existing `.powerpacks/messages/contacts.csv`, produced by `$import-contacts`; include with `--include-existing-artifacts`.
- Twitter/X: existing `.powerpacks/network-import/twitter/*/people.csv`, produced by `twitter_network_import`; include with `--include-existing-artifacts`.

## Run

```bash
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run \
  --from-accounts .powerpacks/ingestion/accounts.json
```

## Parallel source fan-out / fan-in

`run --dry-run --from-accounts ...` emits `worker_groups.import.jobs`. Jobs for
Gmail accounts, LinkedIn CSV import/enrichment, Twitter, and messages artifacts
are independent and marked with `parallelizable` plus a reason. Gmail account
imports use one deterministic child run id per account and isolated output
directories, so they can run concurrently. LinkedIn uses its own child ledger and
may pause for an existing RapidAPI approval confirmation. Twitter and messages remain
existing-artifact or dedicated-skill workers unless explicitly approved; this
orchestrator never runs `$import-contacts` research/upload implicitly.

Fan-in (`merge_network_sources.py` and `build_network_duckdb.py`) runs only after
selected source workers complete or return a blocked approval. Manual fan-out can
use `--only-source gmail|linkedin_csv|twitter|messages` with isolated ledgers;
run the normal command, or `--fan-in-only`, afterward to merge/load DuckDB from
the produced artifacts.

Optional email LinkedIn resolution/enrichment bridge:

```bash
# prepare local harness prompts, no spend/network
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run \
  --gmail-account-email arthur@powerset.co \
  --gmail-linkedin-provider harness

# or use an existing linkedin_resolutions.csv and feed resolved rows into shared enrich_people
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run \
  --gmail-account-email arthur@powerset.co \
  --gmail-resolutions-csv .powerpacks/network-import/gmail/<run-id>/linkedin-resolution/linkedin_resolutions.csv
```

The pipeline writes:

- per-source artifacts under `.powerpacks/network-import/{linkedin,gmail,...}`
- merged CSVs under `.powerpacks/network-import/network-runs/<run-id>/merged/` (`people.csv`, `network_contacts.csv`, `network_contact_sources.csv`, `network_companies.csv`)
- DuckDB under `.powerpacks/network-import/network-runs/<run-id>/duckdb/`

## Approval behavior

The orchestrator does not bypass child confirmations. If LinkedIn enrichment needs paid
RapidAPI calls, it returns `blocked_approval`; use:

```bash
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py approve
uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py continue
```

Gmail msgvault import, merge, and DuckDB loading are local-only. Gmail
email-to-LinkedIn resolution is optional: `--gmail-linkedin-provider harness`
only prepares prompts; `--gmail-linkedin-provider parallel` is spend-bearing and
requires approval. RapidAPI profile hydration only happens after resolutions are
applied and is delegated to `enrich_people.py` with its normal approval/cache
behavior.
