---
name: build-local-search-index
description: Build deterministic local search-index artifacts from the canonical network-import people.csv. Use when the user asks to prepare or inspect a local indexing pipeline without uploads, embeddings, LLM, Postgres, Supabase, or TurboPuffer calls.
---

# Build Local Search Index

Build local indexing artifacts from canonical Powerpacks people data.

## Canonical input

Prefer the merged people CSV from the latest `$import-network` run:

```text
.powerpacks/network-import/network-runs/<run-id>/merged/people.csv
```

If the user explicitly wants the aggregate merge output, use
`.powerpacks/network-import/merged/people.csv` only when it exists and is known
to be current. Do not use legacy merged filenames for indexing.

If the file is missing, create it with `$import-network` or the ingestion merge
primitive:

```bash
uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run
```

## Run locally

Plan:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py plan --input .powerpacks/network-import/network-runs/<run-id>/merged/people.csv --output-dir .powerpacks/search-index --run-id local-run
```

Run:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --input .powerpacks/network-import/network-runs/<run-id>/merged/people.csv --output-dir .powerpacks/search-index --run-id local-run
```

Continue a partial run:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py continue --ledger .powerpacks/search-index/<run-id>/ledger.json
```

Status:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py status --ledger .powerpacks/search-index/<run-id>/ledger.json
```

Artifacts are written under `.powerpacks/search-index/<run-id>/`.

## Constraints

- local files only
- no LLM calls
- no network calls
- no Supabase/Postgres calls
- no TurboPuffer calls
- all generated people, company, school, position, education-edge, and summary IDs are stable UUIDv5 strings
