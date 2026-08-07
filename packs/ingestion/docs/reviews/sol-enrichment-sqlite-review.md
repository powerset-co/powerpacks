# Sol review: enrichment files into canonical SQLite

Status: implementation review
Reviewed: 2026-08-05
Branch: `codex/deep-context-sqlite-rewrite`

## Verdict

The clarified boundary is the right one:

```text
Parallel task group -> atomic files + one manifest -> explicit projector -> SQLite -> web API
```

Parallel output should remain durable, inspectable, and reusable on disk. SQLite
should contain the complete read projection needed by the web application plus
the exact artifact paths and byte fingerprints that prove where it came from.
The browser and HTTP handlers should never scan research directories, reconcile
manifest mtimes, or read `review.csv` to discover enrichment state.

The current branch has most of the table names needed for this shape, but it
does not implement the runtime boundary yet. `migration/legacy.py` is a one-time
bootstrap importer, not an ongoing enrichment projector. The existing
enrichment path still derives state from files, scans per-handle directories,
and writes candidate outcomes back to CSV.

The current Deep Context HTTP surface is frozen. This rewrite is an internal
storage cutover, not an API redesign: every existing endpoint, request body,
response field, status value, next-action value, and SSE behavior must remain
compatible.

## P0 findings

### P0.1 There is no post-enrichment projector

`deep_research_contacts.py` writes per-handle `00_parallel_raw.json` and
`01_research_parallel.json`. `reconcile_deep_research.py` then rereads those
files, judges them, and writes proposals into the legacy override CSV.
`enrichment_contract.py` rereads the manifest, facts, verdicts, override CSV,
and per-handle output existence on every state derivation.

That leaves the webserver coupled to the entire file model even after SQLite
exists. Add one explicit primitive, for example:

```python
project_enrichment(db: Db, manifest_path: Path) -> ProjectionResult
```

The producer calls it only after durable file writes. It must also be callable
directly as the repair/retry operation after a crash. It is the only ongoing
file-to-SQL boundary for enrichment; it is not part of server boot, a GET
handler, or a view query.

### P0.2 The current research projection cannot prove what it ingested

The current `research` row has `dir_path`, a nullable `fingerprint`, and a full
`result_json`. The legacy importer tries to read
`result.metadata.fingerprint`, but the transformed Parallel payload written by
`parallel_to_research_json` does not contain that field. In current artifacts,
the imported fingerprint is therefore normally NULL.

The row needs the exact transformed artifact path and its SHA-256. The raw
provider artifact path and SHA-256 should also be recorded when present. A
directory path and file existence are not evidence that the SQL projection is
current. File mtimes must not participate in this contract.

### P0.3 One research subject can own several child identities

The research queue already carries `source_person_ids` as a JSON array, and one
canonical parent can contain several child people. `research.person_id` and
`links.person_id` are singular, so the current schema loses this relation.

Add a normalized relation such as `candidate_people(row_key, person_id)` (and,
if useful to the producer independently, `research_people(handle, person_id)`).
The identity queue and sibling settlement must join through that relation.
Do not reconstruct membership by decoding handles, slugs, or JSON arrays in
Python.

### P0.4 File completion is not atomic or self-describing enough

The shared `write_json` used by the Parallel primitive writes directly to the
final path. The stage currently has `manifest.json`, `_manifest.json`, and
`_taskgroup.json`, while reuse is based only on the existence of
`01_research_parallel.json`. A crash can leave a malformed or semantically
incomplete file that is subsequently treated as paid-cache completion.

For this stage:

- write each JSON artifact to a sibling temporary file and `os.replace` it;
- write one fixed stage `manifest.json` last for each durable transition;
- list the successful handle, relative artifact paths, and SHA-256 values in
  that manifest;
- list failed handles explicitly, without inventing placeholder outputs;
- treat an existing result as reusable only after parsing and validating it,
  not merely because the path exists.

Parallel's external task-group identifier may be retained inside the one fixed
manifest as provider resume/provenance data. It must not become a Powerpacks
run ID, a directory key, or a second ledger.

