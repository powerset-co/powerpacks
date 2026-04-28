---
name: search-network
description: Run a role-first people search from a natural-language query, job description, or URL. Use when the user wants the agent to decompose the request, choose a search strategy, retrieve candidates from TurboPuffer, review the frontier, and hydrate the best results without using expensive scoring.
---

# Search Network

Run the full Powerpacks search loop from one user request such as:

- `$search-network who are software engineers in sf`
- `$search-network senior engineers at series a fintech companies`
- `$search-network stanford engineers with 3-5 yoe in new york`

## Intent

Give the user one operational entrypoint that does the work end to end.

The agent should:

- decompose the request into the Powerpacks role-search schema
- decide whether to search directly, count first, or generate slices
- execute retrieval through TurboPuffer
- review the candidate frontier after each step
- hydrate only the best frontier through Postgres

## Strategy Loop

1. Expand the request with `expand_search_request`.
2. Choose the initial strategy with `decide_search_strategy`.
3. Run one of:
   - direct role search
   - count then search
   - multi-slice retrieval
4. Assess the frontier with `assess_frontier`.
5. Decide the next action with `plan_candidate_review`.
6. Stop when the frontier is coherent enough to present or hydrate.

## Rules

- do not force slices when the query is already specific
- use counts and frontier feedback to decide whether to widen or narrow
- produce a short decision trace after each stage
- keep role, location, seniority, education, yoe, age, and company constraints
  explicit
- use only documented filter fields, operators, and enum values
- resolve raw company names before execute-time company filtering
- preserve slice reasons and provenance when slices are used
- do not run expensive scoring in V1

## Decision Heuristics

- use direct search when the query is already narrow and explicit
- use count-then-search when the query is clear but maybe broad
- use slices when one query likely misses good adjacent title or geography
  variants
- stop and present when the frontier is already coherent
- hydrate only a bounded shortlist

## Decision Trace

For every run, keep a concise trace with:

- expanded constraints
- strategy decision and reason
- retrieval path used
- counts or per-slice yield
- frontier assessment
- next action and reason

The trace is part of the result. It lets the user inspect why the claw searched
that way.

## Primary Primitives

- `expand_search_request`
- `decide_search_strategy`
- `count_candidates`
- `execute_role_search`
- `generate_search_slices`
- `execute_search_slice`
- `merge_candidate_frontier`
- `assess_frontier`
- `plan_candidate_review`
- `hydrate_people`

## Helper References

The installed user-facing skill is `search-network`. Do not require the user to
invoke helper skills directly.

When needed, consult these reference files in the installed repo:

- `powerpacks/skills/add-query-decomposition/SKILL.md`
- `powerpacks/skills/add-role-search/SKILL.md`
- `powerpacks/skills/add-slice-search/SKILL.md`
- `powerpacks/skills/add-candidate-review-planning/SKILL.md`
- `powerpacks/skills/add-turbopuffer-schema-guard/SKILL.md`
- `powerpacks/skills/add-postgres-hydration/SKILL.md`

## Source Of Truth

- `powerpacks/docs/search-surface.md`
- `powerpacks/docs/expand-execute.md`
- `powerpacks/docs/slice-planning.md`
- `powerpacks/docs/turbopuffer-contract.md`
- `powerpacks/schemas/decomposed-query.schema.json`
- `powerpacks/schemas/role-search-filters.schema.json`
- `powerpacks/schemas/search-slice.schema.json`
- `powerpacks/schemas/search-strategy-decision.schema.json`
- `powerpacks/schemas/frontier-assessment.schema.json`
- `powerpacks/schemas/candidate-review-plan.schema.json`
