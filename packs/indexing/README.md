# indexing

Local pipeline for turning canonical Powerpacks people CSVs into durable search-index inputs without remote service calls.

The file DAG source of truth is `docs/pipeline-file-dag.md` and
`packs/ingestion/pipeline_paths.py`.

## Canonical input

Indexing automatically consumes `.powerpacks/network-import/enrichment/current/people_enriched.csv` when present, otherwise `.powerpacks/network-import/merged/people.csv`.

Create the merge output with:

```bash
uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run
```

## Build local search index artifacts

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py plan
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --force
```

Artifacts are written under:

```text
.powerpacks/search-index/current/
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

Resume/status commands use the canonical current ledger by default:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py continue
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py status
```

All indexing code is stdlib-only/local-file only: no LLM, network, Supabase, Postgres, or TurboPuffer calls.