### P0.5 Machine projection must not overwrite human decisions

Reprojecting a changed or repaired research file may update machine-owned
profile fields, evidence, proposed URL/public identifier, confidence, and judge
projection. It must never replace a human worth, identity, synthetic-gate, or
guidance decision.

The separate `decisions` table is a good foundation for this. Enforce the rule
in the projector's domain upserts and with foreign keys/domain checks. Do not
encode the protection as source-string precedence scattered across callers.

### P0.6 Queryable verdict state belongs on the candidate

The rewrite mandate rejects a second queryable verdict truth. The current
schema still has a separate `verdicts` table while `links` also contains the
queryable judge fields. Preserve the raw verdict artifact path/fingerprint for
audit and paid-cache reuse, but project the effective verdict, confidence,
reason, recommendation, and judge-input fingerprint onto the candidate row.

The projector should upsert the candidate and its people memberships in the
same transaction as its research evidence. It must not call the legacy
`upsert_retargets(...review.csv...)` path.

### P0.7 Web progress still derives from disk

`derive_enrichment_state` currently reads the enrichment receipt, verdicts,
review CSV, facts, and per-handle files. That makes a small SQL-backed webserver
impossible.

Store the current enrichment selection, approval, progress counts, terminal
status, manifest path/fingerprint, and error in a typed `jobs`/stage row. The
projector updates that row after the corresponding manifest is durable. Web
status becomes one SQL read plus the existing response-shape adapter.

## Minimal relational design

Keep the current table names where possible. This is an evolution, not a new
storage subsystem.

### `research`

One row per stable enrichment handle:

```text
handle                       PRIMARY KEY
parent_id                    REFERENCES parents(parent_id)
source_candidate_key         REFERENCES links(row_key), nullable
status                       validated vocabulary
result_path                  exact 01_research_parallel.json path
result_sha256                64-char content hash
raw_path                     exact 00_parallel_raw.json path, nullable
raw_sha256                   64-char content hash, nullable
profile_json                 transformed profile needed by SQL/web hydration
linkedin_url                 normalized query projection, nullable
public_identifier            normalized query projection, nullable
display_name                 query projection, nullable
identity_confidence          REAL, nullable
researched_at                source timestamp, nullable
projected_at                 projection timestamp
```

`profile_json` is an intentional SQLite read projection, not the raw paid
artifact. The raw provider response remains on disk. If every consumer can be
served from narrower columns, the JSON may later shrink; the webserver must not
fall back to opening the research file.

### `candidate_people`

```text
row_key                      REFERENCES links(row_key)
person_id                    REFERENCES people(person_id)
PRIMARY KEY (row_key, person_id)
```

This is the queryable many-to-many relation required by parent settlement and
by `source_person_ids`. If `research_people` would merely duplicate the same
membership, do not add it: resolve research through `source_candidate_key`.

### `links`

Retain the current candidate table and add only the evidence relation/projection
it lacks:

```text
research_handle              REFERENCES research(handle), nullable
artifact_sha256              hash backing this machine proposal, nullable
machine_verdict              validated vocabulary, nullable
machine_recommendation       nullable
```

The existing proposed target, confidence, reason, `llm_reject*`, and
`llm_judge_fingerprint` columns can remain if their meanings are pinned. Do not
store the same effective verdict in both `links` and `verdicts`.

### `jobs`

Use one stable row such as `name='enrich'`, not one row per execution:

```text
name                         PRIMARY KEY
status                       existing enrichment status vocabulary
selection_sha256             current worth-selection fingerprint
review_revision              current review revision
total / completed / failed   INTEGER counts
would_submit                 INTEGER
estimated_usd                REAL
approved_budget_usd          REAL, nullable
approval_selection_sha256    nullable
manifest_path                exact fixed manifest path
manifest_sha256              content hash
error                        nullable
started_at / finished_at     nullable
```

The existing JSON progress column can remain for provider-specific diagnostic
detail, but public state and approval checks should use typed columns. Keep
human stage handoff in the existing typed stage state rather than duplicating
it here.

