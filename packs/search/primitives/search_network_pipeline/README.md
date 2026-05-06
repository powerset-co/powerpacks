# search_network_pipeline

Resumable orchestrator for the mechanical part of `search-network`.

This starts **after query extraction**. Provide either an existing task `--state`
or `--query` plus `--payload-json` containing the `expand_search_request` output.
Natural-language decomposition remains a skill/LLM step.

```bash
python packs/search/primitives/search_network_pipeline/search_network_pipeline.py run \
  --query "software engineers in sf" \
  --payload-json .powerpacks/search/payload.json
```

The runner executes:

1. `task_state init` + record `expand_search_request` when needed
2. `resolve_set_operators`
3. relevant ID resolvers (`resolve_companies`, `resolve_education`, `resolve_investors`)
4. `execute_role_search`
5. `hydrate_people`
6. optional LLM filter/rerank after explicit `approve llm`
7. `persist_search_results`

Approval is explicit:

```bash
python ... approve llm --ledger <ledger> --approval-id <id> --confirm
python ... continue --ledger <ledger>
```

Use `--search-only` to skip LLM spend and go straight to persistence.
