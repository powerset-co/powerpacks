# execute_role_search

Execute a role search using the role vertical contract.

This is the underlying retrieval primitive. In the slice-planning flow, it
should usually be called through `execute_search_slice`.

Expected inputs:

- `semantic_query`
- optional `bm25_queries`
- optional location filters
- optional `company_ids`
- optional `company_semantic_queries`
- optional `education_ids`
- optional `degree_levels`
- optional `tech_skills`
- optional `hard_filters`
- optional `prefilters`
- optional `adjacency_mode`
- optional `company_adjacency_queries`
- optional `adjacent_role_ids`
- optional `adjacent_departments`
- optional `adjacent_seniority`
- optional `seniority_bands`
- optional `role_tracks`
- optional `years_experience_min`
- optional `years_experience_max`
- optional `age_min`
- optional `age_max`
- optional `position_after_date`
- optional `position_before_date`

Execution notes:

- Education and tech skills are prefilters: resolve them to base candidate IDs
  before role retrieval, then search roles inside that narrowed set.
- Position dates are overlap windows. For example, Box between 2019 and 2022
  means roles whose start/end dates overlapped that period.
- Company-domain adjacency should be a separate planned mode, not silently mixed
  into strict role search.

Command:

```bash
python powerpacks/primitives/execute_role_search/execute_role_search.py \
  --state .powerpacks/runs/search-network-<id>.json \
  --env-file .env \
  --limit 0 \
  --top-k 10000 \
  --write-state
```

The primitive reads `expand_search_request.output.role_search_filters`, validates
field/operator usage against the checked-in TurboPuffer contract, embeds the
dense `semantic_query` for hybrid searches, runs BM25 + vector retrieval or
filter-only retrieval, dedupes to base person IDs, writes a retrieval artifact,
and records `candidate_ids` in task state.

`--top-k` controls TurboPuffer retrieval depth per channel/batch. `--limit`
controls the number of unique people kept after retrieval; `--limit 0` means no
post-retrieval cap, which is the local Powerpacks default so the full frontier is
available in artifacts for paging/inspection.