## Projector transaction contract

The projector has two phases.

### 1. Parse and validate outside the transaction

1. Read the explicit `manifest_path`; never discover a manifest by globbing.
2. Require a supported manifest shape and known status.
3. Resolve listed artifact paths relative to the fixed enrichment directory;
   reject path traversal or outputs outside that directory.
4. For every listed successful handle, require the declared files, compute
   SHA-256, compare it with the manifest, parse JSON once into frozen typed
   values, normalize LinkedIn identifiers with the canonical schema helper,
   and validate all referenced parent/person/candidate keys.
5. Build immutable `ResearchProjection`, `CandidateProjection`, membership,
   and `EnrichmentJobProjection` values. No SQL has changed yet.

For a running/approval heartbeat, only the fixed manifest/job projection is
required. Terminal projection additionally requires the per-handle artifacts.

### 2. Apply one SQLite transaction

In one transaction:

1. verify the approved selection/budget row when the manifest reports paid
   submission;
2. upsert every validated `research` row by handle;
3. replace that candidate's machine-owned fields and membership rows;
4. preserve all human decision rows unchanged;
5. upsert synthetic-profile machine data when the terminal free assembly step
   produced it, again preserving its human gate;
6. update the single enrichment job/stage row last, including manifest hash and
   terminal counts.

If any database write or referential check fails, roll back the entire manifest
snapshot. Do not expose `status=completed` with only part of that snapshot in
SQLite.

The producer ordering is always:

```text
write/replace artifacts -> write/replace manifest -> project -> return success
```

## Failure and idempotency semantics

- **Crash before manifest replacement:** SQLite remains at the previous valid
  snapshot. Temporary files are ignored.
- **Crash after manifest replacement but before projection:** files are safely
  ahead of SQLite. Rerun the explicit projector; the webserver does not scan to
  heal this gap.
- **Crash after SQLite commit:** rerunning the projector with identical hashes
  is a no-op and returns `projected=0`.
- **Malformed, missing, or hash-mismatched successful artifact:** reject the
  projection and leave SQLite unchanged. Marking the job failed requires a
  separate durable failed-manifest write followed by projection.
- **`completed_with_errors`:** the manifest must identify successful and failed
  handles. Project the validated successful subset atomically and persist the
  existing partial/failure status and counts; never fabricate failed profiles.
  A retry may add the missing handles without re-billing successful ones.
- **Changed bytes at the same stable path:** update machine projections only
  when the new manifest declares the new SHA-256. Human decisions remain
  unchanged. An unmanifested file edit has no runtime effect.
- **Stale worth selection:** retain the paid evidence and projection, but mark
  the job snapshot non-current using the existing selection/review-revision
  comparison. SQL queue policy, not file deletion, decides whether the
  candidate is visible.
- **Old files not listed by the current manifest:** ignore them. Do not delete
  paid artifacts and do not surface them by directory scan.

## Frozen HTTP/API compatibility

No endpoint should be renamed, removed, or narrowed during this rewrite. Pin
the current contracts before replacing internals, including at least:

- `GET /api/status`, `/api/enrichment`, `/api/worth-card`,
  `/api/linkedin-card`, `/api/retargets`, `/api/dossier`,
  `/api/person`, `/api/avatar`, `/api/events`, `/directory`, and `/healthz`;
- `POST /worth`, `/decide`, `/retarget`, `/approve-enrichment`, `/complete`,
  `/feedback`, and `/auth/login`;
- the existing SSE sequence/nudge behavior;
- enrichment statuses such as `not_started`, `stale`, `needs_approval`,
  `running`, `submitted`, `research_complete`, `completed`, `failed`, and
  `completed_with_errors`;
- current `counts`, `selection`, approval, progress, `next_action`, and error
  fields.

