# linkedin_network_import

Powerpacks-local LinkedIn Connections.csv import.

Ports the old LinkedIn pipeline into a stdlib-only primitive that writes all
artifacts under `.powerpacks/network-import/linkedin/<run-id>/`.

## Flow

```bash
uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py run \
  --csv ~/Downloads/Connections.csv \
  --source-user arthur \
  --operator-id local
```

`run` parses the CSV and blocks before paid external enrichment. A hidden row cap exists only for tiny local smoke tests; do not use caps in real workflows. If approved:

```bash
uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py approve
uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py continue
```

## Outputs

- `connections_for_enrichment.csv` — parsed LinkedIn export rows
- `provider_enriched.csv` — Harmonic + RapidAPI status/raw response columns
- `raw_provider_responses/*.json` — per-profile raw API payloads
- `people_harmonic_all.csv` — pipeline-compatible merged output

## Merge rule

Harmonic and RapidAPI raw responses are both retained. For `work_experiences`
and `education`, the merged row chooses the provider with the larger structured
profile (`len(experience) + len(education)`) because Harmonic is sometimes less
complete than RapidAPI.

## Keys

```bash
uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py check-keys
```

Expected env keys for live enrichment:

- `HARMONIC_API_KEY`
- `RAPIDAPI_KEY` or `RAPIDAPI_LINKEDIN_KEY`

Live enrichment is spend-bearing and approval-gated.
