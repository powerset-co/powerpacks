# count_candidates

Run a cheap filter-only count against the checked-in backend contract.

This is a diagnostic and evaluation primitive. It is not a stage in the
standard `$search` happy path, and a broad result does not trigger slice
planning. Use it when an operator or eval explicitly needs population size
before retrieval.

```bash
uv run --env-file .env --project . python \
  packs/search/primitives/count_candidates/count_candidates.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --write-state
```

The primitive reads `expand_search_request.output.role_search_filters`, maps the
payload to checked-in TurboPuffer filters, and records position-row and
deduplicated-person counts.
