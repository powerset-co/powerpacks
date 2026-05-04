# Role Search

Define the retrieval phase for role-first people search.

## Intent

Support requests like:

- "who are software engineers in sf"
- "product managers in nyc"
- "staff data engineers at stripe"
- "stanford engineers with 3-5 yoe"
- "founders under 35"
- "people who worked at meta after 2020"
- "people at box between 2019 and 2022"
- "adjacent infra people"
- "senior engineers at series a fintech companies"
- "operators at developer tools companies with 50-200 employees"

## Rules

- call `expand_search_request` first unless the user already provided a valid
  role-search filter payload
- generate multiple bounded slices before broad retrieval when the query is
  open-ended
- use only the public V1 role filter contract
- use packaged primitives for candidate retrieval; MCP is optional exploratory
  tooling, not the execution path
- use Postgres only for hydration or follow-up details
- do not invent filter keys or operators
- support recall-style filters when they are present in the payload
- support company-side parity filters when they are present in the payload
- resolve school names with `resolve_education`
- resolve company names and company-attribute sets with `resolve_companies`
- run `apply_prefilters` before count/search when hard constraints produce
  base IDs
- record hard filters as executable expressions before retrieval
- treat base-ID prefilters as narrowing stages before role retrieval
- treat tenure/date filters as overlapping-position windows
- use company-domain adjacency only when explicitly requested, confirmed, or
  recorded as a separate exploratory slice
- do not run expensive scoring in V1

## Required Contract

- filter shape must validate against `role-search-filters.schema.json`
- location values must be strings
- company filters must use resolved `company_ids`, not raw names, once resolved
- `role_tracks` and `seniority_bands` must use allowed enum values only
- age and years-of-experience filters must stay numeric
- tenure/date filters must stay as date-like strings
- tenure windows mean the position overlaps the date range
- funding, valuation, and headcount filters must stay numeric
- founded-year filters must stay integer year values

## Primary Primitives

- `expand_search_request`
- `generate_search_slices`
- `resolve_education`
- `resolve_companies`
- `apply_prefilters`
- `count_candidates`
- `execute_search_slice`
- `merge_candidate_frontier`
- `plan_candidate_review`
- `hydrate_people`
