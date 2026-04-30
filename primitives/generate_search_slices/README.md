# generate_search_slices

Turn one decomposed request into 3-8 bounded retrieval slices.

Each slice must:

- explain why it exists
- validate against `schemas/role-search-filters.schema.json`
- vary title, geography, seniority, currentness, or company strictness only
  when there is a clear reason
- include adjacency slices only when `plan_adjacency_search` chose an
  adjacency mode or the user explicitly requested adjacent people
- preserve hard filters across every slice unless a slice explicitly explains
  why it relaxes them
- declare its knobs:
  - `title_strictness`
  - `geography_strictness`
  - `seniority_strictness`
  - `currentness`
  - `company_strictness`
  - `adjacency_mode`
  - `hard_filters`
  - `prefilters`
  - `count_first`
  - `candidate_limit`
  - `hydrate_limit`

This primitive should not score people.
