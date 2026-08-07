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
| `collection/planning.py` | `getattr`-by-string; MESSAGE_CHANNELS rebuilt per call; meaningless nested dict types; hand-rolled GROUP BY; name says nothing |
| `context_sources.py` | does not own its readiness; `probe_chat_db` returns `dict[str, object]`; lives outside `collection/` |
| `email_context.py` | lives outside `collection/` |
| `synthesis/selection.py` | `_snapshot` performance seam |
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

Affected: shared/build_owner, shared/check_readiness*, collection/collect_person_context,
synthesis/compose_dossier, shared/lookup_person, enrich/parallel_research/models,
realize/persist_review_identities, enrich/prefetch_profiles,
enrich/profile_projection, review/server, synthesis/validate_dossiers.
Only 4 mains pass `db_path=` today (build_owner, check_readiness, collect, lookup).

### people.csv has two owners; stage 1 has no entry point

`people.csv` was projected into SQLite in TWO places: the migration path and
`CollectPersonContext.execute()` (every run). Neither double-write was wrong —
both paths were idempotent
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

`collection/planning.py: source_parents` builds `message_channels =
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

`collection/planning.py: union_bundles` defines a closure `strings(field: str)` that
does `getattr(bundle, field)`, called as `strings("emails")`, `strings("phones")`,
`strings("source_channels")`, `strings("groups")`. Four literal field names
resolved at runtime against a frozen dataclass — rename a CollectionBundle field
and this breaks at runtime with no type-checker signal, which is precisely what
the typed-rows work was meant to prevent.

Fix: pass values, not field names — a helper over `Iterable[str]` groups, called
as `_merged(b.emails for b in source)`. The name can then say what it does
(merge + remove duplicates + sort a string field across bundles) instead of `strings`.

### Stage 2 is split across two locations, and "state.py" names nothing

The real axis is sound: `context_sources.py` reads the outside world (msgvault,
chat.db, wacli -> MessageEntry); `collection/planning.py` reads our own record and
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

### Concurrency is configured inline in four files, with disagreeing defaults

Four sites resolve the same concept (how many OpenAI calls in flight) from the
same env var `POWERPACKS_OPENAI_CONCURRENCY` and profile key `openai_concurrency`,
each spelled out mid-function with its own magic fallback:

- `synthesis/runner.py` fallback 16
- `merge_candidates/judge.py` fallback 64
- `enrich/identity_reconcile/runner.py` fallback 64
- `enrich/research_reconcile/judging.py` fallback `identity_evidence.DEFAULT_IDENTITY_CONCURRENCY` (the only named one)

So unless the env var is set, synthesis runs at a QUARTER of the judges'
concurrency, for no stated reason. Compounds the wave-barrier finding: synthesis
is the slowest stage, the most throttled, and stalls between waves.

Fix: one named constant per stage at module top (or ownership by the shared
schema-call object from the previous item), so the number is a visible decision
rather than a buried literal.

Same file, same class of wart: `total = len(plan.bundles)` in `run_paid` is used
exactly ONCE, in a progress print. Inline it and delete the binding.

### One LLM client object — and stop reimplementing the SDK

Related to the triplicated call loop and the four inline concurrency configs: the
shared helper module already exists but lives in ANOTHER PACK
(`packs/indexing/lib/openai_responses.py`), and it deliberately stops short of
owning the call: `make_async_client` constructs `AsyncOpenAI(..., max_retries=0)`
with the docstring "callers own retry/backoff".

So the OpenAI SDK's own retry-with-backoff is switched off and re-implemented by
hand in three places, and `is_retryable` approximates the status set the SDK
already retries. (One thing this does get right: because SDK retries are disabled
there is no retry amplification — SDK attempts multiplied by our attempts — on a
paid call.)

Target: one client/caller object in a shared home (not the indexing pack) owning
the AsyncOpenAI instance, the concurrency slots (one resolved number, not four
inline fallbacks of 16/64/64/named), retry (the SDK's or exactly one
implementation), schema kwargs, response parsing and usage tally. The three call
sites collapse to `await caller.run(prompt, system_prompt)`.

Before deleting the hand-rolled loop, confirm the SDK's retry semantics match
`_RETRY_STATUS` and the current backoff — this is a paid path, so behaviour parity
must be checked, not assumed.

### Manifests belong in one folder, one file per manifest

Twelve manifest/receipt types live under three different conventions: seven
defined inline in their stage driver (ensure_parents, compose_dossier,
reconcile_linkedin, build_owner, synthesize_person_context,
cluster_merge_candidates, build_parents), four in a package models.py
(collection, review_web, research_reconcile x2), one in its own file
(enrichment_receipt).

Owner's call: pull them into a `manifests/` folder, one file per manifest type,
imported by the stage that emits it. This is the one concept that is genuinely
cross-stage — every stage's public output contract, all deriving from
StageManifest — so it earns its own home rather than being scattered across
drivers and per-package models files.

### `synthesize_person_context.py`: three whole-DB loads and two fingerprint passes per run

One `execute()` does:
1. `_migrate_parent_cache()` -> `_plan()` -> `build_plan` hydrates snapshot #1 and
   runs `pending_target_bundles`, computing `input_evidence_fingerprint` over every
   pending bundle (hashing all their message content);
2. normalizes the parent cache;
3. `_plan()` AGAIN -> snapshot #2 plus the entire fingerprint pass a second time;
4. `run_paid` (its own reads);
5. `canonical_snapshot(self.db)` a third time, purely to count parent facts.

The re-plan has a real reason — normalization rewrites the paid cache — but the
FIRST plan is consumed for one value, `plan.system_prompt`. It computes the
expensive pending-bundle fingerprints only to discard them. Split "build the
system prompt" (needs the owner) from "compute pending bundles" and one full
hashing pass disappears. The third hydration is a `SELECT count(*)` wearing a
whole-graph load.

Same file: the constructor takes 15 parameters and assigns them one-to-one to 15
attributes, and the instance is then handed to `runner.run_paid(self, ...)`, which
types it as `SynthesisStage` — the node is duck-typed as its own config bag. Make
the config a frozen dataclass the node holds, and drop 30 lines of hand-copied
assignment.

### `merge_candidates/models.py` holds four kinds of thing, two of them duplicates

The file is named models.py and contains: the 8 dataclasses (correct, lines
18-134), two contact-field classifiers, one database query, and three
accessors/formatters.

- `load_people(db)` is a QUERY — it hydrates `canonical_snapshot(db)` and
  hand-rolls the identifiers-by-person-by-kind grouping (same shape as
  `source_parents`). It belongs in `db/` as a typed query returning MergePerson
  rows; it is also one of the 37 canonical_snapshot sites the headline conversion
  covers.
- `identifier_emails` / `identifier_phones` DUPLICATE the existing shared home,
  `primitives/common/contact_fields.py`, which already provides EMAIL_EXTRACT_RE,
  normalize_email, emails_from_value/row, normalize_phone, canonicalize_phone,
  phones_from_value/row. They belong there — and note merge's rules genuinely
  differ (7-15 digit window; rejecting domain-lookalikes via
  `[a-z]{2,}\.[a-z]{2,}`), so if the divergence is real it must be PINNED and
  documented at the definition, not silently forked. This is the exact
  helper-duplication the ground rules call out.
- `all_emails` / `all_phones` are derived attributes of MergePerson and belong on
  the model; `fmt_phone` is a display formatter and belongs with rendering or the
  shared contact-field home.

After those moves, models.py holds models.

### `run_paid` returns configuration the caller already owns

`SynthesizePersonContext.__init__` stores `self.reasoning_effort` and
`self.concurrency`, hands `self` to `runner.run_paid(self, plan, tally)` as the
SynthesisStage, and run_paid returns `(concurrency, effort)` — the same two values,
resolved — purely so `execute()` can put them in the manifest.

Two values, three representations: raw config on the node ("medium", None),
resolved values inside the worker (`reasoning_effort(...)`, `... or
env_or_profile_int(..., fallback=16)`), and manifest fields. A function whose job
is doing the work should not own config resolution, and should not have to return
it.

Also: the early `return 0, effort` when there are no bundles makes the manifest
record `concurrency: 0` to mean "did not run", in a field that otherwise means
"slots used" — 0-as-sentinel in a reported value.

Fix: resolve once at construction into a frozen config (concurrency with its one
named default, effort normalized), held by the node, used by run_paid, read by the
manifest. run_paid returns work results; "nothing ran" is the people count being
zero.

### Every stage gets a folder — nothing floats at the package root

36 .py files sit loose at the deep_context root against 12 folders. Owner's call:
tiny buckets, not one pile. Every stage owns a folder INCLUDING its entry point;
the root keeps `__init__.py` and nothing else.

| Folder | Contents |
|---|---|
| `ensure_parents/` | ensure_parents.py, imported_people.py, assignment.py, models.py |
| `collection/` | collect_person_context.py, context_sources.py, email_context.py, models, planning |
| `synthesis/` | synthesize_person_context.py, compose_dossier.py, validate_dossiers.py, facts.py, models.py, rendering.py |
| `merge_candidates/` | cluster_merge_candidates.py, build_parents.py, rendering.py |
| `enrich/` | identity_evidence.py, judge_models.py, assemble_synthetic_profile.py, prefetch_profiles.py, reconcile_linkedin.py, reconcile_deep_research.py, deep_research_contacts.py, enrichment_{pipeline,contract,receipt}.py, research_result.py, profile_{models,projection}.py, synthetic_models.py, and the existing identity_reconcile/, research_reconcile/, parallel_research/ nested under it |
| `review/` | web server/rendering/assets, guided_retarget.py, heal_review.py, reconcile_review_web.py, restart_review.py |
| `realize/` | apply_retargets.py, persist_review_identities.py |
| `migration/` | migrate_sqlite.py, legacy.py, canonical_graph.py, parent_graph.py (the dying mass, together, so it deletes as one folder) |
| `shared/` | common.py, check_readiness.py, readiness_models.py, build_owner.py, lookup_person.py, dossier_evidence.py |
| unchanged | db/, manifests/ (new, per the manifests item), prompts/, tools/ |

Pure moves: imports-only diffs on the moved bodies. Every consumer must be
updated — bin/deep-context, skills that invoke primitives by file path, tests,
docs, the pipeline node registry — finishing with a zero-stale-reference grep, per
the moving-and-deleting rules. Bonus: putting the whole dying legacy mass in
`migration/` means the v1.19.0 deletion is one folder removal.

### `cluster_merge_candidates.py`: clever syntax hiding a no-op and a type collision

The verdict assembly in `execute()` concatenates two list comprehensions, and:
- `[verdict for verdict in survey.reused]` is a no-op comprehension — it is
  `list(survey.reused)` written as a loop;
- `verdict` names two DIFFERENT types in one expression: the raw decision being
  wrapped into a MergePairVerdict in the first half, an already-built
  MergePairVerdict in the second;
- `left`/`right` are list INDICES, so `people[left]` couples the survey's tuples
  positionally to the people list and the two must stay index-aligned forever.

That last point is the root: `survey.slam` / `to_judge` / `reused` are tuples of
untyped tuples (`list[tuple[int, int, str]]`) — the shape the typed-rows work was
meant to remove, which merge_candidates never got. Type them (a pair row carrying
both people, not indices), then the assembly is two plain statements or, better, a
`survey.initial_verdicts(people)` method that owns the composition instead of the
caller assembling it inline.

### Two findings from the post-reorg read-through (2026-08-07)

**Spend-gate asymmetry.** Stage 6 (research) refuses to spend without `--approve`
plus a budget check that short-circuits to `needs_approval` before any paid call.
Stage 3 (synthesis) and stage 4 (the merge judge) spend on a plain `execute()` —
the only friction is choosing to run `--dry-run` first, which is a convention in
the skill, not a gate in the code. Given a full synthesis run is now ~744 parents,
decide whether that asymmetry is intended; if it is, say so at each spend site so
the next reader does not "fix" it.

**`shared_unsettled` pairs may have no owner.** `merge_candidates/receipts.py`
deliberately never judges pairs that already share an observed identifier, on the
theory that the identity graph should have joined them. But stage 1's `_components`
unions only on person-id continuity (`person_id`, `superseded_person_ids`) and
existing parent links — NOT on shared email/phone. Two people.csv rows sharing an
email with no id continuity and no prior parent link are therefore joined by
neither stage 1 nor the judge. Confirm this state is actually unreachable, or give
those pairs an owner.
