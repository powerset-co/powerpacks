# local_search_pipeline

Runs a search against `.powerpacks/search-index/local-search.duckdb` without
resolving Powerset sets, reading Postgres, or querying TurboPuffer.

The DuckDB file is the search scope. If a prepared payload contains remote
scope keys such as `set_id` or `operator_ids`, this pipeline records them for
traceability but the local backend ignores them.

Role searches use the local vectors by default when the payload includes a
semantic query. The DuckDB stays local, but generating the query embedding still
requires the normal OpenAI embedding credentials.

```bash
uv run --project . python packs/search/primitives/local_search_pipeline/local_search_pipeline.py run \
  --db .powerpacks/search-index/local-search.duckdb \
  --query "software engineers in sf that went to stanford" \
  --payload-json .powerpacks/search/query/expand_search_request.local.json
```
