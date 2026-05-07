# sales_nav_pipeline

Resumable orchestrator for Sales Nav local files.

The runner calls the remote `powerset-search` MCP streamable HTTP endpoint
directly with the cached `$powerset login` bearer token. It saves raw responses
under the run's `pages/` directory, ingests them into `leads.jsonl` /
`mutuals.jsonl`, exports CSVs, and tracks progress in `pipeline.json`.

```bash
python packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py run \
  --query "VPs at Brookfield" \
  --set-id <set_id> \
  --search-args-json .powerpacks/sales-nav/search-args.json
```

`--search-args-json` is the JSON object passed to `sales_nav_search` in addition
to `set_id`, `conversation_id`, `persist_artifact: true`, and `count`.

Use `--require-enriched` to call `enrich_extended_profiles` for visible leads and
then re-ingest full artifact content.

Manual response ingestion remains available for debugging/backfill:

```bash
python ... continue --ledger <ledger> --response saved-mcp-response.json --prefer-content
```

Qualitative scoring is approval-gated:

```bash
python ... continue --ledger <ledger> --criteria "real estate exposure"
python ... approve llm --ledger <ledger> --approval-id <id> --confirm
python ... continue --ledger <ledger> --criteria "real estate exposure"
```
