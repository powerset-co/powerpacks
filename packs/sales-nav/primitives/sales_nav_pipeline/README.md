# sales_nav_pipeline

Resumable orchestrator for Sales Nav local files plus MCP handoffs.

The script cannot invoke host MCP tools directly. It initializes/continues the
local run, emits `blocked_mcp_call` JSON telling the agent which MCP tool to call
and where to save the response, then continues after the response path is passed
back.

```bash
python packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py run \
  --query "VPs at Brookfield" \
  --set-id <set_id> \
  --search-args-json .powerpacks/sales-nav/search-args.json
```

Typical block:

```json
{
  "status": "blocked_mcp_call",
  "mcp_tool": "sales_nav_search",
  "mcp_args": {"persist_artifact": true, "count": 25},
  "save_response_to": ".powerpacks/.../sales-nav-search.response.json",
  "continue_command": "python ... continue --response <path> --prefer-content"
}
```

Qualitative scoring is approval-gated:

```bash
python ... continue --ledger <ledger> --criteria "real estate exposure"
python ... approve llm --ledger <ledger> --approval-id <id> --confirm
python ... continue --ledger <ledger> --criteria "real estate exposure"
```
