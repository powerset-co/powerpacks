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
4. `apply_prefilters`
5. `execute_role_search` (defaults: `--limit 0 --top-k 10000`; `limit=0`
   means keep the full retrieved frontier locally)
6. `hydrate_people`
6. optional LLM filter/rerank after explicit `approve llm`
7. `persist_search_results`

Approval is explicit:

```bash
python ... approve llm --ledger <ledger> --approval-id <id> --confirm
python ... continue --ledger <ledger>
```

Use `--search-only` to skip LLM spend and go straight to persistence.

Powerpacks local search is not constrained by a web response size. The default
retrieves and hydrates the full available frontier from the local run, writes it
to artifacts, and leaves paging/inspection to local result viewers or follow-up
queries.
