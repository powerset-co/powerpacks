# Indexing

## Job-description text

`lib/job_descriptions.py` owns text normalization and reviewed model edits.
`clean_description()` normalizes HTML/whitespace. `focused_description()` applies
exact deletion quotes from a model plan bound to the SHA256 of that full normalized
source. No handwritten content-removal rules remain. Without a plan it preserves
normalized content (subject to the existing 24,000-character limit); it does not
silently call a model or fall back to semantic heuristics.

Use `packs/search/prompts/jd-cleaning.txt` on each full normalized JD. Review the
model's proposed deletions: source validation cannot determine whether a real
requirement was removed. Preserve original source and cache the approved result
once per JD, reprocessing when the source or cleaning policy changes.

Preview a dataset's `jobs` (each with `jd_id`, `title`, and `text` or `original_text`):

```bash
uv run --project . python -m scripts.review_jd_cleaning \
  --dataset /path/to/dataset.json --output-dir /path/to/private/review \
  --edits /path/to/reviewed-edits.json
```

The edits JSON list must cover every JD exactly once with `jd_id`, `source_sha256`,
and `remove_quotes` (exact strings, possibly empty). Hash the full output of
`clean_description(original_text)`, not a previously filtered or edited version.
Plans based on the former regex output must be regenerated. Stale, ambiguous,
overlapping or nonexistent quotes and wholly empty results are rejected.

This writes `outputs.json`, `review.md`, and `manifest.json` with before/after text
and source/code/edits hashes. Omitting `--edits` previews normalization only.
Model proposals, review decisions and real-data previews stay outside Git.
Synthetic tests check source integrity and application of explicit plans; they
are not evidence of model judgment quality.

Existing `retrieval_text()`, `job_description_record()` and search callers do not
invoke a model: callers must supply an approved cleaned JD to use semantic
cleanup. Automatic model execution is not integrated by this change. Keep the
raw posting separately. Frozen CE inputs require a new input version and
same-runtime baseline before adopting cleaned text. This helper does not clean
pond queries or candidate profiles.

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
├── job-descriptions/
│   └── job_descriptions.jsonl
├── records/
│   ├── people.records.parquet
│   ├── companies.records.parquet
│   ├── schools.records.parquet
│   ├── education.records.parquet
│   ├── summaries.records.parquet
│   ├── job_descriptions.records.parquet
│   └── job_description_positions.records.parquet
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
  --operator-id <operator-id> \
  --jobs-db <monitoring-listings.duckdb>
```

The local runner first calculates an incremental estimate. Cache-covered work
replays locally. Uncovered role/company classification or embedding work can
use configured providers; it does not use Postgres, Supabase, TurboPuffer, or
Modal. Review the estimate and environment before starting a full run when
provider calls are possible.

`--jobs-jsonl` accepts monitoring exports instead of a DuckDB. Add
`--job-description-embeddings <parquet>` to attach precomputed vectors. JD
similarity retrieval requires vectors; canonical tech-skill metadata remains
available without them. Full descriptions are retained separately from the
focused text used for embedding.

Position/JD links use title overlap only for known companies with 1–100 employees,
at a weak ranking weight. Larger companies and unknown headcounts require reviewed
overlap with the person's original position description. Missing descriptions do
not inherit a company's advertised role or skills.

Prepare reviewed links from the same records (default is a free estimate):

```bash
uv run --project . python packs/indexing/primitives/match_job_description_positions/match_job_description_positions.py \
  --jobs .powerpacks/search-index/records/job_descriptions.records.parquet \
  --positions .powerpacks/search-index/records/people.records.parquet \
  --output-dir .powerpacks/search-index/job-descriptions/position-matches
```

After approving its estimate, add `--allow-paid --max-cost-usd <budget>`; test with
`--limit 1` first. This embeds only original position title/description, retrieves
five distinct same-company/date-compatible JDs, and checks specific work overlap
with quoted source evidence. Cached JD vectors must match `text-embedding-3-small`;
a paid control embedding verifies that before review. Paid embeddings and reviews
are retained for reruns, including usage. No company-wide profile text is embedded.

Pass its `work-matches.jsonl` through `--job-description-work-matches` on the contacts
indexer, or `--work-matches` on `build_job_description_evidence.py`. Both DuckDB and
TurboPuffer use the resulting identical mapping records. Unreviewed large-company
links are omitted, not silently restored from title matches. The weights (0.35 for
title-only, 0.8 for supported work, with date decay) are ranking weights, not calibrated
probabilities. Semantic review is still inference, not verified personal skills.

With an incoming JD, both backends select eligible positions before retrieving
similar JDs. Shared ranking takes each person's strongest similarity multiplied
by mapping score, then combines the resulting people with ordinary search using
RRF (ordinary search 1.0, JD evidence 0.7). Without a JD, that vertical is skipped.

Publishing the same records to the remote search stores is explicit:

```bash
uv run --project . python packs/indexing/primitives/publish_job_description_evidence/publish_job_description_evidence.py \
  --records-dir .powerpacks/search-index \
  --dry-run
```

Remove `--dry-run` only for an approved TurboPuffer/Postgres release.
Publishing upserts individual JD/position links and unions existing operator
access, so a partial network does not remove another person's links. Reruns
update supplied link scores; omitted links are retained. Run one publisher at
a time because operator access is read and merged before writing.

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
