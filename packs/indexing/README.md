# indexing

Local pipeline for turning canonical Powerpacks people CSVs into durable search-index inputs without remote service calls.

## Canonical input

Indexing consumes only `.powerpacks/network-import/merged/people.csv`, produced by:

```bash
uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run
```

## Build local search index artifacts

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py plan \
  --input .powerpacks/network-import/merged/people.csv \
  --output-dir .powerpacks/search-index \
  --run-id local-run

uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run \
  --input .powerpacks/network-import/merged/people.csv \
  --output-dir .powerpacks/search-index \
  --run-id local-run
```

Artifacts are written under:

```text
.powerpacks/search-index/<run-id>/
├── ledger.json
├── index-manifest.json
├── local-search.duckdb
├── local-search.duckdb.sha256
├── unified/
│   ├── flattened_people.jsonl
│   └── unified_person.csv
├── profiles/hydrated_profiles.jsonl
├── roles/
│   ├── raw_titles.jsonl
│   ├── role_mapping.csv
│   └── roles_with_dense_text.jsonl
├── company/companies_corpus.jsonl
├── education/
│   ├── schools_corpus.jsonl
│   └── people_education.jsonl
├── location/locations_corpus.jsonl
├── summaries/summary_records.jsonl
├── records/
│   ├── people.records.jsonl
│   ├── companies.records.jsonl
│   ├── schools.records.jsonl
│   ├── education.records.jsonl
│   └── summaries.records.jsonl
└── stats/*.json
```

After a run is fully ready, the latest validated local DB is also copied to:

```text
.powerpacks/search-index/local-search.duckdb
.powerpacks/search-index/local-search.duckdb.sha256
.powerpacks/search-index/latest-manifest.json
```

`ready` means contract validation passed, the DuckDB opened read-only, all local
search namespaces were probed, and local hydration from `local_profiles` passed.
Unchanged inputs/code/contracts/operator scope reuse an operator-scoped cache
under `.powerpacks/search-index/cache/...`; default cache hits materialize the
same compatibility artifacts as a rebuilt run.

Resume/status commands use the ledger file:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py continue \
  --ledger .powerpacks/search-index/local-run/ledger.json
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py status \
  --ledger .powerpacks/search-index/local-run/ledger.json
```

`status` is read-only and reports the ledger, manifest, and ready state without
adopting artifacts or mutating cache state.

Validate the generated DB/searchability explicitly:

```bash
uv run --project . python packs/indexing/primitives/validate_local_search_index/validate_local_search_index.py run \
  --db .powerpacks/search-index/local-run/local-search.duckdb
uv run --project . python packs/indexing/primitives/validate_index_parity/validate_index_parity.py run \
  --people-csv .powerpacks/network-import/merged/people.csv \
  --run-dir .powerpacks/search-index/local-run \
  --db .powerpacks/search-index/local-run/local-search.duckdb
```

All local indexing code is local-file only: no LLM, Supabase, Postgres, or
TurboPuffer calls. The deterministic local build does not generate embeddings;
vector parity is only checked when a vector-bearing source artifact/index is
explicitly provided.

## Optional Modal acceleration

Powerpacks does not require Modal and does not run it from setup/onboarding. The
wrapper uses lazy Modal imports and packages the repo source into a Modal image
for explicit remote builds. A fixture-sized live smoke has verified packaged
source execution and Modal Volume writes; real `.powerpacks/network-import/merged/people.csv`
data should still be validated separately in the target environment. Inspect the
planned paths first with a transient Modal dependency:

```bash
uv run --with modal --project . python packs/indexing/primitives/modal_index_build/modal_index_build.py plan \
  --input .powerpacks/network-import/merged/people.csv \
  --output-dir .powerpacks/search-index \
  --run-id modal-run \
  --operator-id local:user

# The run subcommand remains opt-in because it uses remote compute and a Modal Volume.
uv run --with modal --project . python packs/indexing/primitives/modal_index_build/modal_index_build.py run \
  --input .powerpacks/network-import/merged/people.csv \
  --output-dir .powerpacks/search-index \
  --run-id modal-run \
  --operator-id local:user \
  --pull-duckdb
```

The Modal wrapper uses lazy imports, no required `pyproject.toml` dependency, an
operator-scoped Volume path, and structured JSON errors/fallback commands when
Modal is unavailable. Full compatibility artifacts stay in the Volume by default;
pulling all JSONL/stats/profile artifacts should be explicit. By default `run`
returns a structured `modal_live_run_unverified` error with the local fallback
command; pass `--allow-unverified-live-run` only when an explicit remote
Modal build/smoke is desired.
