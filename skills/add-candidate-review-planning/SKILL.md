---
name: add-candidate-review-planning
description: Install and maintain candidate-frontier review planning after slice execution. Use when the agent should explain how many candidates each slice produced, what overlap exists, and whether to narrow, widen, hydrate, or stop without running expensive scoring.
---

# Add Candidate Review Planning

Install and maintain candidate-frontier review planning after slice execution.

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
