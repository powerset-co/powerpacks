# indexing

Local pipeline for turning canonical Powerpacks people CSVs into durable search-index inputs without remote service calls.

## Canonical input

Indexing consumes only `.powerpacks/network-import/merged/people.csv`, produced by `$import-network`/setup fan-in. If running the merge primitive directly, pass every source explicitly with `--input`; it never discovers run artifacts.

## Build local search index artifacts

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py plan \
  --input .powerpacks/network-import/merged/people.csv \
  --output-dir .powerpacks/search-index

uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run \
  --input .powerpacks/network-import/merged/people.csv \
  --output-dir .powerpacks/search-index
```

The pipeline has one local index target. A partial `.powerpacks/search-index`
run resumes from its ledger; a completed run refreshes the same directory.

Artifacts are written under:

```text
.powerpacks/search-index/
├── ledger.json
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

Materialize the local DuckDB search backend:

```bash
uv run --project . python scripts/build-local-duckdb-shim.py \
  --records-dir .powerpacks/search-index \
  --force
export POWERPACKS_LOCAL_SEARCH_DB=.powerpacks/search-index/local-search.duckdb
```

Resume/status commands use the ledger file:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py continue \
  --ledger .powerpacks/search-index/ledger.json
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py status \
  --ledger .powerpacks/search-index/ledger.json
```

All indexing code is stdlib-only/local-file only: no LLM, network, Supabase, Postgres, or TurboPuffer calls.
