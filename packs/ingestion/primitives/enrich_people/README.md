# enrich_people

Unified local enrichment flow for shared people schema CSVs.

Input is usually:

```bash
.powerpacks/network-import/merged/people_harmonic_all.merged.csv
```

The primitive is self-contained in Powerpacks. It does not import `aleph-mvp` or
`network-search-api` code. Provider normalization/merge logic was copied/adapted
into this primitive.

## Flow

1. `prepare_queue`
   - Reads a shared people schema CSV.
   - Routes rows with LinkedIn URLs/public identifiers and profile gaps to
     `linkedin_enrichment_queue.csv`.
   - Routes rows without LinkedIn to `needs_resolution_queue.csv`.
   - Skips already-complete rows.
2. `enrich_linkedin`
   - Approval-gated.
   - Calls Harmonic and/or RapidAPI LinkedIn.
   - Saves raw provider responses locally.
3. `merge_people`
   - Merges provider data back into the original people rows.
   - Writes `people_enriched.csv`.

## Commands

```bash
uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py run \
  --input .powerpacks/network-import/merged/people_harmonic_all.merged.csv

uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py approve
uv run --project . python packs/ingestion/primitives/enrich_people/enrich_people.py continue
```

Options:

- Harmonic and RapidAPI are both enabled by default.
- `--no-harmonic`
- `--no-rapidapi`
- At least one provider must remain enabled when enrichment work is queued.
- `--force` to re-enrich rows that look complete
- hidden `--limit` only for tiny local smoke tests

## Outputs

- `linkedin_enrichment_queue.csv`
- `needs_resolution_queue.csv`
- `skipped_enrichment.csv`
- `provider_enriched.csv`
- `raw_provider_responses/*.json`
- `people_enriched.csv`
