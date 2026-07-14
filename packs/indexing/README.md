# Indexing

Powerpacks has two ways to turn a canonical people CSV into the local search
database used by `$search local`.

## Choose the path

| Path | Use it when | Where processing runs | Current input |
| --- | --- | --- | --- |
| `$setup` plus Modal | Standard product setup. Import and enrich LinkedIn, merge sources, build the index, and download it. | Profile enrichment and indexing run in Modal; source fan-in and final validation run locally. | LinkedIn `Connections.csv`. |
| `$build-local-search-index` | Develop, inspect, or rebuild from an existing canonical merged CSV without using Modal. | The processing pipeline and DuckDB build run on the local machine. | `.powerpacks/network-import/merged/people.csv`. |

Read the canonical [LinkedIn and Modal indexing pipeline](docs/linkedin-modal-pipeline.md)
for the product flow, diagrams, data boundaries, shared-cache behavior, and
known limitations.

## Shared output contract

Both paths materialize the fixed local target:

```text
.powerpacks/search-index/local-search.duckdb
```

Local execution of the processing pipeline produces resumable and inspectable
artifacts:

```text
.powerpacks/search-index/
├── ledger.json
├── manifest.json
├── local-search.duckdb
├── unified/
│   └── flattened_people.jsonl
├── profiles/
│   └── hydrated_profiles.jsonl
├── roles/
│   ├── raw_titles.jsonl
│   ├── role_mapping.csv
│   └── roles_with_dense_text.jsonl
├── company/
│   └── companies_corpus_v3.jsonl
├── education/
│   ├── schools_corpus.jsonl
│   └── people_education.jsonl
├── location/
│   └── locations_corpus.jsonl
├── summaries/
│   └── summary_records.jsonl
├── records/
│   ├── people.records.jsonl
│   ├── companies.records.jsonl
│   ├── schools.records.jsonl
│   ├── education.records.jsonl
│   └── summaries.records.jsonl
└── stats/
```

The standard Modal download intentionally copies only
`local-search.duckdb` and `manifest.json` to the laptop. After a successful
Modal run, the operator run directory holds the ledger, manifest, statistics,
and DuckDB. Record JSONL is persisted there only when `--persist-artifacts` is
selected. Reusable enrichment caches live separately under the shared
`/data/cache/` prefix. The sandbox's intermediate processing tree is ephemeral
and is not a durable Modal artifact.

## Local execution

Plan inspection does not run the pipeline or make provider calls:

```bash
uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py plan \
  --operator-id <operator-id>
```

Inspect the incremental processing estimate:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run \
  --dry-run \
  --input .powerpacks/network-import/merged/people.csv \
  --output-dir .powerpacks/search-index \
  --default-operator-id <operator-id>
```

If the estimate includes provider calls or non-zero cost, obtain explicit
approval. Then run fan-in, processing, and DuckDB materialization locally:

```bash
uv run --project . python packs/indexing/primitives/index_contacts_pipeline/index_contacts_pipeline.py run \
  --operator-id <operator-id>
```

The local runner first calculates an incremental estimate. Cache-covered work
replays locally. Uncovered role/company classification or embedding work can
use configured providers; it does not use Postgres, Supabase, TurboPuffer, or
Modal. Review the estimate and environment before starting a full run when
provider calls are possible.

Resume or inspect a partial processing run through its ledger:

```bash
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py continue \
  --ledger .powerpacks/search-index/ledger.json

uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py status \
  --ledger .powerpacks/search-index/ledger.json
```

Use the result explicitly when needed:

```bash
export POWERPACKS_LOCAL_SEARCH_DB=.powerpacks/search-index/local-search.duckdb
```

## Important distinction

`.powerpacks/network-import/duckdb/network.duckdb` is a contact/source lookup
database built during fan-in. It is not the search index. `$search local` reads
`.powerpacks/search-index/local-search.duckdb`.
