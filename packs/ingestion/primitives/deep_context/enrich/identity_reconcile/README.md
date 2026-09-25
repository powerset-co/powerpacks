# deep_context/enrich/identity_reconcile

The attached-LinkedIn identity judge and its queue, settlement, and guided
re-research policy. This package declares no pipeline node and has no CLI:
the review app drives it.

Pipeline-wide context: [deep-context-pipeline.md](../../../../docs/deep-context-pipeline.md)
and the [deep-context skill](../../../../skills/deep-context/SKILL.md).

## Who judges attached links

- `healing.py`, run by `review/heal_review.py` at every `bin/deep-context review`
  boot (and `bin/deep-context heal`): re-fetches links the judge skipped for
  having no usable profile, re-judges those with content through `judge.py`,
  and settles dead links locally.
- `research_reconcile/judging.py` (enrichment) and `guided.py` (guided
  re-research): judge research-proposed LinkedIns through the same `judge.py`.

There is no standalone attached-link pass for now.

## Invariant

Human-settled rows are skipped — a machine verdict would be discarded — and
judgments key on their judge-input fingerprint, so unchanged evidence reuses
the stored verdict instead of re-billing.
