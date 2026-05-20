# Ingestion architecture plan

## Current answer

`linkedin_network_import` does **not** currently call `enrich_people`.

As of this branch, `linkedin_network_import` is source-specific and delegates
profile hydration to `primitives/enrich_people/`.

Current split:

- `primitives/linkedin_network_import/`
  - parses LinkedIn `Connections.csv`
  - writes source-only `connections_for_enrichment.csv` and `source_people.csv`
  - delegates profile enrichment/cache/normalization to `enrich_people`
  - exposes canonical `people.csv` from the delegated run
- `primitives/enrich_people/`
  - accepts an existing shared people-schema CSV
  - queues rows that already have LinkedIn URLs/public identifiers
  - reads seeded RapidAPI profile cache before any network call
  - approval-gates RapidAPI cache misses
  - normalizes/merges provider payloads locally
  - writes canonical `people.csv`

The desired direction remains one shared LinkedIn enrichment primitive that all
network verticals feed into after they resolve or collect LinkedIn URLs.

## Target model

Every network/source vertical should converge on the same local interchange
schema, then hand rows with LinkedIn identity to one enrichment surface:

```txt
source import / account linking
  -> source-specific raw artifacts
  -> normalized local people rows with linkedin_url/public_identifier when known
  -> shared linkedin enrichment primitive
  -> people.csv
  -> optional merge/dedupe/export/indexing steps
```

Examples:

- LinkedIn Connections.csv: parse CSV -> people rows with LinkedIn URLs -> shared enrichment
- Twitter/X: crawl/score/resolve LinkedIn -> people rows with LinkedIn URLs -> shared enrichment
- Gmail/messages: metadata/contact rows -> resolve LinkedIn where possible -> shared enrichment
- Future source verticals: emit the same queue/people schema and reuse the same enrichment step

## Naming contract

Canonical new artifact name should be:

- `people.csv`

Legacy compatibility may temporarily keep aliases/manifests for:

- `people_harmonic_all.csv`
- `people_harmonic_all.merged.csv`

The schema module is already provider-neutral:

- `packs/ingestion/schemas/people_schema.py`

Even if raw provider columns remain in the schema (`harmonic_response`,
`rapidapi_response`) for audit/debug, the artifact should not be named after a
provider.

## Capability checklist

- [x] Account registry/onboarding exists (`account_registry`, `onboarding`).
- [x] Shared people schema exists (`schemas/people_schema.py`).
- [x] Local merge/dedupe exists (`merge_network_sources`).
- [x] LinkedIn Connections.csv import exists.
- [x] Generic LinkedIn enrichment exists (`enrich_people`).
- [ ] Harmonic provider calls exist only in older/legacy paths; shared enrichment is RapidAPI-only pending product decision.
- [x] RapidAPI LinkedIn provider calls exist in shared enrichment.
- [x] RapidAPI Twitter/X source crawl exists.
- [x] Provider calls are approval-gated.
- [x] `linkedin_network_import` delegates provider enrichment to `enrich_people` or shared library code.
- [ ] Twitter/Gmail/messages/future verticals all feed a shared LinkedIn enrichment queue/contract.
- [x] Canonical output artifact is `people.csv` across ingestion primitives touched so far.
- [x] Legacy `people_harmonic_all*.csv` naming is documented as compatibility-only for touched primitives.
- [x] Tests cover the shared LinkedIn CSV -> seeded cache -> `people.csv` contract without network calls.

## Work plan

### 1. Define the shared enrichment boundary

Create or formalize a single contract for rows entering LinkedIn enrichment:

Required/important fields:

- `id`
- `linkedin_url`
- `public_identifier`
- `full_name` / `first_name` / `last_name` when available
- source provenance columns: `source_channels`, `source_artifacts`

Outputs:

- `people.csv`
- `provider_enriched.csv`
- `linkedin_enrichment_queue.csv`
- `needs_resolution_queue.csv`
- `skipped_enrichment.csv`
- `raw_provider_responses/*.json`
- ledger/manifest with row counts, provider flags, approval status, and legacy aliases if emitted

### 2. Extract provider logic out of source importers

Refactor duplicate functions from `linkedin_network_import` and `enrich_people`
into a reusable implementation, or make `linkedin_network_import` produce an
input CSV and invoke/delegate to `enrich_people`.

Shared pieces centralized or being centralized:

- RapidAPI LinkedIn request wrapper
- local RapidAPI profile cache contract
- provider response normalization
- company identity normalization
- raw provider response persistence
- approval-gated ledger behavior for paid provider calls

Harmonic is not part of the current shared primitive; decide separately whether
to reintroduce it behind the same queue/cache/approval interface.

### 3. Make source primitives source-only

Keep source importers focused on source-specific collection/parsing:

- `linkedin_network_import`: parse `Connections.csv` into normalized people rows; do not own provider/cache/merge logic.
- `twitter_network_import`: crawl Twitter/X and resolve/validate LinkedIn; avoid becoming another independent people enrichment implementation.
- `gmail_network_import` / messages-derived flows: emit local people rows or resolution queues, then use shared enrichment.

### 4. Rename canonical artifacts to `people.csv`

Update writers/readers in this order:

1. Add `people.csv` output while still writing legacy aliases where needed. ✅
2. Update `merge_network_sources` discovery to prefer `people.csv`, falling back to `people_harmonic_all.csv`. ✅
3. Update READMEs/skills to refer to `people.csv` as canonical. ✅ for LinkedIn/Twitter/enrich/merge docs touched here.
4. Update tests to assert canonical names and compatibility fallback. ✅
5. Remove or de-emphasize legacy names after downstream consumers are updated.

### 5. Tighten tests

Add/adjust tests for:

- `enrich_people` approval gate before live provider calls.
- provider/cache behavior using seeded RapidAPI cache and mocked network calls.
- `linkedin_network_import` producing source-normalized rows and delegating to shared enrichment.
- `merge_network_sources` discovering `people.csv` first and legacy files second.
- no external calls in tests; all provider calls mocked.

### 6. Document operator workflow

Update ingestion skill docs to describe the common path:

```txt
import/link account -> produce/resolve LinkedIn URLs -> enrich LinkedIn profiles -> merge/export people.csv
```

Explicitly call out that live RapidAPI calls are spend-bearing and need operator
approval; seeded cache hits do not require keys, approval, or network access.

## Open decisions

- Should `enrich_people` remain the shared primitive name, or should it be
  renamed/split to something more explicit like `enrich_linkedin_people`?
- Should provider raw JSON columns remain embedded in `people.csv`, or move to
  sidecar files/manifests only with stable references in `source_artifacts`?
- Should `people.csv` contain only enriched people, or both enriched and skipped
  source rows? Current `enrich_people` preserves skipped complete rows in the
  enriched output.
- How long do we need to maintain `people_harmonic_all.csv` compatibility for
  existing `.powerpacks/` artifacts and downstream scripts?

## Non-goals for this plan

- No live provider calls without explicit approval.
- No Postgres/TurboPuffer writes or indexing changes.
- No generated datasets or provider dumps committed to the repo.
- No broad app/console changes unless they consume ingestion artifacts directly.
