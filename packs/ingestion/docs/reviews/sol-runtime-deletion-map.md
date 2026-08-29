# Deep Context SQLite rewrite: standing architecture

Reviewed: 2026-08-05

This is the maintenance contract for the completed Deep Context SQLite
cutover. Git history is the archive for the removed file-backed runtime.

## Non-negotiable shape

- SQLite is the only runtime authority for people, parents, worth, identity
  links, facts, research, guidance, and review decisions.
- Files remain the provider and downstream handoff boundary. Enrichment writes
  raw and normalized results first, then projects their receipt into SQLite.
  Review CSVs are explicit one-way exports, never a second live store.
- `migration/legacy.py` is the sole old-install adapter: old artifacts are imported
  once into a fresh database. Ordinary stages do not synchronize two stores or
  repair legacy state.
- SQL for the canonical domain lives only in `deep_context/db`. Workflow code
  calls typed store methods or named views.
- External message-store SQL lives with the owning discovery client:
  `discover/messages/chatdb.py`, `discover/messages/wacli/*_db.py`, and
  `discover/gmail/msgvault/*_db.py`. Import, Deep Context, and Logbook shape
  results from those shared readers rather than copying store policy.
- Public CLI and HTTP contracts stay stable. Implementation packages expose no
  compatibility re-exports; callers import concrete definitions from their
  owning modules.
- Prompts and response schemas are assets under `deep_context/prompts/` or
  `deep_context/synthesis/`, so prompt LOC and prompt cache versions are visible
  independently from orchestration code.
- Imports stay at module scope. The import-hygiene test rejects nested imports,
  canonical SQL outside `db`, and expansion of the approved DB operation set.

## Runtime flow

```text
LinkedIn/Gmail/iMessage/WhatsApp imports
                  |
                  v
shared source readers -> person context files
                  |
                  v
synthesis -> fact JSONL -> SQLite facts/worth
                  |
                  v
dossier -> merge candidates -> canonical parents
                  |
                  v
SQLite worth eligibility query
                  |
                  v
Parallel SDK -> raw + normalized research files
                  |
                  v
shared evidence judge -> deterministic threshold
                  |
        +---------+----------+
        |                    |
 accepted real link    synthetic fallback
        |                    |
        +---------+----------+
                  |
                  v
SQLite review projection -> explicit downstream export
```

Synthetic fallback remains required when no real candidate survives the shared
judge. Guided retarget uses the same dossier plus guidance, the same normalized
research loader, and the same evidence judge as ordinary research. A human-
pasted LinkedIn URL remains a direct human decision.

## Ownership map

| Concern | Concrete home |
| --- | --- |
| Schema, typed rows, transactions | `deep_context/db/{schema,models,store}.py` |
| Named workflow reads | `deep_context/db/views.py` |
| Stage and artifact projection | `deep_context/db/projectors.py` |
| Explicit CSV snapshots/exports | `deep_context/db/{snapshots,batons}.py` |
| One-time old-install import | `deep_context/migration/legacy.py` |
| Apple Messages store policy | `discover/messages/chatdb.py` |
| WhatsApp store policy | `discover/messages/wacli/{store_db,message_db,depth_db}.py` |
| Gmail store policy | `discover/gmail/msgvault/{store,aggregation,context_db,logbook_db}.py` |
| Synthesis selection/prompt/calls | `deep_context/synthesis/` |
| Dossier rendering | `deep_context/synthesis/` |
| Parent graph/render/projection | `deep_context/merge_candidates/` and dated `deep_context/migration/` |
| Merge blocking/judging/receipts | `deep_context/merge_candidates/` |
| Attached-link reconciliation | `deep_context/enrich/identity_reconcile/` |
| Parallel provider | `deep_context/enrich/parallel_research/` |
| Research result reconciliation | `deep_context/enrich/research_reconcile/` |
| Shared evidence policy | `identity_evidence.py`, `dossier_evidence.py`, `research_result.py` |
| Enrichment receipt ownership | `enrichment_receipt.py` |

The top-level stage files are stable CLI/in-process entry points. They parse
arguments, construct typed inputs, call the concrete modules, emit the stage
payload, and map status to an exit code.

## Cost and reuse boundaries

- Synthesis spends through the OpenAI Responses API and writes fact JSONL before
  projecting SQLite. A changed synthesis schema changes its cache version.
- Same-person merge judgment spends only for uncached ambiguous pairs after
  deterministic blocking.
- Parallel research uses the official Parallel SDK task-group API. Its exact
  dossier plus guidance fingerprint controls paid-output reuse.
- LinkedIn evidence judgment reuses the same judge and deterministic thresholds
  for attached links, ordinary research, and guided research.
- RapidAPI/profile-cache policy remains local to the few consumers whose
  freshness behavior genuinely differs; it is not hidden behind a misleading
  universal wrapper.

## Deletions that must stay deleted

The file-backed review stack (`review_db.py`, `review_store.py`,
`worth_view.py`, and the old web model/decision/workflow/retarget queue) is gone.
The standalone legacy-resolution command is also gone; current installation
migration is only `migrate-sqlite` through `migration/legacy.py`. Do not restore an old
module, registry, ledger, dual-write path, or package re-export to ease a move.
Update real call sites and tests instead.

## Required validation after structural changes

1. Run the focused shared-store, DB, enrichment, and frozen HTTP contract tests.
2. Run the full repository suite with the repo virtual environment first on
   `PATH`, because subprocess tests invoke `python3`.
3. Run Ruff on every changed/new Python file, `compileall`,
   `bash -n bin/deep-context`, and `git diff --check`.
4. Prove zero nested imports and zero canonical SQLite access outside
   `deep_context/db`.
5. For source-reader changes, run import, Deep Context collection, and Logbook
   against real local stores into temporary output directories and compare
   normalized or byte-identical artifacts with the prior implementation.
6. For prompt/model changes, run a bounded paid comparison into a temporary DB;
   never overwrite canonical facts or human decisions during evaluation.
