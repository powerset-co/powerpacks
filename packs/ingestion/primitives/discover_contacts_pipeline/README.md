# discover_contacts_pipeline

One local orchestration command for network source discovery inputs.

Skills are user-facing handlers; this primitive is the deterministic runtime
handler they call. In other words: `$import-email` / `$discover-contacts` route the
agent to a `SKILL.md`, and that skill calls this script.

## Routing table

| User command / skill | This orchestrator role | Source primitive/script |
| --- | --- | --- |
| `$import-email` | Calls this script with `--gmail-account-email`; this script discovers msgvault contacts | `gmail_network_import.py msgvault` |
| `$discover-contacts` | Calls this script for source discovery only | `linkedin_network_import.py`, `gmail_network_import.py msgvault` |
| `$import-twitter` | Runs Twitter primitive directly | `twitter_network_import.py` |
| `$import-contacts` | Produces message artifacts through messages pack primitives | messages pack primitives |

## Inputs

- LinkedIn CSV: LinkedIn `Connections.csv`, handled by `linkedin_network_import`.
- Gmail: local msgvault SQLite (`~/.msgvault/msgvault.db`), handled by `gmail_network_import msgvault`; multiple selected accounts from onboarding are imported into fixed per-account folders under `.powerpacks/network-import/discover/gmail/<account>/`.
- Setup/account state: `--from-accounts .powerpacks/ingestion/accounts.json` or `--from-setup .powerpacks/setup/setup-run.json` fills in LinkedIn CSV/source label, msgvault DB, selected Gmail accounts, Twitter handle, and message contacts artifacts unless explicit CLI flags override them.
- Messages: existing `.powerpacks/messages/research_review.csv`, produced by `$import-contacts`. Reviewed rows with usable LinkedIn `/in/` URLs are materialized into a local people CSV and hydrated through `enrich_people.py` during import/enrichment. Raw `.powerpacks/messages/contacts.csv` remains review-gated.
- Twitter/X: existing `.powerpacks/network-import/discover/twitter/*/people.csv`, produced by `twitter_network_import`; include with `--include-existing-artifacts`.

## Run

```bash
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run \
  --from-accounts .powerpacks/ingestion/accounts.json
```

## Parallel source discovery

`run --dry-run --from-accounts ...` emits `worker_groups.import.jobs`. Jobs for
Gmail accounts, LinkedIn CSV import/enrichment, Twitter, and messages artifacts
are independent and marked with `parallelizable` plus a reason. Gmail account
imports use fixed per-account output directories, so they can run concurrently
without creating run-specific artifact roots. LinkedIn uses its own child ledger
and hydrates LinkedIn profiles through the shared RapidAPI cache/fetch path.
Messages uses reviewed
local research artifacts only, then delegates LinkedIn profile hydration to
`enrich_people.py`.
Twitter remains an existing-artifact or dedicated-skill worker unless explicitly
approved; this orchestrator never runs `$import-contacts` research/upload implicitly.

Fan-in is not owned by this primitive. The indexing pipeline owns merge,
network DuckDB materialization, and local search indexing:

```bash
uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py run \
  --operator-id <operator-id>
```

Optional email LinkedIn resolution/enrichment bridge:

```bash
# first applies .powerpacks/network-import/directory.csv;
# then prepares local harness prompts only for still-unresolved rows, no spend/network
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run \
  --gmail-account-email arthur@powerset.co \
  --gmail-linkedin-provider harness

# or add an existing linkedin_resolutions.csv to the same apply/enrich pass
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py run \
  --gmail-account-email arthur@powerset.co \
  --gmail-resolutions-csv .powerpacks/network-import/discover/gmail/arthur-powerset-co/linkedin-resolution/linkedin_resolutions.csv
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

The orchestrator still does not bypass non-RapidAPI child confirmations. If an
optional provider such as Parallel returns `blocked_approval`, use:

```bash
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py approve
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/discover_contacts_pipeline.py continue
```

Gmail msgvault import, directory application, and Messages materialization are
local-only. Gmail email-to-LinkedIn provider resolution
is optional and only receives rows not already matched by `directory.csv`:
`--gmail-linkedin-provider harness` only prepares prompts;
`--gmail-linkedin-provider parallel` is spend-bearing and requires approval.
RapidAPI profile hydration only happens after resolutions are applied, or after
reviewed Messages LinkedIn rows are materialized, and is delegated to
`enrich_people.py` with its normal cache/fetch behavior.
