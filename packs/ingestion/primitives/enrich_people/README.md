# enrich_people

RapidAPI-only local enrichment flow for shared people schema CSVs.

Input is usually any shared Powerpacks people-schema CSV, for example a merged network import output.

The primitive is self-contained in Powerpacks. It does not import
`network-search-api` code. LinkedIn people are enriched through RapidAPI only.

## Flow

1. `prepare_queue`
   - Reads a shared people schema CSV.
   - Routes rows with LinkedIn URLs/public identifiers and profile gaps to
     `linkedin_enrichment_queue.csv`.
   - Splits queued rows into `rapidapi_cache_hits.csv`,
     `rapidapi_cache_misses.csv`, and `rapidapi_recent_failures.csv` using the
     local profile cache.
   - Routes rows without LinkedIn to `needs_resolution_queue.csv`.
2. `enrich_linkedin`
   - Approval-gates only RapidAPI cache misses.
   - Hydrates cache hits and newly fetched profiles into `provider_enriched.csv`.
   - Saves raw RapidAPI responses locally.
3. `merge_people`
   - Merges RapidAPI profile data back into the original people rows.
   - Writes canonical `people.csv`.

## Commands

```bash
uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py run \
  --input .powerpacks/network-import/merged/people.csv

uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py approve
uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py continue
```

Options:

- `--profile-cache-dir` defaults to `.powerpacks/network-import/profile_cache_v2`
- `--refresh-cache` forces RapidAPI calls even when cache files exist
- `--company-corpus-jsonl` may be repeated to enrich company metadata by RapidAPI company ID or LinkedIn company slug
- `--max-workers` and `--max-rpm` bound RapidAPI parallelism; defaults are 10 workers and 300 RPM
- `--failure-retry-hours` controls how long recent failed lookups are skipped before retry; default is 24h
- `--force` re-enriches rows that look complete
- hidden `--limit` is only for tiny local smoke tests

RapidAPI key lookup order is `RAPIDAPI_LINKEDIN_KEY`, then `RAPIDAPI_KEY`.

## Company identity

RapidAPI work experiences preserve explicit provider/company identity fields:

- `rapidapi_company_id`
- `company_public_identifier`
- `company_linkedin_url`
- `company_key` (`rapidapi:{id}` preferred over `linkedin_company:{slug}`)

`current_company_urn` is a legacy shared-schema field and is not populated from
RapidAPI-only enrichment.

## Cache seeding

To avoid paid RapidAPI calls, seed `--profile-cache-dir` before running. The
cache filename is the sanitized LinkedIn public identifier, for example:

```txt
.powerpacks/network-import/profile_cache_v2/jane-example.json
```

Each cache file must contain:

```json
{
  "fetched_at": "2026-01-01T00:00:00Z",
  "public_identifier": "jane-example",
  "linkedin_url": "https://www.linkedin.com/in/jane-example",
  "raw_response": {"...": "RapidAPI profile payload"},
  "normalized_profile": {"success": true}
}
```

Rows with usable cache entries are written to `rapidapi_cache_hits.csv` and do
not require `RAPIDAPI_*` keys or approval. Cache misses are listed in
`rapidapi_cache_misses.csv` and are approval-gated before any external call.
Failed provider lookups are cached with `last_checked_at`; recent failures are
listed in `rapidapi_recent_failures.csv` and retried only after the TTL.

## Outputs

- `linkedin_enrichment_queue.csv`
- `rapidapi_cache_hits.csv`
- `rapidapi_cache_misses.csv`
- `rapidapi_recent_failures.csv`
- `needs_resolution_queue.csv`
- `skipped_enrichment.csv`
- `provider_enriched.csv`
- `raw_provider_responses/*.json`
- `people.csv` — canonical enriched people schema
