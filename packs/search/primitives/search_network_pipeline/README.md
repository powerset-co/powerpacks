# search_network_pipeline

Resumable orchestrator for the mechanical part of `search-network`.

This starts **after query extraction**. Provide either an existing task `--state`
or `--query` plus `--payload-json` containing the `expand_search_request` output.
Natural-language decomposition remains a skill/LLM step.

For the normal `search-network` happy path, call `prepare` first instead of
asking the harness to inspect docs or build its own extraction flow:

```bash
uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py prepare \
  --query "software engineers in sf"
```

`prepare` runs `expand_search_request`, writes `expand_search_request.json`,
emits a compact preview, and returns an `execute_command` to run after the user
chooses `execute`. For company-directory-only requests, `prepare` emits
`status: company_directory_fast_path` with the tool request to follow.

```bash
uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py run \
  --query "software engineers in sf" \
  --payload-json .powerpacks/search/payload.json
```

The normal user-facing flow has one approval gate before this runner: show the
extracted search preview and ask whether to modify or execute it. Once the user
chooses execute, call the runner with `--execute-approved`:

```bash
uv run --env-file .env --project . python packs/search/primitives/search_network_pipeline/search_network_pipeline.py run \
  --query "software engineers in sf" \
  --payload-json .powerpacks/search/payload.json \
  --execute-approved
```

The runner then executes without another gate:

1. `task_state init` + record `expand_search_request` when needed
2. `resolve_set_operators`
3. relevant ID resolvers (`resolve_companies`, `resolve_education`, `resolve_investors`)
4. `apply_prefilters`
5. `execute_role_search` (defaults: `--limit 0 --top-k 10000`; `limit=0`
   means keep the full retrieved frontier locally)
6. `hydrate_people`
7. `llm_filter_candidates`
8. `llm_rerank_candidates`
9. `persist_search_results`

Use `--search-only` only when the user explicitly wants retrieval/hydration
without LLM filtering/reranking. If `--execute-approved` is omitted and
`--search-only` is not set, the runner preserves the older explicit
`blocked_approval` gate for compatibility/tests.

`--seniority-bands senior,staff` (on `prepare` and `run`) pins canonical
seniority bands as a hard retrieval filter: the pinned bands REPLACE any
expansion-derived `role_search_filters.seniority_bands`, survive role
shortcuts (founder queries normally drop bands), and unknown band values fail
loudly. `prepare` applies the pin to the prepared payload and threads the flag
into the emitted `execute_command`; on `run` it requires a fresh
`--query`/`--payload-json` start (it cannot retroactively apply to an existing
`--state`). The `$search` deep-mode JD flow uses this to enforce the JD's
seniority band at retrieval on both the TurboPuffer and local DuckDB paths.

The orchestrator is intentionally Sales-Nav-like: it runs sub-primitives
quietly, stores compact step summaries in the ledger, records artifact paths,
and emits one compact JSON object on completion/block/status. Use
`status --ledger <ledger>` for a non-verbose progress summary.

When blocked, the runner records `current_block`, exits with code `20` for
approval, and emits a compact JSON payload with an `uv run --env-file .env ...`
`continue_command`. Agents should surface only `message`, `approval_id`,
`ledger`, and `continue_command` unless diagnosis is needed.

Powerpacks local search is not constrained by a web response size. The default
retrieves and hydrates the full available frontier from the local run, writes it
to artifacts, and leaves paging/inspection to local result viewers or follow-up
queries.
