# generate_search_slices

Turn one decomposed request into 3-8 bounded retrieval slices.

Each slice must:

- explain why it exists
- validate against `schemas/role-search-filters.schema.json`
- vary title, geography, seniority, currentness, or company strictness only
  when there is a clear reason

This primitive should not score people.
