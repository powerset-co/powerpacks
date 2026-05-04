---
name: sales-nav-search
description: Run a Sales Navigator search through the Powerset Search MCP. Resolves company / title filters to LinkedIn IDs, runs a paginated lead search with server-side artifact persistence on by default, and offers paginated retrieval via get_artifact.
---

# Sales Nav Search

Use this skill when the user asks for `/sales-nav-search <query>`,
"find LinkedIn leads at <company>", "show me sales nav results for <title>",
or any other Sales Navigator search request scoped to the user's set.

The skill is in its own pack (`packs/sales-nav`) but **depends on
`packs/powerset`** for Auth0 login and MCP install. If those aren't ready,
route the user to `$powerset-login` first.

It calls three MCP tools served by the remote `powerset-search` MCP at
`https://search-api-7wk4uhe77q-uw.a.run.app/mcp`:

- `sales_nav_resolve` — turn company names + titles into LinkedIn
  `company_ids` / canonical title strings
- `sales_nav_search` — paginated lead search scoped to the user's set;
  with `conversation_id` + `persist_artifact: true` the server writes a
  Supabase artifact row and returns an `artifact_id`
- `get_artifact` — paginated retrieval of a persisted artifact

The skill itself does not call HTTP — Claude Code / Codex invoke the MCP
tools directly once `mcp_install` (in `packs/powerset`) has registered the
server.

## Hard rules

- This skill **requires** the `powerset-search` MCP. If it is not
  registered, route the user through `$powerset-login` →
  `mcp_install install --host all`.
- Every Sales Nav call is **scoped to a `set_id`**. The MCP server enforces
  the set context. Do not invent or guess `set_id`s — pass the one the user
  specifies, or ask which set to search if it is not obvious from context.
- **Default to `persist_artifact: true` on every `sales_nav_search` call.**
  Persistence is cheap and lets the user (or the agent in a later turn) page
  large result sets via `get_artifact`. Only pass `persist_artifact: false`
  when the user explicitly asks for an ephemeral search.
- Persistence requires a `conversation_id`. See the conversation_id playbook
  below — without one, persistence is silently skipped server-side and you
  only get the inline `leads` list back.
- Lead profiles can include LinkedIn URLs and contact metadata. Treat them
  as sensitive — present in chat, do not dump to disk unless the user asks.
- Do not auto-page beyond what the user requested. `sales_nav_search`
  returns `has_more` and `next_start_offset`; surface them, don't loop.

## Prereqs

Run these once (or use `$powerset-login`'s `doctor fix --interactive` which
does both in one shot):

```bash
# 1. Auth (once per machine, refreshed automatically)
python powerpacks/packs/powerset/primitives/auth/auth.py login

# 2. Install the MCP into your host (Claude Code or Codex or both)
python powerpacks/packs/powerset/primitives/mcp_install/mcp_install.py install --host all
```

If on Codex, also export the bearer token env var that Codex reads at
runtime:

```bash
eval "$(python powerpacks/packs/powerset/primitives/mcp_install/mcp_install.py token-env)"
```

## conversation_id playbook

`conversation_id` is a Supabase `conversations.id`. The MCP server uses it
to scope artifact reads/writes to a specific conversation. Three ways to
get one:

1. **From search.powerset.dev** — copy the conversation id from the URL
   when the user has an open chat.
2. **REST API** — `POST /v2/conversations` with the user's bearer token
   returns `{ "id": "..." }`.
3. **Per-skill ephemeral session** — generate a UUID4 the first time you
   need one in the current skill invocation and reuse it for follow-ups
   in the same turn. The artifact rows still persist in Supabase keyed by
   that id; they just won't be linked to a webapp conversation.

Always pass the same `conversation_id` to subsequent `sales_nav_search`
calls and the matching `get_artifact` call.

## Workflow

### Step 0 — Confirm prereqs

```bash
python powerpacks/packs/powerset/primitives/mcp_install/mcp_install.py status --host all
```

If `installed: false` for the host the user is on, route to
`$powerset-login` first.

