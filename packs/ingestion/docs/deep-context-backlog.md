# Deep Context backlog

Created: 2026-08-06

Change log:
- 2026-08-06: initial deferred-work inventory after the SQLite rewrite.

These items are deliberately outside the mechanical D2 cleanup round. They are
not implicit acceptance criteria for that round.

## Deferred items

- Perf: worth CTE as a real `CREATE VIEW`; `eligible_links AS MATERIALIZED`;
  single-row `review_row(db, key)` for the adapter's per-click lookup;
  settlement `links_by_key`; `clear_machine_winner_conflicts` as bulk SQL;
  `workflow_state` currently re-evaluates `WORTH_CTE` 6× per poll.
- Readability polish: `identity_scope` double-negatives → named CTEs;
  `effective_decision` refactor (compute one chosen decision, derive);
  `person_lookup`'s 13 positional params → named SQL params;
  `contact_identifiers` split; cross-module private imports cleanup.
- Parent→children helper: one shared `children_by_parent` home (hand-rolled in
  ~10 sites); `_approved_identities` as one SQL query.
- `owner.json` read-back inversion (file is the record, SQLite the mirror —
  should invert).
- `ReviewExportRow` collapse into a typed decision row (post-batons-deletion).
- Synthetic rows have no delete path when a parent's worth later leaves
  `yes`.
- `.enrich-progress.is-indeterminate` has CSS but no emitter (baseline had an
  agent-handoff indeterminate bar).
- Pyright/mypy in the lint gate (sizing unknown; conventions already require
  annotations).
- `merge_verdicts` stale-row accumulation (unbounded paid-cache growth).
- Guided free-text box auto-applying any pasted LinkedIn URL as a decision —
  review UX question.
- BIG one, own project: network-import speaks SQLite — `people.csv` becomes a
  derived export; Deep Context stage 1 collapses to a join.
- Stage-2 message-entry typing (`MessageEntry` dataclass at the channel
  readers) — REQUIRES a byte-identical bundle-JSON serialization pin
  (`input_evidence_fingerprint` is a paid cache key).

## Owner audit findings (2026-08-07)

Logged during Arthur's read-through; not yet fixed.

### One db door, not two (with a real silent-create hole)

11 stage classes take BOTH `db: Db | None = None` and `db_path: Path = CANONICAL_DB`,
reconcile them (`self.db_path = db.db_path if db is not None else Path(db_path)`),
and then fall back to `Db(self.db_path)` at execute time — which CREATES an empty
database rather than refusing. `open_existing_db()` protection currently lives only
in the 14 CLI `main()` functions, so any in-process construction still silently
creates an empty store and reports "0 rows, completed".

Fix: stage classes take `db: Db` (required); `main()` is the only place a path
becomes a Db, via `open_existing_db`. Delete the `db_path` parameter and the
reconciliation line. Only `check_readiness` (must report on a possibly-absent DB)
and `migrate_sqlite` (the sanctioned creator) stay path-based.

Affected: build_owner, check_readiness*, collect_person_context, compose_dossier,
lookup_person, parallel_research/models, persist_review_identities,
prefetch_profiles, profile_projection, review_web/server, validate_dossiers.
Only 4 mains pass `db_path=` today (build_owner, check_readiness, collect, lookup).
