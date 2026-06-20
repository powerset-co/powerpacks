# Powerpacks pipeline file DAG

The local data pipeline has one canonical file layout under the repo-local
`.powerpacks/` tree. Normal agent/setup runs should use the commands below as-is
and should not invent alternate `--input`, `--output-dir`, `--ledger`, or
`--run-id` paths.

The code source of truth is `packs/ingestion/pipeline_paths.py`. Update that
module first, then update this doc and tests.

## Rules

- Raw/intermediate tabular data stays in CSV for now; index records are JSONL.
- The normal pipeline is one sequential current run, not many competing run
  directories that clobber each other.
- Stage commands may keep hidden/debug path flags for tests or one-off recovery,
  but user/agent-facing setup should use convention-based commands.
- Optional sources no-op by absence: missing Gmail/LinkedIn/Twitter/messages
  artifacts are skipped by merge discovery rather than mutating the DAG.
- `status` and dry-run/plan commands must be read-only.

## Canonical stages

| Stage | Command | Inputs | Outputs |
| --- | --- | --- | --- |
| Setup/onboarding | `uv run --project . python packs/ingestion/primitives/onboarding/onboarding.py run` | user choices | `.powerpacks/ingestion/accounts.json`, `.powerpacks/ingestion/onboarding-run.json` |
| Messages import | `uv run --project . python packs/messages/primitives/import_contacts_pipeline/import_contacts_pipeline.py run` | local Messages/WhatsApp metadata | `.powerpacks/messages/contacts.csv` |
| Gmail import | `uv run --project . python packs/ingestion/primitives/gmail_network_import/gmail_network_import.py run` | Gmail account state | `.powerpacks/network-import/gmail/<run>/people.csv`, `.powerpacks/network-import/gmail/import-run.json` |
| LinkedIn import | `uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py run --csv <Connections.csv> --source-user <label>` | external LinkedIn export | `.powerpacks/network-import/linkedin/<run>/people.csv`, `.powerpacks/network-import/linkedin/import-run.json` |
| Twitter import | `uv run --project . python packs/ingestion/primitives/twitter_network_import/twitter_network_import.py run --handle <handle>` | operator handle | `.powerpacks/network-import/twitter/<run>/people.csv`, `.powerpacks/network-import/twitter/import-run.json` |
| Merge sources | `uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run` | `.powerpacks/network-import/*/*/people.csv`, `.powerpacks/messages/contacts.csv` | `.powerpacks/network-import/merged/people.csv`, `.powerpacks/network-import/merged/review_pairs.csv`, `.powerpacks/network-import/merged/manifest.json` |
| Enrich people | `uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py run` | `.powerpacks/network-import/merged/people.csv` | `.powerpacks/network-import/enrichment/current/people_enriched.csv`, `.powerpacks/network-import/enrichment/import-run.json` |
| Build index records | `uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --force` | enriched people if present, otherwise merged people | `.powerpacks/search-index/current/ledger.json`, `.powerpacks/search-index/current/records/*.records.jsonl` |

## File tree

```text
.powerpacks/
├── ingestion/
│   ├── accounts.json
│   └── onboarding-run.json
├── messages/
│   └── contacts.csv
├── network-import/
│   ├── gmail/
│   │   ├── import-run.json
│   │   └── <run>/people.csv
│   ├── linkedin/
│   │   ├── import-run.json
│   │   └── <run>/people.csv
│   ├── twitter/
│   │   ├── import-run.json
│   │   └── <run>/people.csv
│   ├── merged/
│   │   ├── people.csv
│   │   ├── review_pairs.csv
│   │   └── manifest.json
│   ├── enrichment/
│   │   ├── import-run.json
│   │   └── current/
│   │       ├── linkedin_enrichment_queue.csv
│   │       ├── rapidapi_cache_hits.csv
│   │       ├── rapidapi_cache_misses.csv
│   │       ├── needs_resolution_queue.csv
│   │       ├── skipped_enrichment.csv
│   │       ├── provider_enriched.csv
│   │       ├── people_enriched.csv
│   │       └── raw_provider_responses/
│   └── profile_cache_v2/
└── search-index/
    └── current/
        ├── ledger.json
        ├── stats/*.json
        ├── unified/flattened_people.jsonl
        ├── unified/unified_person.csv
        ├── profiles/hydrated_profiles.jsonl
        ├── roles/raw_titles.jsonl
        ├── roles/role_mapping.csv
        ├── roles/roles_with_dense_text.jsonl
        ├── company/companies_corpus.jsonl
        ├── education/schools_corpus.jsonl
        ├── education/people_education.jsonl
        ├── location/locations_corpus.jsonl
        ├── summaries/summary_records.jsonl
        └── records/
            ├── people.records.jsonl
            ├── companies.records.jsonl
            ├── schools.records.jsonl
            ├── education.records.jsonl
            └── summaries.records.jsonl
```

## DVC-equivalent DAG

There is no `dvc.yaml` dependency yet. The YAML below is a contract sketch, not
a literal DVC file. In particular, enrichment is optional today: the indexer
uses `.powerpacks/network-import/enrichment/current/people_enriched.csv` when it
exists and falls back to `.powerpacks/network-import/merged/people.csv` when it
does not. A future literal DVC implementation must either make enrichment emit a
skipped/no-op `people_enriched.csv` artifact or split `index` into separate
`index-enriched` / `index-merged` stages. It should still be mechanically
rendered from `packs/ingestion/pipeline_paths.py`, not maintained as a second
source of truth.

```yaml
stages:
  merge:
    cmd: uv run --project . python packs/ingestion/primitives/merge_network_sources/merge_network_sources.py run
    deps:
      - .powerpacks/network-import/*/*/people.csv
      - .powerpacks/messages/contacts.csv
    outs:
      - .powerpacks/network-import/merged/people.csv
      - .powerpacks/network-import/merged/review_pairs.csv
      - .powerpacks/network-import/merged/manifest.json
  enrich:
    cmd: uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py run
    deps:
      - .powerpacks/network-import/merged/people.csv
    outs:
      - .powerpacks/network-import/enrichment/current/people_enriched.csv
      - .powerpacks/network-import/enrichment/import-run.json
  index:
    cmd: uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --force
    deps:
      # optional/fallback pair; see note above before turning this into literal DVC
      - .powerpacks/network-import/enrichment/current/people_enriched.csv
      - .powerpacks/network-import/merged/people.csv
    outs:
      - .powerpacks/search-index/current/ledger.json
      - .powerpacks/search-index/current/records/people.records.jsonl
      - .powerpacks/search-index/current/records/companies.records.jsonl
      - .powerpacks/search-index/current/records/schools.records.jsonl
      - .powerpacks/search-index/current/records/education.records.jsonl
      - .powerpacks/search-index/current/records/summaries.records.jsonl
```

## Sanity checks

```bash
# Show canonical index plan without writing files.
uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py plan

# Unit contract for this doc/code path.
uv run --project . python -m unittest tests.test_pipeline_file_paths
```
