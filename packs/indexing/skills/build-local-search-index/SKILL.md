---
name: build-local-search-index
description: Build deterministic local search-index artifacts from the canonical Powerpacks pipeline files. Use when the user asks to prepare or inspect a local indexing pipeline without uploads, embeddings, LLM, Postgres, Supabase, or TurboPuffer calls.
---

# Build Local Search Index

Build local indexing artifacts from the canonical Powerpacks people data.

The file DAG source of truth is `docs/pipeline-file-dag.md` and
`packs/ingestion/pipeline_paths.py`. Do not invent alternate index input/output
paths.

## Canonical input

The indexer automatically uses:

1. `.powerpacks/network-import/enrichment/current/people_enriched.csv` when present
2. `.powerpacks/network-import/merged/people.csv` otherwise

If both are missing, create the merge output first:

```bash
uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run
```

## Run locally

Plan (read-only):

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py plan
```

Run the canonical current index build:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --force
```

Continue/status use the canonical current ledger by default:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py continue
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py status
```

Artifacts are written under `.powerpacks/search-index/current/`.

## Constraints

- local files only
- no LLM calls
- no network calls
- no Supabase/Postgres calls
- no TurboPuffer calls
- all generated people, company, school, position, education-edge, and summary IDs are stable UUIDv5 strings
