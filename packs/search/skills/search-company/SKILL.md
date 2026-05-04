---
name: search-company
description: Resolve company names, company descriptions, investor/funding filters, or vertical queries into canonical TurboPuffer company IDs for downstream people search.
---

# Search Company

Use this when the user asks for company search directly, or when they want the
company set behind a people search inspected first.

Examples:

- `/search-company database companies`
- `/search-company series b fintech startups in california`
- `/search-company companies backed by sequoia`

## Workflow

1. Decompose the request into the `role-search-filters` company fields only:
   `company_names`, `company_semantic_queries`, `entity_types`, `sector_types`,
   funding/headcount filters, company geography, `investor_names`, and
   resolved `investors`.
2. If `investor_names` are present, run `resolve_investors` first and record or
   copy the returned `investor_urns` into `investors`.
3. Run `resolve_companies`.
4. Present the returned company count, sample companies, and the company IDs
   artifact/state path when available.
5. If the user wants people at those companies, pass `company_ids` into
   `search-network`; for large company sets, use `apply_prefilters` so people
   search batches company IDs instead of sending one giant `company_id In [...]`.

## Query Guidance

Use `company_semantic_queries` for vertical/domain intent, not just
`sector_types`. Coarse sectors are useful, but the resolver needs an explicit
sector strategy:

- Use `company_sector_strategy: "staged"` by default for ambiguous domain
  searches. This starts with sector-intersected semantic search and broadens if
  the hard frontier is too small.
- Use `company_sector_strategy: "soft_union"` when recall matters more than
  precision or labels are known to be incomplete. This matches semantic results
  OR sector-filter matches.
- Use `company_sector_strategy: "hard_filter"` when the user explicitly wants
  sector labels as a strict constraint.
- Use `company_sector_strategy: "semantic_only"` when sector labels are too
  noisy for the requested company domain.

Good:

```json
{
  "company_semantic_queries": [
    "Companies building database systems, hosted databases, data storage engines, database infrastructure, SQL or NoSQL databases, or developer platforms for managing application data."
  ],
  "sector_types": ["data"],
  "company_sector_strategy": "staged",
  "company_sector_min_results": 500
}
```

Bad:

```json
{ "sector_types": ["data"] }
```

## Commands

```bash
python powerpacks/packs/search/primitives/resolve_investors/resolve_investors.py \
  --payload-json '<json>' \
  --env-file .env
```

```bash
python powerpacks/packs/search/primitives/resolve_companies/resolve_companies.py \
  --payload-json '<json>' \
  --env-file .env
```
