---
name: build-local-search-index
description: Build or inspect the local search index from the canonical merged people.csv without Modal, Postgres, Supabase, or TurboPuffer. Planning and dry-run are local-only; a full build reuses caches and may call configured classification/embedding providers for uncovered work after its estimate.
---

# Build Local Search Index

Build local indexing artifacts from canonical Powerpacks people data.

For the product-level distinction between the standard Modal setup path and
this local execution path, read
[`packs/indexing/README.md`](../../README.md).

## Canonical input

Prefer the canonical merged people CSV written by the ingestion fan-in merge:

```text
.powerpacks/network-import/merged/people.csv
```

If the aggregate merge output is missing or stale, run the contacts indexing
pipeline. It performs fan-in first, promotes the canonical
`.powerpacks/network-import/merged/people.csv`, then indexes that file.

## Run locally

Plan inspection is local-only and safe:

```bash
uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py plan --operator-id <operator-id>
```

First inspect the processing estimate:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run \
  --dry-run \
  --input .powerpacks/network-import/merged/people.csv \
  --output-dir .powerpacks/search-index \
  --default-operator-id <operator-id>
```

If it reports paid provider calls or non-zero estimated cost, show it to the
user and obtain explicit approval. Then run the full local contacts indexing
pipeline:

```bash
uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py run --operator-id <operator-id>
```

The run emits a dry-run cost estimate in the stage manifest before processing.
When the user starts indexing from the app or this command, the same run proceeds
with the fixed-output pipeline and uses existing stage artifacts as caches.

The run is single-index and idempotent: a partial index resumes from
`.powerpacks/search-index/ledger.json`; a completed index refreshes the same
directory instead of creating a new one.

The stage manifest is written to:

```text
.powerpacks/network-import/index/contacts/manifest.json
```

Use the resulting local search DB:

```bash
export POWERPACKS_LOCAL_SEARCH_DB=.powerpacks/search-index/local-search.duckdb
```

Continue a partial run:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py continue --ledger .powerpacks/search-index/ledger.json
```

Status:

```bash
uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py status
```

Artifacts are written under `.powerpacks/search-index/`. The local DuckDB is
`.powerpacks/search-index/local-search.duckdb`.

## Company HQ locations

Local companies are derived from people's work experiences and carry no HQ
location. The company step always backfills the empty
`city`/`state`/`country`/`metro_area`/`macro_region` fields from the local
RapidAPI company payload cache (`.powerpacks/rapidapi-company-cache/`),
joining each company to its RapidAPI company id from work experiences. Raw
LinkedIn codes are normalized through the same path as people locations
("US" → "United States", "CA" → "California", metro/macro derived). This is a
cache-only disk read — no network calls; companies without a cached payload
are left untouched.

## Constraints

- No Modal dispatch or artifact upload.
- `plan`, `status`, and processing `--dry-run` are local inspection only.
- No LLM/provider calls from `plan`, `status`, or processing `--dry-run`.
- A full run may use configured classification and embedding providers for
  cache misses reported by the estimate.
- No Supabase/Postgres calls.
- No TurboPuffer calls.
- All generated people, company, school, position, education-edge, and summary
  IDs are stable UUIDv5 strings.
