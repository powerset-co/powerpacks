# sales_nav_pipeline

Resumable orchestrator for Sales Nav MCP tool calls plus local file handoffs.

The script does **not** call MCP tools itself. It initializes/continues the local
run, emits `blocked_tool_call` JSON telling the harness/agent which MCP tool to
call, where to save the response, and which command to run next. The harness
should execute the tool through its native MCP layer, write the JSON response to
`save_response_to`, then run `continue_command`.

## Normal run

```bash
python packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py run \
  --query "VPs at Brookfield" \
  --set-id <set_id> \
  --search-plan-json .powerpacks/sales-nav/search-plan.json
```

`--search-args-json` still works for a single search. Prefer
`--search-plan-json` for robust searches that need multiple passes.

## Search plan shape

```json
{
  "score_criteria": "investment/endowment team",
  "queries": [
    {
      "id": "finance",
      "label": "parent company + finance function",
      "args": {"company_ids": [163348], "function_ids": ["10"]}
    },
    {
      "id": "company_only",
      "label": "parent company-only fallback",
      "args": {"company_ids": [163348]}
    },
    {
      "id": "past_company",
      "label": "past-company alumni fallback",
      "args": {"past_company_ids": [163348], "past_company_names": {"163348": "Dartmouth College"}}
    },
    {
      "id": "keyword_last",
      "label": "keyword fallback",
      "args": {"keywords": "Dartmouth College"}
    }
  ]
}
```

Put structured current-company, relaxed structured, and past-company searches
before any keyword-only fallback. Use keyword-only search last.

For every query the runner:

1. blocks for `sales_nav_search` with `persist_artifact=true`
2. blocks for `get_artifact(include_content=true)` and ingests full content
3. blocks for `enrich_extended_profiles` for currently loaded unenriched leads
4. reloads/ingests the enriched artifact
5. after all searches, resolves pending mutual member IDs through
   `sales_nav_resolve_member_ids` and exports `leads.csv` / `mutuals.csv`

Qualitative scoring is approval-gated:

```bash
python ... continue --ledger <ledger> --criteria "real estate exposure"
python ... approve llm --ledger <ledger> --approval-id <id> --confirm
python ... continue --ledger <ledger> --criteria "real estate exposure"
```

Useful flags:

- `--skip-enrich` — skip `enrich_extended_profiles`
- `--enrich-limit N` — max lead profiles to enrich per search artifact
- `--skip-mutual-url-resolution` — skip local mutual URL resolution
- `--resolve-mutuals-external` — allow paid external URL resolution; default is
  cache-only/free layers
