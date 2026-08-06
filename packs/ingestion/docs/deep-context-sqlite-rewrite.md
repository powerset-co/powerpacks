# Deep Context SQLite rewrite mandate

Status: canonical implementation contract
Updated: 2026-08-06

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

All imports live at module scope. Do not use function-local or method-local
imports to hide dependency cycles, optional paths, or startup failures. The
retained Deep Context tree must pass an AST scan with zero nested imports.

Long LLM prompts are assets, not implementation LOC. Move them into named
`prompts/*.txt` files, one prompt/template per concern, and keep Python limited
to small loaders, interpolation, schemas, and policy. Prompt text is excluded
from the 5,000-line Python budget; prompt-loading and prompt-selection code is
included. Use YAML only when a prompt genuinely needs structured metadata or a
literal rule table—do not add a YAML dependency merely to hold prose.

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
Legacy synthetic exports, verdict logs, and lookup snapshots must never compete
with it through mtime checks, implicit refreshes, or read fallbacks.

One explicit legacy importer may read the existing files and populate a fresh
database. That importer is allowed to be ugly because it is the single
boundary that absorbs old shapes. It is isolated entirely in `db/legacy.py`,
tested, and removable after old installs have migrated. No second CSV reader,
compatibility parser, or shared baton loader is permitted elsewhere in Deep
Context.

After bootstrap:

- runtime reads query SQLite only;
- runtime decisions update SQLite only;
- stage writers keep their fixed durable artifacts and project every payload
  needed by later stages into SQLite before returning success;
- compatibility CSV/JSON files are written explicitly at downstream handoff,
  export, or clean shutdown;
- touching an exported file never causes an automatic database import;
- schema mismatch never drops the canonical database. Open it with a supported
  migration or fail with a clear error.

Artifacts remain durable files. A stage worker may read the file it owns for
its own fixed-path reuse policy, then atomically writes its new source bundle,
raw result, facts JSONL, dossier Markdown, or profile-cache payload. The writer
immediately hands the completed bytes to its projector. The projector stores
the full payload required by downstream code in SQLite, including dossier text
and binary assets. No later stage reopens that file to make a decision or
hydrate a response.

At the end of each enrichment step, one explicit in-process projector parses
the completed files and commits their queryable projection to SQLite together
with the stable owner, artifact kind, path, content fingerprint, and projection
status. This is a stage handoff, not a second runtime store:

1. the worker atomically writes the artifact file;
2. the projector is the only current-shape reader of those completed bytes;
3. one SQLite transaction upserts the full downstream payload and marks the fingerprint
   projected;
4. only then is the result visible as ready to the web application.

Projection is idempotent by artifact kind + owner + content fingerprint. A
retry of the same completed artifact is a no-op; a new fingerprint replaces
the queryable projection without erasing human decisions. If projection fails,
the artifact remains reusable and the stage reports the parse/projection error;
the webserver does not attempt recovery or filesystem reconciliation.

Runtime web views query SQLite only. Dossier/profile/image responses return the
projected SQLite payload; they never open artifact paths, glob directories,
compare mtimes, parse enrichment outputs, or silently import files while
serving a request. Frozen responses may still expose a path string as inert
compatibility/provenance metadata; neither the handler nor its adapter may
dereference it.

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

Stage manifests are write-only receipts for counts, timing, and errors. Nothing
reads them to decide pending work or the next action. Spend approval is the
explicit budget flag passed when a paid job launches, never durable control
state. The `jobs` table is the sole async running/progress/error receipt and
double-submit guard.

Submitting guided retarget research is a small web endpoint. The paid research
execution remains a separate in-process job function that writes its artifacts
and projects the result into SQLite. The HTTP handler does not contain the
research engine, select artifact paths, or parse provider output. It passes the
approved budget and projected subject key to the job function; the job owns its
fixed output paths.

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
- artifact projections containing downstream payloads for source bundles,
  facts, research, dossiers, profiles, and binary assets;
- durable guided-retarget/job receipts.

