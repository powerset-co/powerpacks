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

### people.csv has two owners; stage 1 has no entry point

`people.csv` is projected into SQLite in TWO places: `import_legacy` (migration,
via `_load_graph` -> `_merged(merged_people_csv)`) and `CollectPersonContext.
execute()` (every run). Neither double-writes wrongly — both are idempotent
get-or-create — but one job has two owners, which is why it reads as duplicated.

It cannot be migration-only: people.csv is a live feed rewritten by Gmail/message/
LinkedIn imports between runs, while migration happens once per install.

Target shape:
- Stage 1 owns it: "project people.csv into SQLite", its own command, before
  collect, every run, idempotent. The spec already lists this stage; the code
  ships it as a step inside collect, so nine stages have eight commands.
- Migration stops owning people: it imports the legacy deep-context artifacts
  (facts, verdicts, decisions, research); stage 1 picks up people next run.
- Collect just reads, and errors when the DB is absent (see the db-door item).

General rule this implies, worth stating in the spec: a stage projects what it
PRODUCES and never projects another stage's input. Enrichers projecting research
results and judgments honour it; collect projecting imported people does not.

Second-order (dies with the above): the call sits behind
`if self.people_csv is not None`, but the CLI argument defaults to
DEFAULT_PEOPLE_CSV and main() always passes it — so via the CLI the projection
always runs and the None branch is a skip-stage-1 mode only direct construction
can reach.

### ContextSources is assembled from outside (half-done construct-and-run)

`CollectPersonContext.execute()` reaches into the ContextSources instance and
drives it: `store = self.sources.store`, `self.sources.accounts.clear()`,
`self.sources.gmail_available = False`, then `store.connect()`,
`store.require_schema()`, `self.sources.accounts.update(store.account_emails())`,
`self.sources.gmail_available = True`. It also probes chat.db readability and
prints the Full Disk Access warning itself.

So D2 moved the reading methods into the class but left the wiring in the caller.
Consequence, same class as the people_csv hidden mode: `gmail_available` starts
False and is set only by that one caller, so a ContextSources constructed
anywhere else silently reads ZERO Gmail and reports success.

Fix: ContextSources owns its own readiness — open the store, validate schema,
discover accounts, set availability, probe chat.db — in one method returning a
typed readiness result the driver merely reports. No `sources.store`
reach-through, no external field assignment. Source-availability warnings belong
to the source object, not the stage driver.

### Source channels are untyped, and their constant set is rebuilt per call

`collection/state.py: source_parents` builds `message_channels =
{GMAIL_CHANNEL, IMESSAGE_CHANNEL, WHATSAPP_CHANNEL}` inside the function on every
call. It is a constant: hoist to a module-level `MESSAGE_CHANNELS` in caps, named
for its intent (the people we can actually read messages for). It is NOT redundant
— on the owner's real install the source values are gmail_msgvault 519, imessage
102, linkedin_csv 88, whatsapp 26, so this set is what excludes the 88
LinkedIn-only people from collection.

Underlying inconsistency: GMAIL_CHANNEL / IMESSAGE_CHANNEL / WHATSAPP_CHANNEL are
loose string constants in common.py while every sibling concept (RowKind,
IdentifierKind, ArtifactKind) is a StrEnum in db/models.py. Make a SourceChannel
StrEnum and derive MESSAGE_CHANNELS from it.

Also in the same function: `sources: dict[str, list[str]]` and `identifiers:
dict[str, dict[str, list[str]]]` are annotated but meaningless — nothing says
person_id -> identifier_kind -> values. With SourceChannel/IdentifierKind as key
types this reads itself. And the function as a whole is a hand-rolled GROUP BY
over a full snapshot (group sources by person, identifiers by person and kind,
join people, drop owners, regroup by parent) — the same shape flagged in ~10 other
sites; a SQL query or one shared grouping helper replaces it.

### Stringly-typed attribute access defeats the dataclasses

`collection/state.py: union_bundles` defines a closure `strings(field: str)` that
does `getattr(bundle, field)`, called as `strings("emails")`, `strings("phones")`,
`strings("source_channels")`, `strings("groups")`. Four literal field names
resolved at runtime against a frozen dataclass — rename a CollectionBundle field
and this breaks at runtime with no type-checker signal, which is precisely what
the typed-rows work was meant to prevent.

Fix: pass values, not field names — a helper over `Iterable[str]` groups, called
as `_merged(b.emails for b in source)`. The name can then say what it does
(merge + dedupe + sort a string field across bundles) instead of `strings`.

### Stage 2 is split across two locations, and "state.py" names nothing

The real axis is sound: `context_sources.py` reads the outside world (msgvault,
chat.db, wacli -> MessageEntry); `collection/state.py` reads our own record and
makes the decisions (who to collect from the snapshot, what is already cached,
skip-or-recollect, group purge, bundle assembly/merge).

Three problems with how that is expressed:
- `state.py` is a meaningless name for plan-and-assemble. Rename (planning.py) or
  split selection (who/what) from bundle assembly (build/union).
- Stage 2 files live in two places with no rule: collection/{models,
  normalization,state}.py inside the subpackage, collect_person_context.py,
  context_sources.py, email_context.py outside it. Per the repo's per-stage
  subpackage rule all six belong under collection/.
- The boundary leaks: union_bundles/build_bundle sit in state.py but assemble the
  OUTPUT of context_sources' reads; probe_chat_db sits in context_sources but
  exists only to feed the driver's readiness warning, returning dict[str, object].
