# Sol review: Deep Context runtime deletion map

Reviewed: 2026-08-05

Snapshot: `cfe92a09` plus the in-progress SQLite web cutover visible in the
shared worktree. This inventory lists production code consumers only; tests and
documentation are intentionally excluded.

## Bottom line

The canonical SQLite package is sufficient to replace the obsolete runtime
store. Do not replace that foundation. Finish the cutover into
`db/{schema,store,views,projectors,batons,legacy}.py`, then delete the seven
modules below in one zero-stale-reference pass.

The seven files are 4,339 production lines. All 4,339 lines are gross-deletable
once their real callers use named SQLite reads/domain transactions or explicit
baton export. The likely net reduction is roughly 3,500-3,900 lines because a
small guided-retarget runner and a few presentation helpers still need a home;
the current SQLite adapter already absorbs most old model/workflow behavior.

The current web cutover is meaningful but incomplete. `server.py` now opens one
canonical `Db`, uses `SqliteReviewAdapter`, passes that same `Db` to enrichment,
and performs worth/identity clicks through DB domain methods. It still imports
the old `model.py` helpers and the entire file-backed `retarget_queue.py`, while
`sqlite_adapter.py` still imports `model.summarize`. Outside the server, the
pipeline still has many direct CSV/old-review-SQL consumers.

## Real runtime consumer matrix

| Obsolete module | Production LOC | Real production consumers | SQLite replacement and deletion gate |
| --- | ---: | --- | --- |
| `deep_context/review_db.py` | 744 | `apply_retargets.py`, `heal_review.py`, `reconcile_linkedin.py`, `restart_review.py`, `synthesize_person_context.py`, and `worth_view.py` call `commit_review_rows`; the module itself imports `review_store.py` | Replace whole-store commits with the specific projector/domain write that owns the changed columns. Compatibility output is only `Db.export_batons(...)`; runtime must never write CSV and then mirror it into a second SQLite file. Delete after no caller imports `commit_review_rows` or `ReviewDb`. |
| `deep_context/review_store.py` | 418 | `common/legacy.py`, `apply_retargets.py`, `assemble_synthetic_profile.py`, `enrichment_contract.py`, `heal_review.py`, `migrate_legacy_resolutions.py`, `reconcile_deep_research.py`, `reconcile_linkedin.py`, `restart_review.py`, `synthesize_person_context.py`, `worth_view.py`, `review_db.py`, `review_web/workflow.py`, and `review_web/retarget_queue.py` | Enums/constants live in `db/schema.py`; legacy CSV parsing/writing lives only in `db/batons.py`; current decisions use `Db.set_worth`, `settle_identity`, and reset methods; policy predicates become named SQL views. Delete when ordinary runtime has no `load_override_rows`/`write_override_rows` path. |
| `deep_context/worth_view.py` | 503 | `assemble_synthetic_profile.py`, `build_parents.py`, `reconcile_deep_research.py`, and `review_web/model.py` | Use `db.views.worth_rows`, `worth_queue`, `worth_counts`, and `stage_progress`. Facts/projectors populate machine worth; `db/legacy.py` performs the one-time child-to-parent human migration. `build_parents` must project parent membership/worth, not synchronize `parent-worth:*` CSV rows. |
| `review_web/model.py` | 1,216 | `common/legacy.py`, `assemble_synthetic_profile.py`, `heal_review.py`, `prefetch_profiles.py`, `reconcile_deep_research.py`, the executable facade `reconcile_review_web.py`, and `review_web/{cli,decisions,rendering,retarget_queue,server,sqlite_adapter,workflow}.py` | Parent/candidate hydration comes from `db.views.all_parents`, `linkedin_queue`, `person_detail`, `directory`, and artifact-path reads. Move only tiny presentation-only helpers such as primary-candidate selection beside rendering. Replace `summarize(parents)` in the adapter with `stage_progress`/named counts. Path constants move to `common.py` or the owning producer. |
| `review_web/decisions.py` | 309 | `common/legacy.py`, `heal_review.py`, and `review_web/retarget_queue.py` | Worth and identity decisions are `Db` transactions. Synthetic gate effects belong in `settle_identity`/`reset_identity` and `synthetic_profiles`; downstream CSV mutations happen only in `export_batons`. Old decision replay belongs solely in `db.legacy.import_legacy`. |
| `review_web/workflow.py` | 464 | `prefetch_profiles.py`, `reconcile_deep_research.py`, the executable facade `reconcile_review_web.py`, and `review_web/rendering.py` | Selection, pending candidates, progress, enrichment state, and stage completion come from `db.views.{worth_queue,linkedin_queue,stage_progress,enrichment_state,review_state}` plus typed `stage_state`, `jobs`, and `spend_approvals`. Rendering consumes hydrated rows; it does not rebuild policy from files. |
| `review_web/retarget_queue.py` | 685 | `review_web/server.py` | POST `/retarget` writes `guidance` plus a durable `jobs` row. A small separate in-process job function calls the existing paid primitive, durably writes artifacts, calls `project_manifest`, and settles the result through the same `Db`. Direct pasted URLs can settle immediately. Replace the in-memory queue/file mutation machinery; job/status reads use `db.views.retarget_snapshot`. |

Total gross deletion: **4,339 production lines**.

## Caller changes that are not optional

The imports above expose four distinct cutover classes.

