# deep_context/enrich/identity_reconcile — `deep_reconcile`

`deep_reconcile` (`ReconcileLinkedin`, `reconcile_linkedin.py`) runs the
SQLite-selected attached-LinkedIn identity judge. It is the one declared node in
this package (the surrounding `queue`, `judge`, `settlement`, and `guided`
modules are policy/helpers it drives).

Pipeline-wide context: [deep-context-pipeline.md](../../../../docs/deep-context-pipeline.md)
and the [deep-context skill](../../../../skills/deep-context/SKILL.md).

| node | reads | writes | manifest |
|---|---|---|---|
| `deep_reconcile` | — (reads SQLite queues/verdicts) | — (writes SQLite identity verdicts; no file outputs) | `deep-context/reconcile/manifest.json` (`ReconcileLinkedinManifest`) |

## Manifest / status

`ReconcileLinkedinManifest` reports `parents`, `tasks`, `judged`, `reused`
(verdicts answered from the store, unchanged input), `human_settled`,
`conflicts_*`, `errors`, `needs_review`, `deep_research_eligible`.
Status values: `completed`, `needs_approval` (the spend gate — carries the
estimate in `needs_approval`), plus the template `not_ready` / `failed`.

## Control

Paid — bills the OpenAI identity judge. A normal run stops at
`needs_approval` until the caller passes `--approve-spend`; `--dry-run` prints
the same pre-flight estimate and `--reapply` replays already-paid verdicts, so
neither needs approval.

## Invariant

Human-settled rows are skipped (`human_settled`) — a machine verdict would be
discarded — and judgments key on their judge-input fingerprint, so unchanged
evidence reuses the stored verdict instead of re-billing.
