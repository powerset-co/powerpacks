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
3. **import** — automatically refresh linked sources when setup has not already
   done a recent live sync, reusing existing artifacts by default.
4. **index** — after import fan-in, run processing/indexing and local DuckDB
   materialization when safe or approved.

Onboarding must remain link-only. Do not run Gmail metadata import,
`msgvault sync-full`, LinkedIn RapidAPI enrichment, Twitter crawl,
`$import-contacts` research/upload, merge, or indexing from the onboarding
phase. Gmail import workers own `msgvault sync-full` for selected accounts
before they read the local msgvault DB.

Messages onboarding checks iMessage/Contacts permissions and WhatsApp auth/link
state only. WhatsApp link uses `import_whatsapp_wacli.py auth`; it must not run
WhatsApp sync or export contacts in the link phase. Messages import workers
later call `import_contacts_pipeline.py run` with explicit include flags, for
example `--include-imessage --include-whatsapp --include-contact-merge`, instead
of using stop-before/stop-after flags.

## How to explain setup to the user

Keep the user's view simple. Use plain product language first, and keep internal
terms like ledgers, fan-out/fan-in, worker jobs, artifacts, and DuckDB out of
normal updates unless the user asks for technical details.

Good opening summary:

```text
I’m going to get your local Powerpacks search ready in four steps:
1. Restore any safe prior progress we already have for you.
2. Connect the sources you choose, like Gmail, LinkedIn, Messages, Twitter, or WhatsApp.
3. Import each connected source in parallel where possible.
4. Combine everything into one local network and make it searchable on this machine.

I won’t upload anything automatically, and I’ll only stop to ask you when a login,
QR/device link, overwrite, or paid provider step needs your approval.
```

When reporting status, say what changed for the user:

```text
Gmail is connected for 2 accounts. LinkedIn is connected from your Connections.csv.
Setup will import anything that is stale or missing, reuse what is already current,
then finish quietly if there is nothing to do.
```

Avoid saying this to a normal user:

```text
I will dispatch fan-out workers with isolated ledgers and run fan-in after all jobs complete.
```

Instead say:

```text
I’ll run the independent imports in parallel so setup finishes sooner, then merge the results into one local network.
```

Use jargon only in hidden/internal notes, command logs, or when asking another
agent to execute a precise command.

## Commands

Use the setup runner as the normal entry point. It is idempotent: it restores a
safe matching bootstrap when needed, links are read from the account registry,
imports refresh automatically, and completed recent work is reused.

```bash
uv run --project . python packs/ingestion/primitives/setup/setup.py run \
  --operator-id <operator-id> \
  --accounts .powerpacks/ingestion/accounts.json \
  --setup-ledger .powerpacks/setup/setup-run.json
```

For inspection without running refresh work, use local status:

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

`pull-bootstrap` defaults to `--download-backend auto`: it uses `gcloud storage
cp` when Google Cloud CLI is installed, and can fall back to the project
`google-cloud-storage` dependency when run through `uv run --project .` with
`--download-backend python`. Raw service-account JSON in
`GOOGLE_APPLICATION_CREDENTIALS` is materialized to a temporary 0600 key and
cleaned up after either backend.

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
`gmail.config.account_emails`. Newly requested Gmail accounts stay in
`gmail.config.pending_accounts` until the returned `--gmail-authorized-email
<email>` rerun records them as linked. Do not create
`.powerpacks/network-import/gmail` outputs or run `msgvault sync-full` during
linking; Gmail sync/import starts only after the import confirmation.

## Import phase: automatic refresh, then fan-in

Do not ask for a separate "continue import" confirmation after setup has linked
sources. `$setup` should run the refresh when it is due, include existing artifacts
by default, and complete without ceremony when there is no new work.
Use normal user language:

```text
Your sources are linked. I’ll refresh anything that is missing or stale, reuse
the current local data, and then combine everything into one local network.

I won’t upload anything automatically. I’ll only interrupt you if a browser
login, QR/device link, overwrite, or paid provider approval is needed.
```

`setup.py run` is the default path. `setup.py handoff` remains available for
debugging or for agents that need to inspect worker commands:

```bash
uv run --project . python packs/ingestion/primitives/setup/setup.py handoff \
  --operator-id <operator-id> \
  --accounts .powerpacks/ingestion/accounts.json \
  --setup-ledger .powerpacks/setup/setup-run.json
```

If using the handoff output directly, the generated `import_network_*` commands
must include `--include-existing-artifacts`. Use the
`worker_groups.import.jobs` output to spin up parallel worker sub-agents where
possible:

- Gmail/msgvault workers per selected account; each worker runs
  `msgvault sync-full <email>` before `gmail_network_import.py msgvault` when
  msgvault is available.
- LinkedIn CSV import/enrichment worker.
- Twitter worker only when explicitly linked/approved.
- Messages/iMessage/WhatsApp contacts worker using explicit include flags.
  WhatsApp may require a QR/device-linking approval before contacts exist.

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
