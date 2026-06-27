---
name: build-local-search-index
description: Build deterministic local search-index artifacts from the canonical network-import people.csv. Use when the user asks to prepare or inspect a local indexing pipeline without uploads, embeddings, LLM, Postgres, Supabase, or TurboPuffer calls.
---

# Build Local Search Index

Build local indexing artifacts from canonical Powerpacks people data.

## Canonical input

Use only:

```text
.powerpacks/network-import/merged/people.csv
```

If the file is missing, create it with the ingestion merge primitive:

```bash
uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run
```

Do not use legacy merged filenames for indexing.

## Run locally

Plan:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py plan --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index --run-id local-run
```

Run:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index --run-id local-run
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
The run is `ready` only after contract validation, local DuckDB validation, all
local namespace probes, and local hydration parity pass. A ready run writes:

- `.powerpacks/search-index/<run-id>/index-manifest.json`
- `.powerpacks/search-index/<run-id>/local-search.duckdb`
- `.powerpacks/search-index/<run-id>/local-search.duckdb.sha256`
- stable latest copies under `.powerpacks/search-index/local-search.duckdb` and `.powerpacks/search-index/latest-manifest.json`

Unchanged inputs/code/contracts/operator scope may reuse the operator-scoped
cache under `.powerpacks/search-index/cache/...`. Default cache hits must
materialize the same compatibility artifact set as a rebuilt run.

Validate searchability/parity when reporting success:

```bash
uv run --project . python packs/indexing/primitives/validate_local_search_index/validate_local_search_index.py run --db .powerpacks/search-index/<run-id>/local-search.duckdb
uv run --project . python packs/indexing/primitives/validate_index_parity/validate_index_parity.py run --people-csv .powerpacks/network-import/merged/people.csv --run-dir .powerpacks/search-index/<run-id> --db .powerpacks/search-index/<run-id>/local-search.duckdb
```

For local hydration parity, use `POWERPACKS_LOCAL_SEARCH_DB` or `hydrate_people.py --local-db` so hydration reads `local_profiles` instead of Postgres.

## Optional Modal acceleration

Modal is opt-in only. Do not run Modal from setup/onboarding unless the user explicitly asks. The wrapper is a lazy/static scaffold until an opt-in live Modal smoke verifies image source packaging and Volume behavior. Use a transient optional dependency:

```bash
uv run --with modal --project . python packs/indexing/primitives/modal_index_build/modal_index_build.py plan --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index --run-id modal-run --operator-id local:user
uv run --with modal --project . python packs/indexing/primitives/modal_index_build/modal_index_build.py run --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index --run-id modal-run --operator-id local:user --pull-duckdb
```

The Modal wrapper uses lazy imports and no required `pyproject.toml` dependency. By default `run` returns structured `modal_live_run_unverified` JSON with a local fallback command until a live smoke is explicitly requested with `--allow-unverified-live-run`. Do not claim a live Modal Volume/app or remote execution path was verified unless an explicit live smoke was actually run.

## Constraints

- local files only
- no LLM calls
- no network calls
- no Supabase/Postgres calls
- no TurboPuffer calls
- all generated people, company, school, position, education-edge, and summary IDs are stable UUIDv5 strings
