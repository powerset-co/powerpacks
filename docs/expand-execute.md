# Search Network Flow

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

## Step 2: `decide_search_strategy`

Use the expanded request to decide between:

- `direct_execute`
- `count_then_execute`
- `generate_slices`
- `ask_for_clarification`

Do not force slices when the query is already narrow and explicit.

## Step 3: `generate_search_slices`

Input:

- decomposed query payload

Output:

- 3-8 bounded retrieval slices
- explicit reason for each slice
- one schema-valid role-search payload per slice

This step is optional.

## Step 4: `execute_search_slice`

Input:

- one schema-valid slice payload

Optional pre-step:

- resolve company names to `company_ids`

Output:

- candidate IDs
- slice-local counts
- retrieval summary

Direct search can skip slicing and execute the role payload directly.

## Step 5: `merge_candidate_frontier`

Input:

- completed slice results

Output:

- deduped frontier
- slice provenance
- overlap summary
- per-slice yield

## Step 6: `assess_frontier`

Input:

- direct result or merged slice frontier
- counts and overlap
- decomposition context

Output:

- frontier size
- whether the frontier is too broad, too narrow, or coherent
- recommended review path
- reasons

## Step 7: `plan_candidate_review`

Input:

- merged frontier
- per-slice counts and overlap
- decomposition context

Output:

- recommended next action
- suggested shortlist size
- reasons and notes

Powerpacks V1 stops here. Expensive candidate scoring is deferred to a separate
primitive later.

## Why This Split

- it mirrors your existing `expand` / `execute` endpoint model
- it gives the claw a real strategy decision instead of forcing slicing
- it allows multiple targeted retrieval passes instead of one giant search
- it gives the claw an explicit planning trace before hydration or review
- it avoids making retrieval logic guess at raw prose
- it keeps the public search contract small
- it removes summary and company-signal branches from the initial surface
