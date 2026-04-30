# execute_search_slice

Execute exactly one bounded role-search slice.

Rules:

- resolve raw company names before retrieval if needed
- preserve `hard_filters` and `prefilters` across slices unless the slice
  explicitly widens recall
- keep strict role slices separate from company-domain adjacency slices
- record tenure/date windows and adjacency mode in the slice output
- do not re-decompose raw prose
- do not merge slices here
- do not run expensive scoring

This primitive can call `execute_role_search` underneath.

Command:

```bash
python powerpacks/primitives/execute_search_slice/execute_search_slice.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --slice-id senior-ic \
  --env-file .env \
  --write-state
```

It executes one generated slice's `role_search_filters` with the same portable
TurboPuffer path as `execute_role_search`, preserving slice id, reason, and
knobs in task state.
