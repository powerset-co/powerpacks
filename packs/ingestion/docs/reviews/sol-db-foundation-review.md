# Independent review: Deep Context SQLite foundation

Reviewed commit: `1eb2b944`

Review date: 2026-08-05

Scope: schema, legacy import, projection boundary, domain writes, migration
safety, worth/LinkedIn semantics, and proof gates

## Verdict

Do not treat this commit as the canonical database foundation yet. It creates a
useful SQLite skeleton and an explicit one-time importer, but the schema cannot
represent the current identity graph, the importer demonstrably breaks
canonical ownership on the copied real store, and the available write/read API
does not enforce the product's worth and LinkedIn policies.

The four focused tests pass. They prove basic SQLite mechanics, not queue or
output parity.

## Required runtime boundary

The correct boundary is:

1. Enrichment stages durably emit their existing paid/cacheable files.
2. An explicit, idempotent projector parses each stage's files at one boundary
   and transactionally upserts the queryable projection plus stable artifact
   path and fingerprint into SQLite.
3. The projector, not the webserver, owns compatibility with emitted file
   shapes. Re-projecting the same fingerprint is a no-op; a new fingerprint
   replaces that artifact's projection without overwriting human decisions.
4. The web runtime reads SQLite only. It does not glob artifact directories,
   consult mtimes, or fall back to CSV/JSON.
5. The one-time legacy importer remains separate from normal stage projectors.
   Compatibility exports are explicit downstream handoffs.

This keeps paid artifacts as files without creating a second runtime store.

## Ranked blockers

### P0: candidate membership and identifiers are not representable

`links` has one nullable `person_id` and one independently writable nullable
`parent_id`; there is no candidate-to-people relation for a candidate associated
with multiple children (`db/schema.py:247-260`). There is also no normalized
person identifier relation. Email and phone arrays exist only as JSON cells on
a link (`db/schema.py:252-253`).

The importer discards the legacy verdict's `person_ids` list and stores only its
candidate key and `parent_slug` (`migration/legacy.py:195-211`). Consequently, the
database cannot derive the complete sibling set or answer identifier ownership
without rebuilding relations from legacy payloads.

Smallest correction:

- add `link_people(link_key, person_id)` with a composite primary key and real
  foreign keys;
- add `person_identifiers(person_id, kind, normalized_value)` or an equivalent
  normalized relation;
- derive a link's canonical parent through its related people rather than
  allowing `links.parent_id` to disagree;
- project every verdict `person_ids` member explicitly.

### P0: referential and human-decision integrity are absent

`decisions(kind, target)` is a polymorphic text target with no foreign key
(`db/schema.py:262-271`). No person, parent, link, fact, synthetic, verdict,
research, guidance, or job owner column has a foreign key
(`db/schema.py:234-318`). Enabling `PRAGMA foreign_keys` in connections therefore
does not protect anything (`db/store.py:108-122`).

The public `upsert_decision` and generic `update_link(**columns)` methods let any
caller overwrite terminal human state or create mismatched rows
(`db/store.py:148-149,177-188`). A direct check confirmed that an identity
decision targeting a nonexistent link inserts successfully.

Smallest correction:

- place optional human worth directly on `parents`;
- place machine judgment and optional human identity decision on the candidate,
  or use separate typed decision tables with real parent/link foreign keys;
- add foreign keys to all canonical owner relations;
- remove generic product-facing decision and column-update doors;
- make machine upserts preserve non-null human decisions by construction.

### P0: retarget and parent settlement are not one valid domain transaction

`settle_parent` accepts a caller-supplied `person_id` and a caller-built
`synthetic_withdraw_pubs` list (`db/views.py:43-46`). It derives siblings only
through `links.person_id -> people`, ignoring direct parent links and synthetic
profiles (`db/views.py:23-32,56-74`). It does not validate that the clicked link,
decision target, and supplied person belong together.

`identity_decision` accepts `new_url` and `new_pub` but discards both
(`db/views.py:78-88`). A fix therefore records `retarget` without its replacement;
persisting the replacement requires a separate generic link update outside the
settlement transaction.

Smallest correction:

Implement one `settle_identity(clicked_key, action, replacement_url=None)`
transaction. It should load the clicked candidate, derive the canonical parent
and every related real/synthetic/ghost sibling inside SQLite, normalize and
store a replacement when required, preserve prior terminal human decisions,
withdraw undecided siblings, and settle related synthetic gates.

### P0: the required worth and LinkedIn queries do not exist

The only progress query counts every undecided link and every parent lacking a
human worth row (`db/views.py:91-101`). Those counts do not implement either
product queue.

The worth query must be facts-backed, grouped by canonical parent, exclude owner
and all-ghost groups, aggregate child machine judgments as `yes > maybe > no`,
apply `COALESCE(human_worth, machine_worth, 'maybe')`, and exclude researched
synthetic profiles from re-entry.

The LinkedIn query must count canonical parents, not raw links, and encode the
terminal-human, authoritative-machine-detach, accepted-candidate-retarget,
rejected-paid-profile, synthetic-gate, effective-No, and raw-import policies.

Smallest correction:

- project explicit owner, ghost, candidate-origin, research, synthetic, and
  judgment signals into queryable columns/relations;
- implement named `worth_queue` and `linkedin_queue` SQL queries with small
  hydration helpers;
- make `stage_progress` count those named queries rather than inventing a
  weaker predicate.

### P0: the copied real-store import produces broken ownership

An explicit import of the diagnostic mirror named in the mandate produced:

| Check | Result |
|---|---:|
| people | 366 |
| parents | 5,495 |
| links | 6,954 |
| facts | 5,512 |
| verdicts | 113 |
| synthetic profiles | 142 |
| links whose `person_id` has no `people` row | 6,499 |
| verdict parent references absent from `parents` | 111 |
| synthetic parent references absent from `parents` | 140 |
| worth decisions targeting no parent | 103 |

