# sales_nav_pipeline

Resumable orchestrator for Sales Nav local files plus harness tool-call handoffs.

The script does **not** call MCP tools itself. It initializes/continues the local
run, emits `blocked_tool_call` JSON telling the harness/agent which MCP tool to
call, where to save the response, and which command to run next. The harness
should execute the tool through its native MCP layer, write the JSON response to
`save_response_to`, then run `continue_command`.

```bash
python packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py run \
  --query "VPs at Brookfield" \
  --set-id <set_id> \
  --search-args-json .powerpacks/sales-nav/search-args.json
```

Typical block:

```json
{
  "status": "blocked_tool_call",
  "tool_server": "powerset-search",
  "tool_name": "sales_nav_search",
  "tool_args": {"persist_artifact": true, "count": 25},
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
