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

Fresh company resolution only: do not scan, discover, reuse, or resume prior
company search artifacts, state files, CSVs, JSONL, or manifests. Resolve the
company set from the user's current request every time.

1. Decompose the request into the `role-search-filters` company fields only:
   `company_names`, `company_semantic_queries`, `entity_types`, `sector_types`,
   funding/headcount filters, company geography, `investor_names`, and
   resolved `investors`. Preserve `set_id` when the user provides one.
2. Resolve set scoping before company resolution. If the user did not provide a
   `set_id`, run `resolve_set_operators` without `--set-id` so it inherits
   `POWERPACKS_DEFAULT_SET_ID` / `POWERSET_DEFAULT_SET_ID`, or falls back to the
   logged-in operator's active personal set. Copy the returned `operator_ids`
   into the company resolver payload. Do not pass the raw set UUID as
   `allowed_operator_ids`.
3. If `investor_names` are present, run `resolve_investors` first and record or
   copy the returned `investor_urns` into `investors`.
4. Run `resolve_companies`.
5. Present the returned company count, sample companies, set scope, and the company IDs
   artifact/state path when available.
6. If the user wants people at those companies, pass `company_ids` and
   `operator_ids` into
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
uv run --project powerpacks python powerpacks/packs/search/primitives/resolve_set_operators/resolve_set_operators.py \
  --payload-json '<json>' \
  --env-file .env
```

```bash
uv run --project powerpacks python powerpacks/packs/search/primitives/resolve_investors/resolve_investors.py \
  --payload-json '<json>' \
  --env-file .env
```

```bash
uv run --project powerpacks python powerpacks/packs/search/primitives/turbopuffer/turbopuffer_resolve_companies.py \
  --payload-json '<json>' \
  --env-file .env
```