Implement a thin compatibility adapter that renders those exact response
shapes from SQL rows. For example, `GET /api/enrichment` may continue to return
the normalized receipt shape and `POST /approve-enrichment` may continue to
return `{"ok": true, "enrichment": ...}`; only their storage implementation
changes. Golden request/response fixtures should compare old and new handlers
byte-for-byte after normalizing explicitly volatile timestamps.

## P1 findings

### P1.1 Separate provider progress from product policy without a second store

The fixed manifest may contain provider-specific counts and task-group
provenance. The projector maps those into the stable product status vocabulary
already expected by the API. Do not teach HTTP handlers Parallel-specific
states, and do not create an in-memory job registry that becomes another truth.

### P1.2 Projection should consume a manifest inventory, not a directory

A terminal manifest should enumerate its outputs. This makes projection O(the
current task-group result set), deterministic, and testable. The explicit
projector is allowed to read those named files; the runtime webserver is not.

### P1.3 Synthetic assembly needs the same boundary

`assemble_synthetic_profile.py` currently rescans research dirs and writes
`synthetic-people.csv`. Keep a compatibility export for downstream consumers,
but project synthetic payloads and gates into SQLite before the stage reports
complete. Future reruns may replace machine-owned synthetic payloads; explicit
yes/no gates remain terminal.

### P1.4 Exact artifact paths should be validated at write time

Store normalized paths relative to the Deep Context root where practical, and
reject path escape. This keeps copied local stores usable while preventing a
manifest from pointing the webserver at an arbitrary file.

## Required tests

Add focused synthetic tests; do not fold them into the legacy 8k-line suite.

1. Producer writes valid raw/transformed files, hashes, then manifest.
2. Projector hydrates research, candidate, memberships, and job status in one
   transaction.
3. Reprojecting identical bytes is a no-op.
4. Changed artifact bytes update only machine fields.
5. Human identity/worth/synthetic decisions survive reprojection.
6. Missing, malformed, path-escaping, and hash-mismatched files roll back.
7. A multi-child research subject creates queryable membership rows.
8. `completed_with_errors` projects only declared successes and preserves
   failed counts/status.
9. A crash-gap fixture (manifest present, DB stale) is repaired by the explicit
   projector without a paid call.
10. Unlisted stale files never appear in SQL queues.
11. Stale selection retains evidence but does not claim current completion.
12. Legacy import computes real content hashes and produces the same rows as
    the ongoing projector for equivalent artifacts.
13. Every frozen GET/POST endpoint matches the existing response contract.
14. SSE still nudges clients to resnapshot the same `/api/status` shape.
15. A full approved enrichment fixture writes files, projects SQL, restarts the
    server, and serves the same state without opening any stage files.

For the last test, patch file-open/glob operations in the web package to fail
after startup. All web API reads must still pass from SQLite; only exact
dossier/avatar asset delivery explicitly authorized by their SQL path may open
the referenced asset.

## LOC implications

A reasonable implementation budget is:

- projector and typed boundary parsing: 180-260 Python lines;
- schema/domain upsert additions: 60-100 lines;
- producer integration and atomic manifest inventory: 60-100 lines;
- SQL-to-existing-API compatibility adapter: 80-140 lines;
- focused tests: roughly 20-30 test methods.

This should replace, not accompany, the disk-derived parts of
`enrichment_contract.py`, the research-directory scan in the web path, and the
CSV proposal writes. The net production LOC should fall. If the implementation
adds a projector while retaining disk derivation as a fallback, it has missed
the single-store objective.

## Recommended implementation order

1. Pin frozen endpoint/request/response/status fixtures from the current app.
2. Extend `research`, `links`, `jobs`, and candidate membership constraints.
3. Make Parallel artifact and manifest writes atomic and inventory hashes.
4. Implement and unit-test the explicit idempotent projector.
5. Call it from the enrichment pipeline after every durable status transition
   and after terminal artifact writes.
6. Switch the existing API implementations to the SQL compatibility adapter.
7. Move synthetic assembly and retarget proposal writes onto domain upserts.
8. Delete disk-derived web state and CSV runtime fallbacks.
9. Run real copied-store parity and prove that no GET handler scans stage files.
