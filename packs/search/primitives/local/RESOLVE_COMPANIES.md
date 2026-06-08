# resolve_companies

Resolve raw company names or company descriptions into canonical company IDs and
company filter objects before per-slice person search.

Use before `apply_prefilters` and role search when a query contains company
names, funding/headcount constraints, sectors, investors, or company geography.

For company-domain intent, provide `company_semantic_queries`. If `sector_types`
are also present, choose a `company_sector_strategy`:

- `hard_filter`: semantic company search must also match the sector labels.
- `soft_union`: semantic company results are unioned with sector-filter matches.
- `semantic_only`: sector labels do not broaden or constrain semantic company search.
- `staged`: run `hard_filter` first, then broaden like `soft_union` when the
  hard semantic frontier is below `company_sector_min_results`.

Use `soft_union` when label quality is the bottleneck and recall matters. Use
`hard_filter` when the user explicitly wants the sector as a strict constraint.
Use `staged` when you want to start narrow and broaden only if the initial
company frontier is too small. Other hard filters such as company geography,
entity type, funding, headcount, and investor constraints still apply to the
sector branch.

Example:

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

When many company IDs are returned, pass them to `apply_prefilters` so people
search uses batched `company_id In [...]` filters.