### Step 1 — Decide whether to resolve filters

If the user gave you free-text companies or titles, call
`sales_nav_resolve` first to convert them into stable IDs / canonical
strings:

```text
Tool: sales_nav_resolve
Args: {
  "set_id": "<the user's set>",
  "companies": ["Stripe", "Ramp"],
  "titles": ["staff engineer", "principal engineer"]
}
```

If the user already gave you a `company_id` or `function_id`, skip this
and go straight to step 2.

### Step 2 — Run the search (always with persistence)

```text
Tool: sales_nav_search
Args: {
  "set_id":         "<the user's set>",
  "conversation_id":"<UUID — see conversation_id playbook>",
  "persist_artifact": true,
  "company_ids":    [...],         // from sales_nav_resolve
  "title":          "staff engineer",
  "function_ids":   [...],
  "seniority_ids":  [...],
  "geography_ids":  [...],
  "headcount_ids":  [...],
  "keywords":       "distributed systems",
  "count":          25,            // max 25 per page
  "start_offset":   0
}
```

Response shape (with persistence):

```jsonc
{
  "artifact_id": "...",
  "artifact": { /* metadata */ },
  "total_count": 1342,
  "max_total_count": 2500,
  "results_returned": 25,
  "has_more": true,
  "next_start_offset": 25,
  "filters_used": { /* what the server actually filtered on */ },
  "reconnect_required": false,
  "leads": [
    {
      "name": "...",
      "headline": "...",
      "linkedin_url": "...",
      "company": "...",
      "location": "...",
      "mutual_count": 3,
      "mutual_member_ids": ["...", "..."]
    }
  ]
}
```

`reconnect_required: true` means the user's Unipile/LinkedIn session
needs upstream re-auth; tell the user and stop. Do not retry.

**Save the `artifact_id`** in your skill's working memory — every
follow-up retrieval (next page, mutual lookup, re-display) goes through
`get_artifact` against that id.

### Step 3 — Present the first page

- Lead with `total_count`, `results_returned`, and any non-zero `mutual`
  counts.
- For each lead present `name`, `headline`, `company`, `location`,
  `mutual_count`, and `linkedin_url`.
- Mention `artifact_id` to the user (they may want to reference it across
  turns / sessions).
- If `has_more`, ask if they want page N+1 — do **not** auto-fetch.

### Step 4 — Paginate via get_artifact

Subsequent pages of the same result set should go through
`get_artifact`, not a fresh `sales_nav_search`:

```text
Tool: get_artifact
Args: {
  "artifact_id":     "<from step 2>",
  "offset":          25,
  "limit":           25,
  "include_content": false   // true to also return full filters_used etc.
}
```

This avoids re-running an expensive Sales Nav fetch and keeps the
filters/result-set stable across pages.

### Step 5 — Refining filters

Refining filters is a **new search**, not pagination. Re-call
`sales_nav_search` with the new args, the same `conversation_id`, and
`persist_artifact: true`. You'll get back a new `artifact_id`. Tell the
user the artifact id changed.

| User says | What changes |
| --- | --- |
| "next page" | `get_artifact` with `offset += 25` |
| "narrow to NYC" | new `sales_nav_search` with `geography_ids` |
| "only senior+ folks" | new `sales_nav_search` with `seniority_ids` |
| "filter to mid-stage companies" | new `sales_nav_search` with `headcount_ids` |
| "show me their mutual" | use `mutual_member_ids` from the existing leads |

### Step 6 — Resuming a previous search

If the user references a prior `artifact_id` (from a previous session,
the web app, or a paste), skip directly to `get_artifact` — no
`sales_nav_search` needed.

## What this skill does NOT do

- It does not write leads to your local `contacts.csv`. Lead → contact
  promotion is a separate (future) `import-sales-nav-leads` skill, likely
  also under `packs/sales-nav`.
- It does not promote leads into the Powerset main set. That UI affordance
  is on the web app today.
- It does not run a sequence of pages on its own. Pagination is always
  user-confirmed and goes through `get_artifact`.
