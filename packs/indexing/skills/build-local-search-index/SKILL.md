---
name: build-local-search-index
description: Build deterministic local search-index artifacts from the canonical network-import people.csv. Use when the user asks to prepare or inspect a local indexing pipeline without uploads, embeddings, LLM, Postgres, Supabase, or TurboPuffer calls.
---

# Build Local Search Index

Build local indexing artifacts from canonical Powerpacks people data.

## Canonical input

Prefer the canonical merged people CSV from `$import-network`:

```text
.powerpacks/network-import/merged/people.csv
```

If the aggregate merge output is missing or stale, use the latest
`.powerpacks/network-import/network-runs/*/merged/people.csv` and refresh the
aggregate before indexing. Do not use legacy merged filenames for indexing.

If the file is missing, create it with `$import-network` or the ingestion merge
primitive:

```bash
uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run
```

## Run locally

Plan:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py plan --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index
```

Run:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index
```

The run is single-index and idempotent: a partial index resumes from
`.powerpacks/search-index/ledger.json`; a completed index refreshes the same
directory instead of creating a new one.

Materialize the local search DuckDB:

```bash
uv run --project . python scripts/build-local-duckdb-shim.py --records-dir .powerpacks/search-index --operator-id <operator-id> --force
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
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py status --ledger .powerpacks/search-index/ledger.json
```

Artifacts are written under `.powerpacks/search-index/`. The local DuckDB is
`.powerpacks/search-index/local-search.duckdb`.

## Constraints

- local files only
- no LLM calls
- no network calls
- no Supabase/Postgres calls
- no TurboPuffer calls
- all generated people, company, school, position, education-edge, and summary IDs are stable UUIDv5 strings
