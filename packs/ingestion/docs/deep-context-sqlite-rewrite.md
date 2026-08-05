# Deep-context ground-up rewrite: one sqlite store, no additive paths

Created: 2026-08-05

Changelog:
- 2026-08-05: initial spec (owner directive: full rewrite, keep only boundary
  files; "every view, every piece of logic in deep context operates on
  sqlite"; no incremental dual-path migration).

## Rule zero: this is a single-user local tool

One user, one local webserver, one command at a time. There are NO concurrent
writers, no multi-process coordination, no recovery protocols. Consequences:

- **The db is the record; the CSVs are re-derivable exports.** Any staleness
  or truncation question is answered by re-exporting (db → baton) or
  re-importing (baton → db) — never by flags, pending markers, or refusal
  ceremonies. The `pending_export` apparatus, `recover_pending_export`, the
  schema-drop guard, and explicit BEGIN IMMEDIATE choreography do NOT carry
  into the rewrite; sqlite's default single-writer transaction is already
  ACID and the built-in busy timeout covers a freak overlap.
- **No locks anywhere**: no flock, no `ensure_no_review_session`, no session
  contracts, no mtime sniffing. Atomic temp+rename on file writes is the one
  crash protection that stays (it is how files are written, not a guard).
- A guard may be added only for an operation a single local user actually
  performs. "Two processes racing" is not one of them.

## Non-negotiables

1. **The truths stay.** Paid/LLM artifacts keep their formats and paths:
   Parallel research outputs, `facts/*.jsonl` dossier checkpoints,
   `dossiers/*.md`, the RapidAPI profile cache. Nothing paid is regenerated,
   moved, or re-keyed.
2. **Boundary files stay.** Inputs read from other stages and outputs other
   stages read keep their formats: `merged/people.csv` (in),
   `review.csv` + `synthetic-people.csv` (export batons out — written FROM
   the db, never read as state), directory.csv contributions via
   persist_review_identities (out), stage `manifest.json`s (out).
3. **Everything between is ONE sqlite store** —
   `.powerpacks/deep-context/deep-context.sqlite`. Every view is a query.
   Every write is a row upsert in a transaction. No file/JSON re-derivation
   anywhere inside the stage. No CSV loaded as state anywhere inside the
   stage.
4. **No additive paths.** Old code is deleted the same commit its replacement
   lands. No `write=` seams, no fallback branches, no "both worlds" states.
   A file that survives does so because it IS the new design.

## The store (all stage state)

| Table | Replaces | Notes |
|---|---|---|
| `people(person_id PK, parent_id, child_slug, parent_slug)` | index.json parents/slugs | already shipped (v3) |
| `parents(parent_id PK, public_identifier, worth_person_ids, llm_worth…)` | parent-worth rows | shipped |
| `links(row_key PK, kind, person_id, url, proposal…, judge fields…)` | identity rows | shipped |
| `decisions(kind+target PK, value, approved, source, note, decided_at)` | approved/network_worth/gates | shipped; absence = pending |
| `verdicts(candidate_key PK, parent_slug, verdict, confidence, reason, fingerprint, judged_at)` | verdicts.jsonl | judge writes rows; jsonl becomes export-only if any external reader exists, else dies |
| `synthetic_profiles(pub PK, …all profile columns…)` | synthetic-people.csv columns | csv becomes export baton |
| `facts(person_id PK, path, mtime_ns, llm_worth, llm_worth_reason)` | facts-dir globbing + worth re-parse | references the paid files; worth mirrored once at write |
| `research(handle PK, dir_path, status, fingerprint)` | per-handle research dir scans | references paid outputs |
| `guidance(handle PK, person_id, guidance, state, submitted_at, applied_url, detail)` | the MEMORY-ONLY retarget queue | durable — kills the "history vanished on restart" class Jake hit |
| `meta` | — | schema version, baton stats, pending_export |

Write rules: `upsert_decision` / `upsert_link` / `upsert_verdict` etc. —
single-row upserts inside one `BEGIN IMMEDIATE` transaction per user action;
the full derive-and-replace exists ONLY at the import boundary (absorbing an
externally-produced baton). Typed rows (`ReviewDecision` etc.) at every
caller boundary; a `dict[str, dict[str, str]]` appearing outside the
import/export modules is a review-rejection.

Read rules: named views/queries in one module (`db/views.py`): worth_queue,
linkedin_queue, siblings_of, parent_state, stage_progress, directory_pane.
The server renders query results; it holds NO memory-authoritative model, so
the flock, `refresh_parents_from_disk`, `cached_parents`, and the boot scrubs
have nothing left to guard and are deleted.

## Fate of every current file (deep_context/)

| File (LOC) | Fate |
|---|---|
| review_db.py (760) | SPLIT → `db/schema.py`, `db/store.py`, `db/views.py`, `db/batons.py` (import/export); gains upsert primitives; loses apply_rows-as-click-path |
| review_store.py (420) | SHRINKS → enums move to db/schema; CSV loader/writer live in db/batons; thresholds/predicates move to policy |
| review_web/model.py (1200+) | DELETED → views + a thin render-model adapter |
| review_web/server.py (1650) | REWRITE → HTTP + jobs only; reads views, writes upserts; no model cache |
| review_web/decisions.py (310) | REWRITE → ~100: typed upserts |
| review_web/retarget_queue.py (600) | REWRITE → guidance table states; blank-and-restore deleted (transaction) |
| review_web/rendering.py | KEEP (pure render) minus queue derivation (moves to views) |
| reconcile_linkedin.py (2007) | SPLIT → `reconcile/judge.py`, `reconcile/policy.py`, `reconcile/run.py`; store-half becomes upserts (~300 LOC die) |
| reconcile_deep_research.py | REWRITE store-half onto upserts; engine untouched |
| worth_view.py (503) | DELETED → one view query + render loop (~120) |
| heal_review.py (530) | REWRITE → selection/termination as queries+upserts (~250) |
| common/legacy.py scrubs (280) | DELETED → one dated import-time normalizer in db/batons |
| flock (common.py, 35) | DELETED |
| apply_retargets / persist_review_identities / restart_review | REWRITE store-half onto queries/upserts; baton writes unchanged |
| synthesize_person_context | facts-table upsert after each dossier write; engine untouched |
| migrate_legacy_resolutions.py | DELETED (its job is the import normalizer) |
| tests/test_deep_context.py (8257) | DELETED whole; new suite by stage from the invariant catalog, ~150 tests + `review_db audit` invariant queries + snapshot-corpus sweep |

## Gates

- Baton fidelity: export(import(baton)) byte-identical on both real stores +
  the 47-snapshot corpus (already proven machinery, kept).
- Behavior: golden queries on the jake mirror — queue contents, sibling sets,
  stage progress — equal between old code and new views before old code is
  deleted; every diff explained.
- Click latency: one decision = one transaction, measured < 10ms on the 17k
  mirror (vs 733ms today).
- End state: deep-context prod LOC and test count both BELOW main's current
  numbers. If not, the rewrite failed its own bar.

## Language

Python + stdlib sqlite3. Go would mean rewriting the LLM clients, the
pipeline contract, the adapters, and the skill surface for zero leverage on
the actual problem (state management), and losing the uv env every agent
harness here already speaks. The disease was never Python; it was files
pretending to be a database.
