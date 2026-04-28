# Expand / Execute

Powerpacks V1 should expose one simple search flow:

## Step 1: `expand_search_request`

Input:

- natural-language query
- job description
- URL

Output:

- normalized query
- optional raw company names
- role-search filter payload
- execution hint that this should go through `people_by_role`

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

## Step 2: `execute_role_search`

Input:

- schema-valid role-search payload

Optional pre-step:

- resolve company names to `company_ids`

Output:

- candidate IDs
- result summary
- hydrated profiles

## Why This Split

- it mirrors your existing `expand` / `execute` endpoint model
- it avoids making retrieval logic guess at raw prose
- it keeps the public search contract small
- it removes summary and company-signal branches from the initial surface
