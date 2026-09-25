# deep_context/enrich/identity_reconcile

The LinkedIn identity judge, its profile view, settlement, and guided
re-research policy. This package declares no pipeline node and has no CLI:
research and the review app drive it.

Pipeline-wide context: [deep-context-pipeline.md](../../../../docs/deep-context-pipeline.md)
and the [deep-context skill](../../../../skills/deep-context/SKILL.md).

## Who judges

- `research_reconcile/judging.py` (enrichment) and `guided.py` (guided
  re-research): judge research-proposed LinkedIns through `judge.py` and
  settle them through `results.upsert_retargets`.

Attached links are not machine-judged.

## Invariant

Human-settled rows are skipped — a machine verdict would be discarded — and
judgments key on their judge-input fingerprint, so unchanged evidence reuses
the stored verdict instead of re-billing.
