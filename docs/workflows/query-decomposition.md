# Query Decomposition

Define the expand phase for search requests.

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
- write `role_search_filters.semantic_query` as 2-3 sentences of dense
  retrieval prose about the target person's work, responsibilities, and
  relevant profile. Do not use a bare title phrase. Bare titles and aliases
  belong in `bm25_queries`.
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
  - explicit adjacent/domain-adjacent intent
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
- optional `adjacency_request`
- optional `notes`

## Semantic Query Standard

`semantic_query` should describe what the target person does: responsibilities,
skills, scope, domain, and profile signals that matter for embedding retrieval.
It should not restate the title, company, school, location, or hard filters.
Use 2-3 concise natural-language sentences when there is meaningful role or
domain intent.

Use `bm25_queries` for title aliases, acronyms, and lexical variants. If you
need examples, consult `powerpacks/docs/semantic-query-examples.md`; do not
copy an example unless it matches the user's intent.

## Schema Source Of Truth

- `powerpacks/schemas/decomposed-query.schema.json`
- `powerpacks/schemas/role-search-filters.schema.json`

If a field cannot be grounded in the schema, omit it.
