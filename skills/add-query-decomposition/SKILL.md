# Add Query Decomposition

Install and maintain the decomposition workflow for search requests.

## Intent

Convert one of these inputs:

- a natural-language search query
- a job description
- a URL with role or company context

into a normalized search plan and vertical-specific filters.

## Rules

- always decompose before searching when the user starts with free text
- do not jump straight into TurboPuffer filters from raw prose
- output both:
  - a normalized query summary
  - a schema-valid filter payload per vertical
- keep V1 narrow:
  - `people_by_role`
  - `people_by_company`

## Required Outputs

- `intent_type`
- `source_type`
- `normalized_query`
- `verticals`
- `role_search_filters`
- `company_search_filters`

## Schema Source Of Truth

- `powerpacks/schemas/decomposed-query.schema.json`
- `powerpacks/schemas/role-search-filters.schema.json`
- `powerpacks/schemas/company-search-filters.schema.json`

If a field cannot be grounded in the schema, omit it.
