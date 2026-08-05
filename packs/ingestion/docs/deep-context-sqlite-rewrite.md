# Deep Context SQLite rewrite mandate

Status: canonical implementation contract  
Updated: 2026-08-05

This document is the source of truth for the Deep Context rewrite. When code,
an older migration document, a test, or an agent plan disagrees with it, this
document wins until the owner changes it.

## Outcome

Deep Context is a small local pipeline and labeling application backed by one
SQLite database:

```text
.powerpacks/deep-context/deep-context.sqlite
```

Keep and improve the existing SQLite foundation. Do not replatform it and do
not preserve the old file-state architecture inside SQL-shaped wrappers.

The initial rewritten runtime targets about 5,000 production Python lines. It
may temporarily approach 7,000 during cutover, but it is not done until the old
paths are deleted and the runtime is moving toward the 5,000-line shape.

## Rule zero

This is one user running one local webserver and one pipeline command at a
time. Design for that product:

- no file locks, writer fleets, recovery protocols, run IDs, ledgers, or
  background state machines;
- ordinary SQLite transactions provide durability;
- atomic temp-file plus rename is the rule for exported files;
- one user action is one domain transaction, even when it touches several
  rows;
- correctness is measured by product queues and outputs, not by preserving the
  internal structure of the legacy implementation.

## One runtime store

SQLite is the runtime record after bootstrap. `review.csv`,
`synthetic-people.csv`, `verdicts.jsonl`, and `index.json` must never compete
with it through mtime checks, implicit refreshes, or read fallbacks.

One explicit legacy importer may read the existing files and populate a fresh
database. That importer is allowed to be ugly because it is the single
boundary that absorbs old shapes. It must be isolated in `db/legacy.py` (and
the narrow CSV helpers it calls), tested, and removable after old installs have
migrated.

After bootstrap:

- runtime reads query SQLite only;
- runtime decisions and stage writes update SQLite only;
- compatibility CSV/JSON files are written explicitly at downstream handoff,
  export, or clean shutdown;
- touching an exported file never causes an automatic database import;
- schema mismatch never drops the canonical database. Open it with a supported
  migration or fail with a clear error.

Paid artifacts remain files. Parallel results, source bundles, facts JSONL,
dossier Markdown, and profile-cache payloads are not regenerated or moved.
SQLite stores their stable owner, path, fingerprint/status, and the parsed
projection or JSON payload required by queries. Web views must not rediscover
state by globbing those directories.

## Product semantics

### Worth

Synthesis currently emits:

- `network_worth.decision`: `yes | maybe | no`;
- `network_worth.reason`;
- overall profile `confidence` (this is not a separate worth confidence).

The database stores those values plus an optional human override. Effective
worth is:

```sql
COALESCE(human_worth, machine_worth, 'maybe')
```

The worth review queue is the facts-backed, non-owner, non-ghost population
whose effective worth is `maybe`, grouped as one canonical parent/person.
Child machine judgments aggregate `yes > maybe > no`. A synthetic profile that
already passed through research is not reintroduced as a worth card.

The worth view is a named query plus hydration. It is not a Python model
rebuild. A worth click writes the human override and note, then the next query
returns the next card.

### LinkedIn identity

The database must represent the relation among canonical parent, child people,
and all identity candidates. Candidates include real LinkedIn profiles,
research retargets, synthetic profiles, candidate-origin people, and ghost/no-
link rows. A candidate may be related to more than one child identity; this
relation must be queryable rather than reconstructed from key spelling.

The pending queue preserves current product policy:

- a human identity decision is terminal;
- an authoritative machine detach is terminal;
- an accepted candidate-origin retarget stands;
- a rejected or ambiguous paid profile remains reviewable when current policy
  says the human should see it;
- synthetic candidates remain pending until their human gate is yes/no;
- effective-worth No and raw import candidates do not enter LinkedIn review.

One decision settles the canonical parent in one transaction: apply the clicked
decision, withdraw undecided sibling candidates, and settle related synthetic
gates. The database derives the sibling set. Callers must not pass a prebuilt
list assembled from files or an in-memory model. A retarget decision must store
the replacement URL/public identifier in that same transaction.

### Workflow and jobs

Stage completion, spend approval, enrichment selection fingerprint, and guided
retarget status are small typed rows. They are not an untyped meta ledger.

Submitting guided retarget research is a small web endpoint. The paid research
execution remains a separate in-process job function that writes its artifacts
and projects the result into SQLite. The HTTP handler does not contain the
research engine.

## Database concepts

Improve the existing tables where they are useful; do not rename them merely
for aesthetics. The canonical model must cover these concepts:

- `parents`: canonical person/group, display name/slug, dossier reference,
  machine worth, optional human worth and note;
- `people`: child identity, parent membership, display/facts projection and
  dossier reference;
