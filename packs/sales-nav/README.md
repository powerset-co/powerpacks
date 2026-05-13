# Sales Nav Pack

`packs/sales-nav` exposes a Sales Navigator search workflow on top of the
`powerset-search` MCP server. MCP tools perform the upstream Sales Nav calls;
local primitives normalize page responses into file-backed handoffs so agents
can pass paths instead of large lead/mutual payloads.

## Why a separate pack

Sales Nav lives in its own pack instead of under `packs/powerset` because:

- It is a workflow / skill, not part of the auth + MCP install plumbing
  that `packs/powerset` provides.
- Future Sales Nav skills (e.g. `import-sales-nav-leads` for lead →
  contact promotion) belong here, not under `packs/powerset`.
- Users may install `packs/sales-nav` without taking on every other
  Powerset workflow.

## Dependencies

This pack **depends on `packs/powerset`** for:

- `auth` — Auth0 PKCE login that mints the bearer token written into MCP host
  config.
- `mcp_install` — registers the remote `powerset-search` MCP with each
  host (Claude Code, Codex, nanoclaw).
- `doctor` — verifies the MCP is reachable and the token is fresh.

If those are not in place, the skill routes the user through
`$powerset login` first.

## Skill Surface

- `sales-nav-search` — Run a Sales Navigator search through the MCP.
  Resolves company / title filters, runs a resumable multi-query lead search
  with server-side artifact persistence on by default, enriches loaded leads,
  resolves mutual URLs locally, stores local `leads.jsonl` / `mutuals.jsonl`
  handoff files, and offers paginated retrieval via `get_artifact`. See
  [`skills/sales-nav-search/SKILL.md`](skills/sales-nav-search/SKILL.md).

## MCP Tools Used

The skill orchestrates these tools served by the remote MCP:

| Tool | Purpose |
| --- | --- |
| `sales_nav_resolve` | Translate human company / title strings to LinkedIn IDs. |
| `sales_nav_search` | Paginated lead search; persists an artifact when `persist_artifact=true`. |
| `get_artifact` | Page back through a persisted result set without re-running the search. |
| `enrich_extended_profiles` | Enrich selected lead member IDs with full profile fields and update the latest artifact. |
| `sales_nav_resolve_member_ids` | Resolve lead/mutual member IDs to LinkedIn URLs from cache/free layers by default. |

Tool descriptions, input schemas, and return shapes are owned by the
MCP server (`network-search-api/mcp_server/server.py`). The skill is the
agent-facing orchestration layer; it does not duplicate those contracts.

## Local primitives

| Primitive | Purpose |
| --- | --- |
| `sales_nav_pipeline` | Resumable orchestrator. Emits MCP `blocked_tool_call`s, ingests artifacts, enriches leads, resolves mutual URLs, exports CSVs, and approval-gates optional local scoring. |
| `sales_nav_artifacts` | Initialize a local run, ingest MCP page responses into `leads.jsonl` / `mutuals.jsonl`, merge member URL resolutions, export CSVs, and answer lookup queries against the files. |
| `score_sales_nav_leads` | Fan-out LLM scoring over a run's `leads.jsonl` + mutual context, writing matching leads to `scores/<criteria>/matches.csv`. |

## Defaults

- `persist_artifact: true` on every `sales_nav_search` call. Persistence
  is cheap and lets the agent page large result sets via `get_artifact`
  in a later turn.
- `count: 25` per page (LinkedIn cap).
- Do not loop unbounded. Use explicit multi-query search plans for known
  recall gaps (strict search + fallback search, one deliberate extra broad page,
  etc.); otherwise surface `next_start_offset` and ask before loading more.
- Prefer structured fallbacks before free text: current company, relaxed
  structured filters, past company, then keyword-only search last.

## Tasks

- [`tasks/sales-nav-search.task.json`](tasks/sales-nav-search.task.json)
  — orchestration manifest used by harnesses that drive the skill
  programmatically.
