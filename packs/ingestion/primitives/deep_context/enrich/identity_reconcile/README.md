# deep_context/enrich/identity_reconcile

The LinkedIn identity judge, its profile view, settlement, and guided
re-research policy. This package declares no pipeline node and has no CLI:
research and the review app drive it.

Pipeline-wide context: [deep-context-pipeline.md](../../../../docs/deep-context-pipeline.md)
and the [deep-context skill](../../../../skills/deep-context/SKILL.md).

## Who judges

- Enrichment judges every mapped real LinkedIn without a human or valid
  machine verdict, including attached and researched links. Guided correction
  uses the same judge and settlement primitives.

## Invariant

Human-settled rows and rows with a valid machine verdict are skipped.
Empty or unreadable verdicts are retried.