Avoid duplicating one truth across tables. LinkedIn-candidate judgments belong
on the candidate row. The separate `merge_verdicts` table is only the paid
same-person pair cache and accepted graph-edge projection; no CSV copy controls
it. Preserve the immutable raw payload/path for evidence and cache reuse.

Inside SQLite use `NULL`, `REAL`, and JSON payloads where they express the
domain. Empty strings and pipe-delimited lists are legacy baton representations
and belong only in the importer/exporter. Foreign keys or domain write methods
must prevent orphan decisions and mismatched candidate kinds.

## Minimal database API

The database package should expose only what the product uses:

- open/create a supported database;
- explicit `import_legacy(...)` for a fresh database;
- explicit idempotent artifact projectors for facts, people/parents,
  candidates, synthetic profiles, research results, dossier/profile snapshots,
  and job state;
- domain transactions: `decide_worth`, `settle_identity`, `reset_identity`,
  `save_guidance`, and async job receipt updates;
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

### Frozen HTTP compatibility contract

The current Deep Context HTTP interface is already the product contract. The
SQLite rewrite is an implementation replacement behind it, not an API redesign.
Preserve every existing route, accepted query/form field, response content type
and shape, status code, and browser-visible behavior. In particular, do not
rename, consolidate, REST-ify, or delete routes merely to make the new server
smaller.

The frozen route inventory is:

- GET `/`, `/directory`, `/healthz`, `/api/events`, `/api/status`,
  `/api/enrichment`, `/api/retargets`, `/api/dossier`,
  `/api/worth-card`, `/api/linkedin-card`,
  `/api/person`, `/api/avatar`, and the two existing asset paths;
- POST `/decide`, `/worth`, `/complete`, `/approve-enrichment`, `/retarget`,
  `/feedback`, and `/auth/login`.

Before replacing a handler, pin its current success and error responses with
contract tests. Internal DB/domain method names are not public API and may be
made smaller, but the existing JavaScript and any external caller must continue
to work unchanged.

The required surface is small:

- status/progress;
- next worth card and set/reset worth;
- next LinkedIn card and settle/reset identity;
- directory/person/dossier/avatar reads;
- guided retarget submit/status;
- stage-complete compatibility and budget-approved job launch;
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
5. Keep enrichment workers file-output-first, then call their explicit SQLite
   projectors at successful stage handoff. Keep paid raw artifacts at their
   current paths; do not teach the webserver to import them.
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
- projected research results, job receipts, and guided-retarget state survive
  restart; spend approval itself is only the launch-time budget flag and is not
  persisted;
- explicit exports reproduce the boundary contracts needed downstream;
- downstream directory and realized people outputs match or have explained
  deltas;
- user click latency is measured on the complete domain transaction, not an
  isolated insert;
- production Python trends toward 5,000 lines, the webserver is below 1,000,
  and the focused suite is roughly 100-200 tests. Prompt `.txt` assets are
  reported separately from Python LOC.

Reference snapshot from `/Users/arthur/workspace/powerpacks-jake-mirror` on
2026-08-05 (diagnostic only; parity tests compare keys dynamically when the
exact mirror is present):

- worth: 5,379 total, 61 pending, 4,169 yes, 1,149 no;
- LinkedIn: 756 in scope, 191 pending, 565 done;
- rejected parents: 1,136.

## Explicitly rejected shapes

- two runtime stores synchronized by mtimes;
- enrichment workers writing only SQL and discarding their durable file output;
- web requests globbing, parsing, or auto-projecting enrichment artifacts;
- automatic schema drop/rebuild of the canonical DB;
- CSV-shaped empty strings and pipe lists throughout query code;
- generic polymorphic targets with no referential/domain validation;
- caller-supplied sibling/synthetic fan-out;
- whole-file export on every click;
- cached in-memory parent models that must be patched after writes;
- ledgers, run IDs, pending-export flags, writer locks, and recovery ceremonies;
- keeping old code beside new code after the new path owns the behavior.
- embedding hundreds of lines of prompt prose inside Python modules.
