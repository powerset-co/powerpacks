# Sales Nav Pack

`packs/sales-nav` is a thin pack that exposes a Sales Navigator search
workflow on top of the `powerset-search` MCP server. It owns no local
primitives — every action happens by the MCP host (Claude Code, Codex,
nanoclaw) calling tools registered through `packs/powerset`.

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

- `auth` — Auth0 PKCE login that mints the bearer token used by the MCP
  server (`POWERPACKS_POWERSET_TOKEN`).
- `mcp_install` — registers the remote `powerset-search` MCP with each
  host (Claude Code, Codex, nanoclaw).
- `doctor` — verifies the MCP is reachable and the token is fresh.

If those are not in place, the skill routes the user through
`$powerset-login` first.

## Skill Surface

- `sales-nav-search` — Run a Sales Navigator search through the MCP.
  Resolves company / title filters, runs a paginated lead search with
  server-side artifact persistence on by default, and offers paginated
  retrieval via `get_artifact`. See
  [`skills/sales-nav-search/SKILL.md`](skills/sales-nav-search/SKILL.md).

## MCP Tools Used

The skill orchestrates three tools served by the remote MCP:

| Tool | Purpose |
| --- | --- |
| `sales_nav_resolve` | Translate human company / title strings to LinkedIn IDs. |
| `sales_nav_search` | Paginated lead search; persists an artifact when `persist_artifact=true`. |
| `get_artifact` | Page back through a persisted result set without re-running the search. |

Tool descriptions, input schemas, and return shapes are owned by the
MCP server (`network-search-api/mcp_server/server.py`). The skill is the
agent-facing orchestration layer; it does not duplicate those contracts.

## Defaults

- `persist_artifact: true` on every `sales_nav_search` call. Persistence
  is cheap and lets the agent page large result sets via `get_artifact`
  in a later turn.
- `count: 25` per page (LinkedIn cap).
- Loop on `next_start_offset` until `has_more` is false.

## Tasks

- [`tasks/sales-nav-search.task.json`](tasks/sales-nav-search.task.json)
  — orchestration manifest used by harnesses that drive the skill
  programmatically.
