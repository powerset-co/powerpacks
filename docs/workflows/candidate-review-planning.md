# Candidate Review Planning

Plan candidate-frontier review after direct or sliced retrieval.

## Intent

Review a merged frontier without jumping straight to expensive scoring.

## Rules

- reason about slices, not just one global count
- report per-slice yield and overlap
- recommend a concrete next action
- keep any suggested shortlist small enough to inspect
- do not mutate the retrieval contract
- do not run expensive scoring in V1

## Primary Primitives

- `merge_candidate_frontier`
- `plan_candidate_review`
- `hydrate_people`
