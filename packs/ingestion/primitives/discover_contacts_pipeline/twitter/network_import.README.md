# twitter/network_import.py

Powerpacks-local Twitter/X network import orchestrator. This ports the legacy Twitter pipeline into the local `run` / `approve` / `continue` workflow style.

All artifacts stay under `.powerpacks/network-import/discover/twitter/<handle>/`. The primitive does not write to Postgres and does not use local CSV input for production ingestion: Twitter source data comes from RapidAPI.

## Pipeline

1. `load_or_crawl` — RapidAPI Twitter/X follower crawl.
2. `score_candidates` — local heuristic enrichment score.
3. `moe_evaluate` — OpenAI mixture-of-experts triage using the legacy expert lenses.
4. `pre_resolve_linkedin` — free parallel LinkedIn URL extraction from bio, website, and link aggregators.
5. `validate_linkedin` — parallel RapidAPI LinkedIn validation/enrichment.
6. `format_people` — canonical `people.csv` output plus temporary legacy `people_harmonic_all.csv` alias.

## Cost / approval gates

External API spend is gated:

1. `load_or_crawl` — RapidAPI Twitter/X follower crawl.
2. `moe_evaluate` — OpenAI MOE expert evaluation.
3. `validate_linkedin` — RapidAPI LinkedIn validation for pre-resolved LinkedIn URLs.

A hidden internal row cap exists for tiny local smoke tests only. Do not use row caps as part of real workflows.

## Commands

```bash
# Create a run; stops before the first RapidAPI call.
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/twitter/network_import.py run \
  --handle mytechceoo \
  --max-pages 5

# Approve current blocked spend-bearing step.
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/twitter/network_import.py approve

# Continue until completed or the next spend-bearing gate.
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/twitter/network_import.py continue

# Show ledger / artifact status.
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/twitter/network_import.py status

# Check local key presence without printing values.
uv run --project . python packs/ingestion/primitives/discover_contacts_pipeline/twitter/network_import.py check-keys
```

## Environment

- `RAPIDAPI_TWITTER_KEY` preferred for Twitter/X; falls back to `RAPIDAPI_KEY`.
- `RAPIDAPI_LINKEDIN_KEY` preferred for LinkedIn validation; falls back to `RAPIDAPI_KEY`.
- `OPENAI_API_KEY` for MOE evaluation.

## Outputs

- `followers_dump.csv` — follower profile rows from RapidAPI Twitter.
- `candidates.csv` — scored candidates using local heuristics.
- `moe_evaluated.csv` — MOE verdicts, composite scores, top expert, and raw expert signals.
- `linkedin_resolved.csv` — free parallel pre-resolution of LinkedIn URLs from bio/website/link aggregators.
- `linkedin_resolution_queue.csv` — remaining candidates that need a later lookup/search pass.
- `linkedin_validated.csv` — parallel RapidAPI LinkedIn validation results when approved.
- `people.csv` — canonical downstream-compatible person shape with Twitter handle and provider raw JSON columns.
- `people_harmonic_all.csv` — temporary compatibility alias.
- `raw_twitter_responses/`, `raw_linkedin_responses/` — local raw response cache for audit/debug.