- normalized identifiers or an equivalent queryable relation for email/phone;
- `links`/candidates: candidate-parent/person membership, real/synthetic/ghost
  kind, profile projection, machine proposal/judgment, replacement target;
- human identity decisions that cannot be overwritten by machine refreshes;
- `synthetic_profiles`: complete exportable payload and human gate;
- artifact projections for facts, research, dossiers, and profile snapshots;
- durable guided-retarget/job state;
- typed stage completion and spend approval.

Avoid duplicating one truth across tables. In particular, do not create a
second verdict table when the queryable verdict projection belongs on the
candidate row. Preserve the immutable raw payload/path for evidence and cache
reuse.

Inside SQLite use `NULL`, `REAL`, and JSON payloads where they express the
domain. Empty strings and pipe-delimited lists are legacy baton representations
and belong only in the importer/exporter. Foreign keys or domain write methods
must prevent orphan decisions and mismatched candidate kinds.

## Minimal database API

The database package should expose only what the product uses:

- open/create a supported database;
- explicit `import_legacy(...)` for a fresh database;
- machine-stage upserts for facts, people/parents, candidates, synthetic
  profiles, research projections, and job state;
- domain transactions: `set_worth`, `settle_identity`, `reset_identity`,
  `save_guidance`, and stage/spend state updates;
- named reads: `worth_queue`, `linkedin_queue`, `siblings_of`,
  `stage_progress`, `directory`, and `person_detail`;
- explicit compatibility exports.

Do not provide a generic `update_link(**columns)` escape hatch, automatic mtime
imports, whole-store click commits, or one transaction per row when the domain
action spans several rows.

## Web boundary and size

The Python webserver is transport, not a second model. Target less than 1,000
Python lines for HTTP/SSE/job wiring. Static HTML, CSS, and JavaScript remain
assets; pure render helpers may remain separate.

The required surface is small:

- status/progress;
- next worth card and set/reset worth;
- next LinkedIn card and settle/reset identity;
- directory/person/dossier/avatar reads;
- guided retarget submit/status;
- stage complete and spend approval;
- feedback/auth adapters;
- static assets and an optional small SSE nudge stream.

The retarget/reconcile/synthesis engines are not HTTP handler code. The server
calls them and reads their projected status from SQLite.

## Execution order

1. Improve the existing SQLite schema/store and add the explicit legacy
   importer. Do not touch runtime consumers yet.
2. Import a copied real store and implement the named worth, LinkedIn, progress,
   directory, and settle operations.
3. Prove exact key/count parity against the old runtime. Explain every intended
   policy change before accepting it.
4. Rewrite the webserver to use only those queries and transactions; delete the
   in-memory model, cached parent snapshots, file locks, mtime observers, and
   CSV decision paths in the same cutover.
5. Move the remaining stage writers onto SQLite projections/upserts. Keep paid
   raw artifacts at their current paths.
6. Export the existing boundary files and prove downstream fan-in, realization,
   lookup, and directory outputs.
7. Delete the old 8k-line test file and rebuild roughly 100-200 focused tests by
   domain, retaining incident invariants rather than implementation trivia.
8. Run the real-store parity gates, targeted suites, output diffs, and LOC gate.

## Gates

The rewrite cannot claim a phase complete merely because a schema round-trips
CSV or a single row write is fast.

Required gates:

- a fresh legacy import contains every facts-backed worth subject and every
  current identity/synthetic candidate;
- worth queue keys/counts match the old effective-Maybe view;
- LinkedIn queue parent/candidate keys and progress match current policy;
- keep, skip, fix/retarget, reset, synthetic gating, and ghost/sibling settle
  match pinned incidents;
- research selection/spend approval and guided retarget state survive restart;
- explicit exports reproduce the boundary contracts needed downstream;
- downstream directory and realized people outputs match or have explained
  deltas;
- user click latency is measured on the complete domain transaction, not an
  isolated insert;
- production Python trends toward 5,000 lines, the webserver is below 1,000,
  and the focused suite is roughly 100-200 tests.

Reference snapshot from `/Users/arthur/workspace/powerpacks-jake-mirror` on
2026-08-05 (diagnostic only; parity tests compare keys dynamically when the
exact mirror is present):

- worth: 5,379 total, 61 pending, 4,169 yes, 1,149 no;
- LinkedIn: 756 in scope, 191 pending, 565 done;
- rejected parents: 1,136.

## Explicitly rejected shapes

- two runtime stores synchronized by mtimes;
- automatic schema drop/rebuild of the canonical DB;
- CSV-shaped empty strings and pipe lists throughout query code;
- generic polymorphic targets with no referential/domain validation;
- caller-supplied sibling/synthetic fan-out;
- whole-file export on every click;
- cached in-memory parent models that must be patched after writes;
- ledgers, run IDs, pending-export flags, writer locks, and recovery ceremonies;
- keeping old code beside new code after the new path owns the behavior.
