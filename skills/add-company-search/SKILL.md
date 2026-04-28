# Add Company Search

Install and maintain simple company-first people search.

## Intent

Support requests like:

- "engineers at AI startups"
- "founders at series a fintech companies"
- "operators at developer tools companies in sf"

## Rules

- decompose free text first
- use company criteria to find company IDs or company candidate sets
- then search people with company filters plus optional role/location filters
- keep the public contract limited to the V1 company filter schema

## Required Contract

- filter shape must validate against `company-search-filters.schema.json`
- `entity_types`, `sector_types`, and funding-stage values must come from the
  allowed public vocabulary
- `company_ids` are preferred over raw names once resolution has happened

## Primary Primitives

- `decompose_search_request`
- `resolve_companies`
- `count_candidates`
- `search_people_company_signals`
- `search_people_roles`
- `hydrate_people`
