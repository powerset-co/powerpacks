---
name: setup
description: Unified Powerpacks ingestion setup. Use for $setup, one-time setup, operator bootstrap restore, account/source linking, import/enrichment fan-out, and local search-index/DuckDB readiness.
---

# setup

Use this skill for `$setup` and first-run Powerpacks ingestion setup. This is
an ingestion/product setup flow, not a generic `$powerset login` alias.

## Phase model

1. **bootstrap** — inspect/pull/apply operator bootstrap bundles as prior local
   artifacts/checkpoints.
2. **link** — run onboarding as source linking only; record non-secret state in
   `.powerpacks/ingestion/accounts.json`.
3. **import** — after explicit user confirmation, dispatch parallel worker
   sub-agents for independent linked source imports/enrichment.
4. **index** — after import fan-in, run processing/indexing and local DuckDB
   materialization when safe or approved.

Onboarding must remain link-only. Do not run Gmail metadata import, LinkedIn
RapidAPI enrichment, Twitter crawl, `$import-contacts` research/upload, merge,
or indexing from the onboarding phase.

## Commands

Start with local status/handoff inspection:

```bash
uv run --project . python packs/ingestion/primitives/setup/setup.py status \
  --operator-id <operator-id> \
  --accounts .powerpacks/ingestion/accounts.json \
  --setup-ledger .powerpacks/setup/setup-run.json
```

If the user has a local operator bootstrap bundle, inspect before applying:

```bash
uv run --project . python packs/ingestion/primitives/setup/setup.py inspect-bootstrap \
  --bundle .powerpacks/operator-bootstrap/bundles/<operator>.operator-bootstrap.tar.gz
```

Only after explicit approval, apply with `--force` if overwrites are required:

```bash
uv run --project . python packs/ingestion/primitives/setup/setup.py apply-bootstrap \
  --bundle .powerpacks/operator-bootstrap/bundles/<operator>.operator-bootstrap.tar.gz \
  --operator-id <operator-id> \
  --force
```

Remote GCS bootstrap pulls require explicit approval and an exact object URI:

```bash
uv run --project . python packs/ingestion/primitives/setup/setup.py pull-bootstrap \
  --gcs-uri gs://bucket/path/operator-bootstrap.tar.gz \
  --output .powerpacks/operator-bootstrap/bundles/<operator>.operator-bootstrap.tar.gz \
  --allow-gcs-download
```

Then link sources:

```bash
uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py step \
  --accounts .powerpacks/ingestion/accounts.json \
  --operator-id <operator-id>
```

## Gmail/msgvault multi-account linking

For Gmail, route through existing msgvault automation. Browser/GCP actions are
user-action/linking steps, not import steps:

- `packs/ingestion/primitives/msgvault_setup/msgvault_setup.py browser-setup`
  creates/configures the Google OAuth Desktop app and can authorize the first
  account with `--add-account`.
- `msgvault_setup.py add-test-users <emails...>` uses browser automation to add
  more OAuth test users.
- `msgvault_setup.py add-account --email <email>` authorizes each additional
  Gmail account.

After msgvault is configured and local accounts are discoverable, ask once:

```text
Which other Gmail accounts should we link before import?
```

Record selected accounts in `gmail.config.selected_accounts` /
`gmail.config.account_emails`. Do not create `.powerpacks/network-import/gmail`
outputs during linking.

## Import phase: parallel fan-out, then fan-in

Before import/enrichment, ask one explicit confirmation to leave the link phase.
After confirmation, use `setup.py handoff`:

```bash
uv run --project . python packs/ingestion/primitives/setup/setup.py handoff \
  --operator-id <operator-id> \
  --accounts .powerpacks/ingestion/accounts.json \
  --setup-ledger .powerpacks/setup/setup-run.json
```

Use the `worker_groups.import.jobs` output to spin up parallel worker sub-agents
where possible:

- Gmail/msgvault workers per selected account.
- LinkedIn CSV import/enrichment worker.
- Twitter worker only when explicitly linked/approved.
- Messages/iMessage artifacts worker; WhatsApp may require a QR/device-linking
  approval before artifacts exist.

Workers must use isolated ledgers/run ids for `--only-source` source jobs and
must return blocked approvals to the main thread. Merge/network DuckDB fan-in
runs only after all selected source workers complete or block.

## Consent boundaries

The main thread owns these approvals. Never let workers approve them silently:

- browser/Gmail OAuth and gcloud login;
- GCP Desktop OAuth app creation and OAuth test-user additions;
- adding/authing each extra Gmail account;
- WhatsApp QR/device linking;
- exact-object GCS bootstrap download;
- destructive bootstrap restore/overwrite (`--force`);
- RapidAPI, Parallel, OpenAI/TLM, embedding, or other provider spend;
- `$import-contacts` research/review/upload or any upload/prod write.

Local status, msgvault account listing, import-network dry-run, processing
`plan`, local merge, and local DuckDB materialization may run without spend
approval.

## Index phase

If bootstrap restored `.powerpacks/search-index/local-search.duckdb` and a
verified ledger/records, report local search ready. If records exist without
DuckDB, run only local materialization:

```bash
uv run --project . python scripts/build-local-duckdb-shim.py \
  --records-dir .powerpacks/search-index \
  --operator-id <operator-id> \
  --force
```

If only `.powerpacks/network-import/merged/people.csv` exists, run
`build_processing_pipeline.py plan` first. Real provider stages require
precomputed artifacts or explicit paid/provider approval flags.
