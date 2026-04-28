# Add Role Search

Install and maintain simple role-first people search.

## Intent

Support requests like:

- "who are software engineers in sf"
- "product managers in nyc"
- "staff data engineers at stripe"

## Rules

- call query decomposition first unless the user already provided a valid
  role-search filter payload
- use only the public V1 role filter contract
- prefer TurboPuffer MCP for candidate retrieval
- use Postgres only for hydration or follow-up details
- do not invent filter keys or operators

## Required Contract

- filter shape must validate against `role-search-filters.schema.json`
- location values must be strings
- company filters must use resolved `company_ids`, not raw names, once resolved
- `role_tracks` and `seniority_bands` must use allowed enum values only

## Primary Primitives

- `decompose_search_request`
- `resolve_companies`
- `count_candidates`
- `search_people_roles`
- `hydrate_people`
