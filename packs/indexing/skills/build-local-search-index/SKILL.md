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

If the aggregate merge output is missing or stale, refresh it with `$import-network`/setup fan-in. If you run the ingestion merge primitive directly, pass each source explicitly with `--input`; it does not discover run artifacts or use legacy merged filenames.

## Run locally

Plan/status inspection and dry-run cost estimates are local-only and safe to
run before asking for provider spend approval:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py plan --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index
```

Estimate processing spend without writing artifacts or calling providers:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --dry-run --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index
```

Run:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index
```

The run is single-index and idempotent: a partial index resumes from
`.powerpacks/search-index/ledger.json`; a completed index refreshes the same
directory instead of creating a new one.

If the dry run reports that real provider stages are needed, do not run them by
default. Continue only when precomputed/restored artifacts are already present
or the user has explicitly approved the relevant provider allow flags/spend and
the reported cost. Do not treat `plan`, `status`, or `run --dry-run` as
approval to call providers.

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
- `plan`, `status`, and `run --dry-run` are local inspection only
- no LLM/provider calls unless the user explicitly approves the required allow flags
- no network calls unless explicitly approved by the generated plan
- no Supabase/Postgres calls
- no TurboPuffer calls
- all generated people, company, school, position, education-edge, and summary IDs are stable UUIDv5 strings
