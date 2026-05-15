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
   - Splits queued rows into `rapidapi_cache_hits.csv` and
     `rapidapi_cache_misses.csv` using the local profile cache.
   - Routes rows without LinkedIn to `needs_resolution_queue.csv`.
2. `enrich_linkedin`
   - Approval-gates only RapidAPI cache misses.
   - Hydrates cache hits and newly fetched profiles into `provider_enriched.csv`.
   - Saves raw RapidAPI responses locally.
3. `merge_people`
   - Merges RapidAPI profile data back into the original people rows.
   - Writes `people_enriched.csv`.

## Commands

```bash
uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py run \
  --input .powerpacks/network-import/merged/people.merged.csv

uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py approve
uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py continue
```

Options:

- `--profile-cache-dir` defaults to `.powerpacks/network-import/profile_cache_v2`
- `--refresh-cache` forces RapidAPI calls even when cache files exist
- `--company-corpus-jsonl` may be repeated to enrich company metadata by RapidAPI company ID or LinkedIn company slug
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

## Outputs

- `linkedin_enrichment_queue.csv`
- `rapidapi_cache_hits.csv`
- `rapidapi_cache_misses.csv`
- `needs_resolution_queue.csv`
- `skipped_enrichment.csv`
- `provider_enriched.csv`
- `raw_provider_responses/*.json`
- `people_enriched.csv`
