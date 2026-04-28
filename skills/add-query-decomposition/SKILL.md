# Add Query Decomposition

Install and maintain the expand phase for search requests.

## Intent

Convert one of these inputs:

- a natural-language search query
- a job description
- a URL with role or company context

into a normalized search plan and a schema-valid retrieval seed payload.

## Rules

- always decompose before searching when the user starts with free text
- do not jump straight into TurboPuffer filters from raw prose
- output both:
  - a normalized query summary
  - a schema-valid retrieval seed payload
- keep V1 narrow:
  - `people_by_role`
- make seniority and geography explicit
- output planning notes when the query is open-ended or can benefit from
  multiple slices
- support recall-style constraints:
  - education
  - years of experience
  - age
  - tenure/date windows
- support company-side parity constraints inside role search:
  - headcount
  - funding
  - valuation
  - founded year
  - sector/entity types
  - company geography

## Required Outputs

- `intent_type`
- `source_type`
- `normalized_query`
- `vertical`
- `role_search_filters`
- optional `company_names`
- optional `notes`

## Schema Source Of Truth

- `powerpacks/schemas/decomposed-query.schema.json`
- `powerpacks/schemas/role-search-filters.schema.json`

If a field cannot be grounded in the schema, omit it.
