# LinkedIn to searchable index on Modal (historical onboarding v3 plan)

> **Historical design note, not the current product contract.** See the
> canonical [LinkedIn and Modal indexing pipeline](linkedin-modal-pipeline.md)
> for shipped behavior, data boundaries, limitations, and current commands.
> This file is retained for implementation history.

**Created:** 2026-06-12

**Changelog:**
- 2026-06-12: Initial plan.

## Goal

Drop `Connections.csv` → searchable local DuckDB in minutes. Two visible
stages only: **Importing** → **Indexing**. All compute and all provider keys
live in Modal sandboxes (workspace secrets `powerset-rapidapi`,
`powerset-openai`); the laptop holds only a Modal token, dispatches, shows
progress, and downloads the finished index. RapidAPI is **always approved** —
no spend gates on this path.

## What already exists (reuse, don't rebuild)

- `packs/ingestion/primitives/setup_linkedin_csv/setup_linkedin_csv.py` —
  the onboarding-v2 end-to-end pipeline: inspect → discover → enrich →
  source_people → merge_network → network_duckdb → index_estimate →
  index_records → search_duckdb, with `RunContext.event()` writing atomic
  `status.json` + `events.jsonl` under `.powerpacks/runs/setup-linkedin-csv/`.
- Console v2 page polls `/local-api/onboarding-v2/linkedin/status` every 2s
  and renders `status.stages` + `progress`.
- Modal indexing vertical (merged, PR #54): shared volume layout
  (`cache/` union-merged, `operators/<id>/{input,runs}`), `run_in_sandbox.py`
  orchestrator, `process`/`download` driver, workspace secrets.

## Architecture

```
laptop (console or CLI)                    Modal (powerset-co)
┌──────────────────────────┐               ┌──────────────────────────────────┐
│ onboarding-v3 page       │               │ Sandbox 1: run_linkedin.py       │
│  drop Connections.csv    │── upload ────▶│  parse → RapidAPI enrich (secret │
│  [Process] button        │               │  mounted, auto-approved) → merge │
│                          │               │  → operators/<op>/input/people.csv│
│ linkedin_modal_pipeline  │               │  profile_cache_v2 union → cache/ │
│  .py  (driver)           │               ├──────────────────────────────────┤
│  - dispatch sandboxes    │── dispatch ──▶│ Sandbox 2: run_indexing.py       │
│  - poll volume status    │               │  (existing) people.csv → records │
│  - re-emit v2-format     │               │  → local-search.duckdb           │
│    status.json locally   │◀─ download ───│  cache key-union refresh         │
└──────────────────────────┘               └──────────────────────────────────┘
```

### File layout (renames per direction)

```
packs/indexing/modal/
  sandbox_common.py            status writes, key-union merge, bench helpers
  run_linkedin.py              NEW import sandbox: stages lifted from
                               setup_linkedin_csv.py (inspect/discover/enrich/
                               merge), RapidAPI always-on, auto-approve the
                               primitive's approval block
  run_indexing.py              renamed run_in_sandbox.py (indexing only)
  linkedin_modal_pipeline.py   renamed driver. Commands:
                               pipeline --csv <path>   (the one-shot)
                               import / index / download / status
                               upload --seed-cache, amplify, run (benchmarks)
```

### Progress bridge (sandbox → console)

Sandboxes already write `status.json` to `operators/<op>/runs/<label>/` on the
volume. The local driver polls it (~3s) and re-emits into
`.powerpacks/runs/setup-linkedin-modal/{status.json,events.jsonl}` in the
exact onboarding-v2 schema, collapsed to two stages:

- `importing` ("Importing contacts") — covers sandbox-1 inspect→merge
- `indexing` ("Building search index") — covers sandbox-2 pipeline→duckdb→download

Console v3 stays a dumb poller; no new progress store (CLAUDE.md rule).

### Runtime estimate (shown on the page)

`estimate_seconds = 40 (dispatch+image) + misses/3.3 (RapidAPI @200/min)
+ 0.25 × people + 60 (duckdb+download)`; misses computed instantly by the
prepare-queue scan against the shared profile cache, people = csv row count.
Calibrate constants from the timed test runs below.

### Incremental / no-op semantics (per operator)

- Driver hashes the uploaded csv; if it matches
  `operators/<op>/runs/last-input.sha`, the whole pipeline is a **no-op**
  (instant "already up to date").
- Changed csv: import fetches only profiles missing from the shared
  `cache/profile_cache_v2/` (RapidAPI hits only the delta); indexing
  recomputes records with all enrichment cache-covered (compute-only,
  minutes) and refreshes the shared caches by key-union.
- Operator-scoped: connections.csv, people.csv, runs/, downloaded duckdb.
  Shared: profile_cache_v2, rapidapi-company-cache, all enrichment artifacts.

### Console onboarding-v3 page

Minimal single-purpose page (`/onboarding-v3`): drop zone showing the chosen
file name → one Process button → progress bar with the two stages + ETA from
the estimate. POST `/local-api/onboarding-v3/linkedin/run` spawns
`linkedin_modal_pipeline.py pipeline --csv <path>`; GET `.../status` reads the
local status.json. v2 page stays untouched.

## Test protocol

1. **Half csv**: split a sample Connections.csv in half → `pipeline` e2e
   → query downloaded DuckDB: people count ≈ half, spot-check names.
2. **Full csv**: rerun e2e → count ≈ full; only the delta hit RapidAPI.
3. **Third drop (same csv)**: no-op, near-instant.
4. **Scoping audit**: volume listing shows operator data only under
   `operators/<op>/`, shared caches grew incrementally (union counts logged).
5. **Timing + memory**: bench wrapper already wraps both sandboxes; record
   wall + peak RSS per stage and per sandbox; calibrate the ETA formula.

## Out of scope (this PR)

- Gmail/Messages verticals on Modal (same pattern later).
- True incremental indexing inside the pipeline (input-hash no-op + cache
  replay is the contract for now).
- Replacing onboarding-v2 (v3 is additive).
