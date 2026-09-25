# enrich — `enrich_people`, `enrich_prepare_queue`, `enrich_linkedin_profiles`, `enrich_merge_people`

The RapidAPI people-enrichment stage: one store node plus three step nodes that
share this package and are orchestrated by the store.

- `enrich_people` (`EnrichPeople`) — the store: owns the artifact dir, run
  order, spend gate, and the one `manifest.json`.
- `enrich_prepare_queue` (`EnrichQueuePrepare`) — step 1: route rows and split
  LinkedIn-provider rows into cache hits / misses / recent failures.
- `enrich_linkedin_profiles` (`EnrichLinkedInProfiles`) — step 2: hydrate hits
  and fetch misses into `provider_enriched.csv`.
- `enrich_merge_people` (`EnrichedPeopleMerge`) — step 3: merge provider
  profiles back into every input row.

Declared in `pipeline/graph.py`; contract in `pipeline/contract.py`.

| node | reads | writes | manifest |
|---|---|---|---|
| `enrich_people` | `merged/people.csv` (optional) | — | `enrichment/manifest.json` (`EnrichManifest`) |
| `enrich_prepare_queue` | `merged/people.csv` | `enrichment/rapidapi_cache_hits.csv`, `rapidapi_cache_misses.csv`, `rapidapi_recent_failures.csv` (full_rewrite) | none (`manifest = ""`) |
| `enrich_linkedin_profiles` | `rapidapi_cache_hits.csv`, `rapidapi_cache_misses.csv` | `enrichment/provider_enriched.csv` (full_rewrite) | none (`manifest = ""`) |
| `enrich_merge_people` | `merged/people.csv`, `provider_enriched.csv`, `rapidapi_recent_failures.csv` | `enrichment/people.csv` (full_rewrite) | none (`manifest = ""`) |

All paths are under `.powerpacks/network-import/`; the three step nodes report
into the store's single manifest.

## Manifest / status

`EnrichManifest` (store): `completed`, `needs_approval` (paid fetches would be
billed without `--approve-spend`; carries the credit-gate payload), plus the
template `not_ready` / `failed`. The step summaries default to `completed`.
Per-row `enrichment_status` is `enriched` / `failed` / `skipped`.

## Control

Paid — RapidAPI profile fetches. Spend gate `--approve-spend`: the step-1 miss
count IS the estimate, and the store reads it before constructing any client.
Cache hits never need approval. Without approval a run with misses stops at
`needs_approval` before any fetch.

## Invariant

Step 3 keeps every input row, stamped `enriched` / `failed` / `skipped` — a row
the provider could not hydrate stays with its identity columns and the failure
reason, because deleting it made a rate-limited fetch byte-identical to "never
attempted".
