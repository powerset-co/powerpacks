# Indexing

Powerpacks has two ways to turn a canonical people CSV into the local search
database used by `$search local`.

## Choose the path

| Path | Use it when | Where processing runs | Current input |
| --- | --- | --- | --- |
| Install skill plus Modal | Standard product setup. Import and enrich LinkedIn, merge sources, build the index, and download it. | Profile enrichment and indexing run in Modal; source fan-in and final validation run locally. | LinkedIn `Connections.csv` plus imported sources. |
| `$build-local-search-index` | Develop, inspect, or rebuild from an existing canonical merged CSV without using Modal. | The processing pipeline and DuckDB build run on the local machine. | `.powerpacks/network-import/merged/people.csv`. |
| `upload_powerset` (`$deep-context` step 9) | Push the worth-yes slice of a built index to Powerset — TurboPuffer + Postgres — after the worth refresh. Dry-run by default. | The laptop, with `.env` creds — after a Modal build, `download` has already brought the DuckDB home. | The canonical store's worth and tags + `local-search.duckdb`. |

Read the canonical [LinkedIn and Modal indexing pipeline](docs/linkedin-modal-pipeline.md)
for the product flow, diagrams, data boundaries, shared-cache behavior, and
known limitations.

## School parents

`data/education/school_parents.jsonl` says which school rolls up to which:
"Stanford Graduate School of Business" to Stanford University, a translated or
misspelled name to the main one. `build_education_corpus` applies it, so every
build writes the parent into `canonical_education_id`. Rows already in
TurboPuffer change only when they are published again.

To bring the mapping up to date, run this from a checkout and commit the file:

```bash
uv run python packs/indexing/primitives/update_school_parents/update_school_parents.py
```

It reads `aleph_education_v3` (or `--schools file.jsonl`), adds schools it has
not seen, and rolls name-keyed schools into a school with the same name for free.
It then prints how many schools nobody has reviewed and what reviewing them
costs. `--llm` spends that; `--limit N` tries the largest N first. Each batch is
saved as it finishes, and reviewed rows are never asked again. To correct a row
by hand, edit it and set `"source": "manual"`.

To apply the mapping to published rows without rebuilding them:

```bash
ALEPH_ENV=dev uv run python packs/indexing/primitives/republish_school_parents/republish_school_parents.py
```

Every school keeps its own doc in `aleph_education_v3` under its own name; a
child's doc carries `parent_id` and a parent's `person_count` covers its
children. People rows stay as they are: search finds a parent, then filters
`education_id` on the parent and its children. It prints counts until `--apply`,
which backs the school docs up first. `ALEPH_ENV` picks the family: unset is
prod, `dev` is `_dev`. Correcting the mapping and rerunning undoes a merge.

### Keeping the mapping right

The file is the source of truth: `school_parents.jsonl` decides what the live
docs say, and `school_parent_reviews.jsonl` records why a contested row went the
way it did. A test holds the file to those reviews, so a later edit can't quietly
undo one. The LLM only proposes rows for schools nobody has reviewed, from names
alone, so treat `llm` rows as unconfirmed until they are checked; `manual` rows
are never asked again.

To change a merge, edit the row, set `"source": "manual"`, add a review line with
the reason, then republish to dev, then prod.

A live `school_name` is what clients match on. Before changing one on prod, check
it against the resolver in the last release, not just the code on main:
3.50.0 has no `parent_id` lookup, so taking "(MIT)" off a parent left its search
with 24 of 978 people. Republish writes names exactly as this file has them, so
parent names keep their parentheticals unless someone edits the file.


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
│   ├── flattened_people.jsonl
│   └── summary_embeddings.parquet
├── profiles/
│   └── hydrated_profiles.jsonl
├── roles/
│   ├── raw_titles.jsonl
│   ├── role_mapping.csv
│   ├── roles_with_dense_text.jsonl
│   └── roles_with_embeddings.parquet
├── company/
│   ├── companies_corpus_v3.jsonl
│   └── company_embeddings_v3.parquet
├── education/
│   ├── schools_corpus.jsonl
│   └── people_education.jsonl
├── location/
│   └── locations_corpus.jsonl
├── summaries/
│   └── summary_records.jsonl
├── records/
│   ├── people.records.parquet
│   ├── companies.records.parquet
│   ├── schools.records.parquet
│   ├── education.records.parquet
│   └── summaries.records.parquet
└── stats/
```

The standard Modal download intentionally copies only
`local-search.duckdb` and `manifest.json` to the laptop. After a successful
Modal run, the operator run directory holds the ledger, manifest, statistics,
and DuckDB. Record Parquet is persisted there only when `--persist-artifacts` is
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

## Search database

`$search local`, contact lookup, and local profile inspection use the single
`.powerpacks/search-index/local-search.duckdb` artifact. Fan-in retains contact
and source provenance as CSVs under `.powerpacks/network-import/merged/`; it does
not build a second DuckDB.
