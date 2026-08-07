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

Findings are tracked per file — the owner is reviewing file by file, so each entry
below names the files it touches and fixes are scoped to those files.

| File | Findings |
|---|---|
| `collect_person_context.py` | dual db door; projects another stage's input; assembles ContextSources from outside |
| `collection/state.py` | `getattr`-by-string; MESSAGE_CHANNELS rebuilt per call; meaningless nested dict types; hand-rolled GROUP BY; name says nothing |
| `context_sources.py` | does not own its readiness; `probe_chat_db` returns `dict[str, object]`; lives outside `collection/` |
| `email_context.py` | lives outside `collection/` |
| `synthesis/selection.py` | owner treated as optional; dead `no_owner` flag; `_snapshot` performance seam |
| `db/models.py`, `db/snapshots.py` | `CanonicalSnapshot`/`IdentitySnapshot` as whole-DB dumps (headline) |
| `common.py` | source channels as loose strings instead of a StrEnum |
| 11 stage classes | dual `db`/`db_path` door with silent-create fallback |
| package-wide | 50 snapshot hydration sites; thin intent comments |

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

### Do NOT drop the people projection relying on migration

Tempting shortcut, and it silently breaks ingestion: `import_legacy` runs once per
install, while `merged/people.csv` is rewritten by the fan-in (`imports/
merge_people.py`) after EVERY `$import-gmail`, `$import-messages`, or LinkedIn
re-import. Remove the projection from collect and lean on migration, and any
person imported after migration never enters SQLite — the roster freezes at
migration time, collect never sees them, and nothing errors.

Correct sequencing: remove the projection AND the `people_csv` parameter from
collect, and have stage 1 own it as its own command run before collect on every
run. Migration then keeps only the legacy deep-context artifacts (facts, verdicts,
decisions, research) and stops touching people at all.

### Inline comments should state intent, not narrate

Density is too low across the package: several load-bearing decisions (the resume/
skip predicate, identity_scope's nested negations, the group-purge policy, the
worth CTE's owner exclusion) carry no comment saying WHY the rule is what it is.
Add intent comments at each non-obvious decision — both to help a reader and as a
check that the author actually understood the rule. Comments explain constraints
and consequences ("excludes LinkedIn-only people: nothing to collect"), never
change history and never restate the code.

### HEADLINE: CanonicalSnapshot is index.json wearing a dataclass

Two competing read models coexist. `Db` is the store you query; `CanonicalSnapshot`
is a full in-memory dump of the database — owner, parents, people, identifiers,
sources, artifacts, facts, dossiers, merge_verdicts — hydrated in one go and then
walked in Python. There are 37 `canonical_snapshot(` call sites and 13
`identity_snapshot(` call sites.

The pre-rewrite pipeline loaded one big JSON graph (index.json) and iterated it.
The rewrite changed the STORAGE to SQLite but kept the ACCESS PATTERN, which is
why "we converted to SQLite" and "we still hand-roll GROUP BYs" are both true.

This one root explains most other findings: the hand-rolled grouping in
`source_parents`; `_approved_identities` hydrating two whole snapshots to answer
one question; `person_lookup` building a Python inverted index over an indexed
table; `sqlite_adapter.decide` hydrating an identity snapshot to find one row by
key; and the `_snapshot: CanonicalSnapshot | None = None` performance seam in
`synthesis/selection.pending_target_bundles` — you only thread a snapshot down to
avoid re-hydrating something that should have been a query.

Target: stages take a `db` and query for exactly what they need. The whole-graph
snapshot shrinks to the places that genuinely need the whole graph (migration, and
arguably dossier evidence assembly). The `_snapshot` escape hatch disappears with
its cause, not by being renamed.

### `synthesis/prompting.py`: four worth policies, and a version hash that misses them

`WORTH_POLICIES` loads four prompt files (mixed/email/phone/unknown), each ONE
sentence, selected by a four-way ternary on which channels are present. The
distinctions are real product intent (email biases yes; a bare phone number is
weak evidence; mixed means either channel can carry the relationship), but that is
one paragraph with a condition — not four files, four `load_prompt` calls, a dict
and a selector. Collapse to one worth-policy prompt that states the channel facts
inline.

CORRECTNESS BUG found while reading it: `SYNTHESIS_VERSION` hashes only
`{contract, SYSTEM_PROMPT, FACT_SCHEMA}`. The worth policies and the owner blocks
(`OWNER_PROMPT_SUFFIX`, `OWNER_IDENTITY_CHECK`, `owner_identity_block`) are NOT in
the hash, while re-synthesis is skipped whenever `(input_fingerprint,
synthesis_version)` matches. So editing a worth policy — or the owner identity
block — changes nothing for existing parents: they keep the verdict produced under
the old wording, silently and permanently. Every prompt input that can change the
model's output must enter SYNTHESIS_VERSION.

Note the deliberate cost when fixing: bumping the hash re-synthesizes every parent
(paid). That is correct behaviour and should be stated in an intent comment next
to the hash, so the cost is a visible decision rather than a surprise.

### One LLM call pattern, written three times — and synthesis stalls between waves

Three sites call `client.responses.create` with the same shape:
`synthesis/runner.py` (`call_one`), `identity_evidence.py` (the identity judge),
`merge_candidates/judge.py` (the pair judge). The primitives are shared
(`make_async_client`, `responses_kwargs`, `is_retryable`, `parse_json_response`,
`usage_tokens`) but the LOOP is triplicated: acquire semaphore, retry with
backoff, parse the schema response, tally usage. Extract one schema-call object
(client/model/effort/schema in, typed result + usage out) and have all three use
it.

Concurrency is also inconsistent between them:
- `identity_evidence.judge_batch` builds every coroutine and `asyncio.gather`s
  them under a semaphore — true slot filling: a finished call frees its slot
  immediately.
- `synthesis/runner.driver` chunks people into waves of `stage.chunk_people` and
  `await drain_pool(...)` per wave. Inside a wave it is slot-filled, but the await
  is a barrier — as a wave drains to its last straggler the remaining slots idle,
  then the next wave starts. On a 542-person run that is a tail stall per wave.

The chunking appears to exist to bound how many message-batch sets are
materialized at once (`person_batches` is built for the whole chunk before the
coroutines are created). Build batches lazily inside `synthesize_person` and use
one flat pool over all bundles, so the semaphore alone bounds concurrency.