The old worth view over the same copied store still returns the pinned
`5,379 total / 61 pending / 4,169 yes / 1,149 no` counts. The new database has
no equivalent query to compare, and its imported relations already show why a
future query would drift.

Two direct causes are visible: synthetic `parent_id` receives
`source_parent_slug` (`migration/legacy.py:153-165`) and verdict `parent_id` receives
`parent_slug` (`migration/legacy.py:195-211`), although canonical parents use index
parent IDs.

Smallest correction:

- resolve all slugs to canonical parent IDs during import;
- reject every unresolved owner relation with a summarized error;
- make zero orphan relations a hard import gate before commit.

### P0: legacy worth semantics are not preserved

The importer reads only the final JSONL object and treats a direct-shape facts
record as empty unless it has a nested `facts` object (`migration/legacy.py:171-192`).
The current worth reader accepts both shapes and accumulates name, owner, and
the last valid worth verdict across records (`worth_view.py:87-119`).

The importer also leaves legacy child worth decisions targeted to child/link
keys (`migration/legacy.py:122-128`) instead of moving the latest applicable human mark
onto the canonical parent. It does not apply the retired message-LinkedIn alias
grouping used by the current view.

Smallest correction:

Pin these legacy-only rules inside `migration/legacy.py`: parse all valid records,
support direct and enveloped facts, retain `is_owner`, apply the retired alias
mapping, aggregate machine worth by parent, and migrate the latest child human
mark to the canonical parent. The normal projector should accept only current
stage output shapes.

### P1: `verdicts` duplicates candidate truth

The schema creates a second verdict table (`db/schema.py:282-287`) while links
already hold parts of the same machine judgment (`db/schema.py:247-257`). The
store exposes a separate verdict upsert (`db/store.py:154-155`), and the importer
does not project verdict JSON into its candidate row (`migration/legacy.py:195-211`).

Smallest correction: remove the verdict table/type/upsert. Store queryable
verdict, confidence, reason, fingerprint, raw payload/path, and projection
timestamp on the candidate, with one machine projector owning those fields.

### P1: the stage projector and artifact projection are missing

There are row-level upserts, but no explicit idempotent projector for emitted
facts, research results, dossiers, or profile snapshots. Facts store path plus
mtime rather than a stable input fingerprint; verdicts have a payload but no
artifact path; dossier and cached profile artifacts have no projection at all
(`db/schema.py:273-301`).

Smallest correction:

- add narrow projectors per emitted artifact shape;
- store artifact kind, stable owner, path, content/input fingerprint, status,
  and the JSON/columns required by queries;
- apply one artifact's projection in one transaction;
- skip a repeated fingerprint and replace only machine-owned fields on change;
- never invoke projectors from web reads.

### P1: workflow and spend state remain untyped blobs

`jobs` accepts arbitrary names/statuses and `stage_state` is an unrestricted
JSON blob (`db/schema.py:309-318`). The schema has no typed enrichment selection
fingerprint or spend approval tied to that selection. The importer can ingest
arbitrary manifests but does not import guided-retarget state or approvals
(`migration/legacy.py:227-254`).

Smallest correction: use small literal-schema rows for stage completion,
selection fingerprint, approval amount/count/fingerprint, guided-retarget state,
and job status. Preserve result detail as JSON only after the typed decision
columns exist.

### P1: schema-version validation is not a migration strategy

The rewritten layout retains schema version 4 (`db/schema.py:13`). Opening a
same-version store executes the current DDL before checking columns
(`db/store.py:77-106`). Operational errors are now converted to a clear failure,
but the version was not advanced and validation checks only missing column
names, not types, constraints, or foreign keys.

Smallest correction: assign this incompatible layout a new version, inspect and
validate before schema mutation, implement only explicit supported migrations,
and otherwise fail without touching the canonical database.

### P1: the proof suite does not cover the mandate

`tests/test_deep_context_db_core.py` has four tests: version refusal, primitive
type round-trip, a two-link happy settle, and a one-row import
(`tests/test_deep_context_db_core.py:24-112`). It does not assert copied-store
queue keys/counts, multi-child candidates, orphan rejection, human-decision
preservation, retarget replacement, synthetic fan-out, ghost handling,
authoritative detach, rejected paid profiles, reset, spend persistence, or
boundary exports.

Smallest correction: make the copied-store dynamic parity checks and focused
synthetic incident tests the acceptance gate before web cutover.

## Size assessment

At this commit, `packs/ingestion/primitives/deep_context/` contains 24,114
Python lines, and `review/` alone contains 6,476 Python lines. The new DB
package is 1,093 Python lines. No prompt `.txt` or YAML assets currently exist
under Deep Context, so the prompt budget has not yet been split out.

The route to roughly 5,000 runtime Python lines is deletion during cutover, not
further layering: once named SQL queries and domain transactions reach parity,
remove the in-memory model, CSV decision paths, mtime observers, old
`review_db.py`, and superseded tests in the same ownership change.

## Recommended correction order

1. Repair candidate membership, identifiers, foreign keys, and human-decision
   ownership; remove the duplicate verdict truth.
2. Rewrite the one-time importer and current-stage projectors against that
   model; require zero orphan relations.
3. Implement worth and LinkedIn named queries and prove copied-store key/count
   parity.
4. Implement `set_worth`, `settle_identity`, resets, guidance, stage, and spend
   transactions; pin the incident semantics.
5. Cut the webserver to SQLite-only reads/writes and delete the old runtime in
   the same change.
6. Add explicit compatibility exports, downstream output diffs, click-latency
   measurement, and the Python/prompt LOC report.
