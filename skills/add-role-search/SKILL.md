# Add Role Search

Install and maintain the execute phase for simple role-first people search.

## Intent

Support requests like:

- "who are software engineers in sf"
- "product managers in nyc"
- "staff data engineers at stripe"
- "stanford engineers with 3-5 yoe"
- "founders under 35"
- "people who worked at meta after 2020"

## Rules

- call `expand_search_request` first unless the user already provided a valid
  role-search filter payload
- use only the public V1 role filter contract
- prefer TurboPuffer MCP for candidate retrieval
- use Postgres only for hydration or follow-up details
- do not invent filter keys or operators
- support recall-style filters when they are present in the payload

## Required Contract

- filter shape must validate against `role-search-filters.schema.json`
- location values must be strings
- company filters must use resolved `company_ids`, not raw names, once resolved
- `role_tracks` and `seniority_bands` must use allowed enum values only
- age and years-of-experience filters must stay numeric
- tenure/date filters must stay as date-like strings

## Primary Primitives

- `expand_search_request`
- `resolve_companies`
- `count_candidates`
- `execute_role_search`
- `hydrate_people`
