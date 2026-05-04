# count_candidates

Run a cheap count using the same public filter contract before a full search.

Use this to catch obviously over-broad or over-tight slices before retrieval.

Command:

```bash
python powerpacks/primitives/count_candidates/count_candidates.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --env-file .env \
  --write-state
```

The primitive reads `expand_search_request.output.role_search_filters`,
converts the schema payload into checked-in TurboPuffer filter fields, runs a
filter-only query, and records `position_rows` plus deduped `unique_people`.
