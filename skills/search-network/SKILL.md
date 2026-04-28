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
2. Decide the initial strategy.
3. Run one of:
   - direct role search
   - count then search
   - multi-slice retrieval
4. Assess the frontier.
5. Decide the next action.
6. Stop when the frontier is coherent enough to present or hydrate.

## Rules

- do not force slices when the query is already specific
- use counts and frontier feedback to decide whether to widen or narrow
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

## Primary Primitives

- `expand_search_request`
- `count_candidates`
- `execute_role_search`
- `generate_search_slices`
- `execute_search_slice`
- `merge_candidate_frontier`
- `plan_candidate_review`
- `hydrate_people`

## Source Of Truth

- `powerpacks/docs/search-surface.md`
- `powerpacks/docs/expand-execute.md`
- `powerpacks/docs/slice-planning.md`
- `powerpacks/docs/turbopuffer-contract.md`
- `powerpacks/schemas/decomposed-query.schema.json`
- `powerpacks/schemas/role-search-filters.schema.json`
- `powerpacks/schemas/search-slice.schema.json`
- `powerpacks/schemas/candidate-review-plan.schema.json`
