# execute_role_search

Execute one validated role-search payload against the selected search backend.

The standard `$search` path calls this primitive directly from
`search_network_pipeline.py` after set/company/education resolution and
prefiltering. Deep search sends each approved candidate-archetype probe through
that same pipeline. It does not normally route through `execute_search_slice`;
that primitive remains only for legacy task-state compatibility.

Inputs come from `role_search_filters` in task state or `--payload-json`. The
supported fields are defined by
[`role-search-filters.schema.json`](../../schemas/role-search-filters.schema.json)
and the [TurboPuffer query contract](../../docs/turbopuffer-contract.md). Do not
invent attributes or operators from the physical index schema.

Execution behavior:

- resolve names to canonical IDs before retrieval when a field requires it;
- apply education and technical-skill base-person prefilters before role search;
- interpret position date bounds as overlapping employment windows;
- keep strict role intent separate from explicitly requested adjacency; and
- deduplicate position-level hits to base people before writing the retrieval
  artifact and `candidate_ids`.

Direct diagnostic use:

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/execute_role_search/execute_role_search.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --limit 0 \
  --top-k 10000 \
  --write-state
```

`--top-k` controls backend retrieval depth per channel or batch. `--limit`
controls unique people retained after retrieval; `--limit 0` keeps the full
retrieved frontier.