1. **Machine producers must project, not commit a reconstructed store.**
   `reconcile_linkedin.py`, `synthesize_person_context.py`, `heal_review.py`, and
   the retarget/research proposal path currently load a whole override map and
   call `commit_review_rows`. Each producer should upsert only its owned machine
   columns/artifact projection. Human decision columns remain untouched.

2. **Downstream realization reads an explicit export, not runtime CSV.**
   `apply_retargets.py`, any reviewed-identity persistence primitive, and fan-in
   keep their boundary files, but those files are produced immediately before
   the downstream handoff with `Db.export_batons`. They must not remain live
   alternate stores.

3. **Read-only workers query the same DB.** `assemble_synthetic_profile.py`,
   `prefetch_profiles.py`, `enrichment_contract.py`, and
   `reconcile_deep_research.py` should receive an explicit `Db` and use the
   named worth/LinkedIn/enrichment reads. They should not accept an override-map
   fallback in normal Deep Context execution.

4. **Legacy cleanup is bootstrap-only.**
   `common/legacy.resolve_stored_identity_policy` lazily imports
   `review_store`, `model`, and `decisions`, and `heal_review.py` calls it on
   ordinary runs. Absorb the required historical semantics into
   `db.legacy.import_legacy` and remove that runtime scrub. The supported
   process is old files -> one fresh canonical DB, never continuous dual-store
   repair.

## Canonical enrichment `Db` plumbing

There are exactly two production construction surfaces for
`ReconcileDeepResearch`.

- `review_web/server.py:203` and `:214` construct the free and paid jobs. The
  current web cutover already passes the handler's bootstrapped `db=db`. Keep
  this; do not reopen a database inside the worker.
- `reconcile_deep_research.py:1198` is the canonical CLI construction reached
  by `bin/deep-context reconcile-deep-research`. It still omits `db`. The CLI
  must open `.powerpacks/deep-context/deep-context.sqlite`, fail clearly if it
  is missing/unsupported, and pass that `Db` to the node. `bin/deep-context`
  itself needs no storage logic.

`deep_research_contacts.py` is the lower-level file-first producer. Its
`ResearchRunParams.db=None` behavior is correct for an isolated primitive call;
the canonical Deep Context node already passes its existing `Db` into both
`ResearchRunParams` constructions. Do not make the low-level primitive discover
or open the canonical store implicitly.

The guided-retarget job in `review_web/server.py` is the other enrichment
handoff: it must retain the same handler-owned `Db`, project the completed
manifest, then perform any identity settlement. It must not write review CSV or
teach a GET/POST handler to parse research output.

## Disjoint implementation slices

Freeze the DB API first. After that, slices B-D can run in parallel because
their production file ownership does not overlap. Slice E is the integration
deletion pass.

| Slice | Exclusive production ownership | Result | Depends on |
| --- | --- | --- | --- |
| A. Finish/freeze SQLite contract | `deep_context/db/{schema,store,views,projectors,batons,legacy}.py` and DB tests | Named reads/writes, artifact projection, durable guidance/jobs, exact explicit export. Finish the active DB/web work before assigning other agents changes here. | Current active web cutover |
| B. Identity producer/realization cutover | `reconcile_linkedin.py`, `heal_review.py`, `apply_retargets.py`, `restart_review.py`, `migrate_legacy_resolutions.py`, `synthesize_person_context.py`, `primitives/common/legacy.py` | No whole-store commits, runtime legacy scrub, or direct decision CSV writes; explicit export at downstream handoff. | A |
| C. Worth/enrichment worker cutover | `build_parents.py`, `assemble_synthetic_profile.py`, `enrichment_contract.py`, `prefetch_profiles.py`, `reconcile_deep_research.py` | Named SQL reads/projectors; canonical CLI passes `Db`; no `worth_view`, `model`, or `workflow` imports. | A |
| D. Web cutover and job shrink | `review_web/{server,sqlite_adapter,cli,rendering,model,decisions,workflow,retarget_queue}.py` and `reconcile_review_web.py` | Frozen routes over SQLite only; durable guided jobs; tiny presentation helpers local to rendering; delete the four obsolete `review_web` modules in this slice. | A; serialize with the currently active server owner |
| E. Final deletion/export proof | `review_db.py`, `review_store.py`, `worth_view.py`, affected package exports/entrypoints | Delete remaining obsolete top-level modules, run zero-stale-reference grep, export batons, and diff downstream realize/directory output. | B, C, D |

Required order is `A -> (B || C || D) -> E`. Do not partially delete the old
modules while a slice still imports them, and do not add compatibility
re-exports to make an intermediate state look complete.

## Completion checks

- `rg` finds no production import of `review_db`, `review_store`, `worth_view`,
  `review_web.model`, `review_web.decisions`, `review_web.workflow`, or
  `review_web.retarget_queue`.
- Web boot and `review-status` open SQLite only; they do not stat/glob review,
  facts, research, synthetic, or manifest files to derive state.
- Free, approved-paid, reuse, failure, and zero-work enrichment receipts are
  projected using the exact handler/CLI-owned `Db`.
- A fresh legacy import followed by explicit export reproduces required baton
  rows byte-for-byte or records every deliberate policy delta.
- Worth/LinkedIn queue keys, sibling/ghost/synthetic settlement, guided job
  restart state, and downstream realized people/directory output pass the
  pinned parity gates before the seven files are removed.
