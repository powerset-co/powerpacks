# Search Network Flow

> **Legacy V1 design reference.** Primitive notes may remain useful, but this
> is not the current `$search` lifecycle. See the canonical
> [`$search` architecture](search-architecture.md) and executable
> [`$search` skill](../skills/search/SKILL.md).

Powerpacks V1 should expose one role-first search flow with explicit planning
between expansion and review.

## Step 1: `expand_search_request`

Input:

- natural-language query
- job description
- URL

Output:

- normalized query
- optional raw company names
- role-search filter seed payload
- optional adjacency request
- execution hint that this should go through `people_by_role`
- optional planning notes about seniority, geography, or company constraints

The expand step should be able to extract:

- role/title intent
- location
- company names
- company attributes such as headcount, funding stage, sector, and company geography
- seniority
- education
- years of experience
- age
- tenure/date constraints
- adjacent/domain intent

## Step 2: `decide_search_strategy`

Use the expanded request to decide between:

- `direct_execute`
- `count_then_execute`
- `generate_slices`
- `ask_for_clarification`

Do not force slices when the query is already narrow and explicit.

## Step 3: `plan_adjacency_search`

Use this when the query has domain intent or the user asks for adjacent people.

Output:

- whether adjacency is off, requires confirmation, or should be included
- whether adjacency is title-only or company-domain based
- company semantic queries, sector/entity filters, or adjacency queries

Examples:

- "infra engineers" can mean strict infrastructure titles
- "adjacent infra people" means include company-domain adjacency
- "engineers at infra companies" means company-domain adjacency by company
  domain, not title-only matching

## Step 4: `generate_search_slices`

Input:

- decomposed query payload

Output:

- 3-8 bounded retrieval slices
- explicit reason for each slice
- one schema-valid role-search payload per slice
- explicit adjacency, hard-filter expression, and prefilter plan

This step is optional.

## Step 5: `execute_search_slice`

Input:

- one schema-valid slice payload

Optional pre-step:

- resolve company names to `company_ids`

Output:

- candidate IDs
- slice-local counts
- retrieval summary

Direct search can skip slicing and execute the role payload directly.

## Step 6: `hydrate_people`

Hydrate the full candidate frontier before LLM filtering. Hydration should use
canonical Postgres/Supabase profile context keyed by base person IDs. Do not
hydrate only the first visible page when the next stage needs to decide who to
kick out.

## Step 7: `llm_filter_candidates`

Run the conservative LLM pre-screen only after hydration. This uses the
`result_filter` prompt from the Aleph pipeline:

- score 0.0-1.0
- keep score >= 0.3 by default
- include borderline candidates
- record filtered people with score and reason
- do not treat this as expensive scoring or final ranking

Tenure/date filters use overlap semantics. "At Box between 2019 and 2022" means
the matched position overlaps that period; it is not restricted to positions
that started inside the period.

Filters should be represented as executable expressions. Education, tech-skill,
social, interaction, and large-company-intersection constraints may also require
a prefilter plan that computes/intersects base IDs before role retrieval. Keep
those expressions and prefilter plans in all slices unless the plan explicitly
says a diagnostic widening slice should relax them.

## Step 8: `merge_candidate_frontier`

Input:

- completed slice results

Output:

- deduped frontier
- slice provenance
- overlap summary
- per-slice yield

## Step 9: `assess_frontier`

Input:

- direct result or merged slice frontier
- counts and overlap
- decomposition context

Output:

- frontier size
- whether the frontier is too broad, too narrow, or coherent
- recommended review path
- reasons

## Step 10: `plan_candidate_review`

Input:

- merged frontier
- per-slice counts and overlap
- decomposition context

Output:

- recommended next action
- suggested shortlist size
- reasons and notes

After candidate review, hydrate the frontier, optionally run the conservative
LLM filter, then persist results. Expensive candidate scoring is deferred to a
separate primitive later.

## Why This Split

- it mirrors your existing `expand` / `execute` endpoint model
- it gives the claw a real strategy decision instead of forcing slicing
- it allows multiple targeted retrieval passes instead of one giant search
- it gives the claw an explicit planning trace before hydration or review
- it avoids making retrieval logic guess at raw prose
- it keeps the public search contract small
- it removes summary and company-signal branches from the initial surface
