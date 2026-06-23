# linkedin_network_import

Powerpacks-local LinkedIn Connections.csv import.

This primitive is now source-specific: it parses LinkedIn's `Connections.csv`
export into Powerpacks' shared people schema, then delegates LinkedIn profile
enrichment to `packs/ingestion/primitives/enrich_people`.

All artifacts stay under `.powerpacks/network-import/discover/linkedin/` by
default. No database writes or uploads occur.

## Flow

```bash
uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py run \
  --csv ~/Downloads/Connections.csv \
  --source-user operator-a \
  --operator-id local
```

`run` always performs the local CSV conversion first. Usable RapidAPI cache
entries hydrate without API keys. Cache misses run immediately when
`RAPIDAPI_LINKEDIN_KEY` or `RAPIDAPI_KEY` is present; if no key is available,
the run fails with a missing-key error instead of opening an approval step.

## Output contract

Canonical run-level artifacts:

- `connections_for_enrichment.csv` — parsed/deduped source rows from LinkedIn.
- `source_people.csv` — source-only rows in `packs/ingestion/schemas/people_schema.py` shape.
- `linkedin_enrichment_queue.csv` — rows with LinkedIn identifiers that need enrichment.
- `rapidapi_cache_hits.csv` — rows hydrated from local cache; no spend.
- `rapidapi_cache_misses.csv` — rows that require live RapidAPI fetches.
- `rapidapi_recent_failures.csv` — recently failed provider lookups skipped until retry TTL.
- `needs_resolution_queue.csv` — rows without LinkedIn identifiers, if any.
- `provider_enriched.csv` — RapidAPI profile payload/status columns from `enrich_people`.
- `raw_provider_responses/*.json` — local audit/debug payloads.
- `people.csv` — canonical enriched people-schema output.
- `enrich_people.ledger.json` — delegated enrichment ledger.

The top-level LinkedIn ledger exposes these paths in `artifacts`, including
`people_csv` as the canonical interface.

## Cache seeding to reduce RapidAPI fetches

Use the shared `enrich_people` cache format. By default, cache files are read
from `.powerpacks/network-import/profile_cache_v2`; override with
`--profile-cache-dir`.

For LinkedIn URL `https://www.linkedin.com/in/jane-example`, seed:

```txt
.powerpacks/network-import/profile_cache_v2/jane-example.json
```

with:

```json
{
  "fetched_at": "2026-01-01T00:00:00Z",
  "public_identifier": "jane-example",
  "linkedin_url": "https://www.linkedin.com/in/jane-example",
  "raw_response": {"full_name": "Jane Example", "experiences": []},
  "normalized_profile": {"success": true}
}
```

Usable cache entries are counted in `cache_hit_count`; cache misses are counted
in `paid_call_count` for historical ledger compatibility and run as live
RapidAPI fetches when a key is configured.
Failed provider lookups are cached with `last_checked_at`; recent failures are
not retried until `--failure-retry-hours` elapses.

## Keys

```bash
uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py check-keys
```

Expected env keys for live enrichment:

- `RAPIDAPI_LINKEDIN_KEY` or `RAPIDAPI_KEY`

Harmonic is no longer called by this source importer. Provider enrichment is
centralized in `enrich_people` and is currently RapidAPI-only.
