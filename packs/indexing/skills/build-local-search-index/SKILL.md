---
name: build-local-search-index
description: Build deterministic local search-index artifacts from the canonical network-import people.csv. Use when the user asks to prepare or inspect a local indexing pipeline without uploads, embeddings, LLM, Postgres, Supabase, or TurboPuffer calls.
---

# Build Local Search Index

Build local indexing artifacts from canonical Powerpacks people data.

## Canonical input

Prefer the canonical merged people CSV from `$discover-contacts`:

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

Run the full local contacts indexing pipeline:

```bash
uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py run --operator-id <operator-id>
```

If the pipeline reports provider work above the approval threshold, rerun with
explicit approval:

```bash
uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py run --operator-id <operator-id> --approve-provider-spend
```

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

## Constraints

- local files only
- `plan`, `status`, and `run --dry-run` are local inspection only
- no LLM/provider calls unless the user explicitly approves the required allow flags
- no network calls unless explicitly approved by the generated plan
- no Supabase/Postgres calls
- no TurboPuffer calls
- all generated people, company, school, position, education-edge, and summary IDs are stable UUIDv5 strings
