# discover_contacts_pipeline

> **Legacy monolithic-orchestrator reference.** Current product flows use the
> split source-specific skills and handlers. Start with the
> [Gmail import pipeline](../../docs/gmail-import-pipeline.md),
> [Message import pipeline](../../docs/message-import-pipeline.md),
> and their `SKILL.md` files. The gmail import step functions live in
> `gmail/import_steps.py` (extracted from the retired before_split orchestrator,
> which has been deleted) and are not exposed by the current CLI.

One local orchestration command for network source discovery inputs.

Skills are user-facing handlers; this primitive is the deterministic runtime
handler they call. In other words: `$import-gmail` / `$discover-contacts` route the
agent to a `SKILL.md`, and that skill calls this script.

## Routing table

| User command / skill | This orchestrator role | Source primitive/script |
| --- | --- | --- |
| `$import-gmail` | Uses the split Gmail discovery and import handlers | `discover_contacts_pipeline/gmail/sync.py`, `import_contacts_pipeline/gmail/importer.py` |
| `$discover-contacts` | Calls this script for source discovery only | `linkedin/network_import.py`, `gmail/network_import.py msgvault` |
| `$import-twitter` | Runs Twitter primitive directly | `twitter/network_import.py` |
| `$import-messages` | Does not use this generic runner; it exclusively owns iMessage/WhatsApp | `discover_contacts_pipeline/messages/discover.py`, `import_contacts_pipeline/messages/importer.py`, and ingestion message primitives |

## Inputs

- LinkedIn CSV: LinkedIn `Connections.csv`, handled by `linkedin/network_import.py`.
- Gmail: local msgvault SQLite (`~/.msgvault/msgvault.db`), handled by `gmail/network_import.py msgvault`; multiple selected accounts from onboarding are imported into fixed per-account folders under `.powerpacks/network-import/discover/gmail/<account>/`.
- Setup/account state: `--from-accounts .powerpacks/ingestion/accounts.json` or `--from-setup .powerpacks/setup/setup-run.json` fills in LinkedIn CSV/source label, msgvault DB, selected Gmail accounts, and Twitter handle unless explicit CLI flags override them.
- Twitter/X: existing `.powerpacks/network-import/discover/twitter/*/people.csv`, produced by `twitter/network_import.py`; include with `--include-existing-artifacts`.

Message artifacts are not generic discovery inputs. `$import-messages` owns
iMessage/WhatsApp discovery, review, materialization, fan-in, and indexing.

## Run

```bash
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run \
  --from-accounts .powerpacks/ingestion/accounts.json
```

## Parallel source discovery

`run --dry-run --from-accounts ...` emits `worker_groups.import.jobs`. Jobs for
Gmail accounts, LinkedIn CSV import/enrichment, and Twitter artifacts are
independent and marked with `parallelizable` plus a reason. Gmail account
imports use fixed per-account output directories, so they can run concurrently
without creating run-specific artifact roots. LinkedIn uses its own child ledger
and hydrates LinkedIn profiles through the shared RapidAPI cache/fetch path.
Twitter remains an existing-artifact or dedicated-skill worker unless explicitly
approved; this orchestrator never dispatches `$import-messages` implicitly.

Fan-in is not owned by this primitive. The indexing pipeline owns the merge and
the single local search index:

```bash
uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py run \
  --operator-id <operator-id>
```

Optional email LinkedIn resolution/enrichment bridge:

```bash
# first applies .powerpacks/network-import/directory.csv;
# then prepares local harness prompts only for still-unresolved rows, no spend/network
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run \
  --gmail-account-email operator@example.com \
  --gmail-linkedin-provider harness

# or add an existing linkedin_resolutions.csv to the same apply/enrich pass
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run \
  --gmail-account-email operator@example.com \
  --gmail-resolutions-csv .powerpacks/network-import/discover/gmail/operator-example-com/linkedin-resolution/linkedin_resolutions.csv
```

The bridge maintains `.powerpacks/network-import/directory.csv`, a reusable
checkpoint keyed by `source_key` with `source`, `email`, `phone`, `name`,
`linkedin_url`, `public_identifier`, confidence, evidence, and source artifact
metadata. Existing local restore flows may populate this file before discovery.
Gmail/provider stages may write their own intermediate
`linkedin_resolutions.csv` files, but only the final combined stage output is
folded back into the canonical `directory.csv`. Gmail then applies matching
directory rows first, writes filtered unresolved queues for optional
harness/Parallel resolution, and delegates resolved LinkedIn rows to
`enrich_people.py`.

The pipeline writes:

- per-source artifacts under `.powerpacks/network-import/discover/{linkedin,gmail,...}`
- `.powerpacks/network-import/directory.csv` for reusable email/phone/name to LinkedIn mappings

## Approval behavior

RapidAPI profile hydration does not require an approval step. It runs when
`RAPIDAPI_LINKEDIN_KEY` or `RAPIDAPI_KEY` is configured and fails clearly when
neither key is available.

The current split discovery CLI does not expose the old `approve` or `continue`
commands. Follow the source-specific skill when a child blocks rather than using
the compatibility examples from this historical document.

Gmail msgvault import and directory application are local-only. Gmail
email-to-LinkedIn provider resolution
is optional and only receives rows not already matched by `directory.csv`:
`--gmail-linkedin-provider harness` only prepares prompts;
`--gmail-linkedin-provider parallel` is spend-bearing and requires approval.
RapidAPI profile hydration only happens after resolutions are applied and is
delegated to `enrich_people.py` with its normal cache/fetch behavior.
