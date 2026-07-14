# execute_search_slice

> **Legacy compatibility primitive.** Current standard search calls
> `execute_role_search` directly, and deep search uses contract-preserving
> candidate-archetype probes. Neither current path generates retrieval slices.

This CLI can replay one old `search-slice` task-state object through
`execute_role_search` while preserving its legacy slice ID and reason. Keep it
for old artifacts and focused compatibility tests; do not introduce it into new
search orchestration.

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/execute_search_slice/execute_search_slice.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --slice-id senior-ic \
  --write-state
```
