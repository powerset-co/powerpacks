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
route the user to `$powerset setup` first.

It calls these MCP tools served by the remote `powerset-search` MCP configured in
`POWERPACKS_MCP_URL`:

- `sales_nav_resolve` — turn company names + titles into LinkedIn
  `company_ids` / canonical title strings
- `sales_nav_search` — paginated lead search scoped to the user's set;
  with `conversation_id` + `persist_artifact: true` the server writes a
  Supabase artifact row and returns an `artifact_id`
- `get_artifact` — paginated retrieval of a persisted artifact
- `enrich_extended_profiles` — enrich selected lead member IDs with profile
  fields, then persist them back to the latest Sales Nav artifact
- `sales_nav_resolve_member_ids` — resolve mutual member IDs to LinkedIn URLs
  through cache/free layers by default

The skill itself does not call HTTP — Claude Code / Codex invoke the MCP
tools directly once `mcp_install` (in `packs/powerset`) has registered the
server.

## Hard rules

- This skill **requires** the `powerset-search` MCP. If it is not
  registered, route the user through `$powerset setup` →
  `mcp_install install --host all`.
- Every Sales Nav call is **scoped to a `set_id`**. The MCP server enforces
  the set context. Pass the one the user specifies. If the user does not
  specify one, resolve the default with
  `packs/search/primitives/resolve_set_operators/resolve_set_operators.py`
  so it can inherit `POWERPACKS_DEFAULT_SET_ID` / `POWERSET_DEFAULT_SET_ID`, or
  fall back to the logged-in operator's active personal set. Do not invent or
  guess `set_id`s.
- **Default to `persist_artifact: true` on every `sales_nav_search` call.**
  Persistence is cheap and lets the user (or the agent in a later turn) page
  large result sets via `get_artifact`. Only pass `persist_artifact: false`
  when the user explicitly asks for an ephemeral search.
- Persistence requires a `conversation_id`. See the conversation_id playbook
  below — without one, persistence is silently skipped server-side and you
  only get the inline `leads` list back.
- Lead profiles can include LinkedIn URLs and contact metadata. Treat them
  as sensitive: keep them in local `.powerpacks/` handoff files and present only
  compact summaries in chat unless the user asks for details.
- Do not pass accumulated lead/mutual payloads through chat or command args.
  Save each MCP page response to a small JSON file, then run
  `sales_nav_artifacts ingest-page --response <path>` so later steps pass only
  paths/state.
- **Default to the orchestrator.** For normal runs, do not hand-compose a long
  MCP/tool sequence in chat. Create a search args/plan JSON, run
  `sales_nav_pipeline`, execute only the `blocked_tool_call` it emits, save the
  response to the requested path, then continue until completed or blocked.
  Use primitives directly only for narrow debugging or power-user requested
  edits.
- For clear, unambiguous searches, do not ask approval/clarifying questions.
  Resolve filters, build a one-or-more-query search plan, let the orchestrator
  search, ingest full artifact content, enrich the visible leads, resolve mutual
  URLs locally, export CSVs, and present a compact summary. Ask only when a
  filter is genuinely ambiguous (for example multiple plausible "Cisco"
  companies) or when the user asks to page/enrich far beyond the visible result
  set.
- Start a fresh Sales Nav search for each search request. Do not scan, discover,
  reuse, resume, or refine from previous `.powerpacks/sales-nav`, artifact,
  `artifact_id`, CSV, JSONL, manifest, or state files. You may use artifacts
  created by the current run for ingestion, enrichment, pagination, and export.
- Do not auto-page endlessly. It is OK for the orchestrator search plan to
  include deliberate same-turn fallback queries/pages for robustness (for
  example strict role + company-only fallback), but otherwise surface
  `has_more` / `next_start_offset` and ask before loading more.
- Keep fallback searches structured before falling back to free text. If an
  exact current-company query returns 0, retry with the same org ID in
  `past_company_ids` before using `keywords`. Keywords are the last fallback.

## Prereqs

Run this once. It starts Auth0 login if credentials are missing or expired,
then installs the MCP into the chosen host:

```bash
./install-powerset-mcp.sh --host all
```

If on Codex, rerun MCP install when the Auth0 token needs to be refreshed.
The installer writes a fresh `Authorization` header into `~/.codex/config.toml`:

```bash
./install-powerset-mcp.sh --host codex
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

## Fast path runner

Prefer the resumable Sales Nav orchestrator for normal runs:

```bash
uv run --project powerpacks python powerpacks/packs/sales-nav/primitives/sales_nav_pipeline/sales_nav_pipeline.py run \
  --query "<user query>" \
  --set-id "<set_id>" \
  --search-plan-json .powerpacks/sales-nav/<run>/search_plan.json
```

For a single simple pass, `--search-args-json` is still accepted. Prefer
`--search-plan-json` because robust Sales Nav recall often needs multiple
queries.

The runner owns local state for the current invocation and exits with
`status: blocked_tool_call` whenever the harness should call a `powerset-search`
MCP tool. The block includes the `tool_name`, `tool_args`, `save_response_to`,
and `continue_command`. The harness/agent should call the MCP tool natively,
write the JSON response to the specified path, then run the continue command.
Local ingest, lead enrichment, mutual URL resolution, export, scoring approvals,
and the ledger are handled by the runner.

For follow-up qualitative search criteria, run a new Sales Nav search. Only pass
`--criteria` when the user explicitly asks to score or analyze the current run's
loaded leads rather than search again.

Search plan shape:

```json
{
  "score_criteria": "investment/endowment team",
  "queries": [
    {"id": "finance", "label": "parent company + finance", "args": {"company_ids": [163348], "function_ids": ["10"]}},
    {"id": "company_only", "label": "parent company only", "args": {"company_ids": [163348]}}
  ]
}
```

The MCP `sales_nav_search` schema accepts current-company IDs via
`company_ids`, past-company IDs via `past_company_ids`, optional display-name
maps for both, and text filters. Keep unrelated agent-only annotations in plan
metadata; the orchestrator strips unsupported fields from tool args.

## Local file handoff

Normally `sales_nav_pipeline` performs these local handoffs. Use the primitives
below directly only when debugging a failed block in the current run or honoring
a power-user request to operate at the primitive level.

Use `packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py` as
the durable local store for each skill invocation. It normalizes MCP page output
into these files:

- `leads.jsonl` — internal handoff, one row per lead with `member_id`,
  `profile_id`, `source_account_ids`, `operators`, `mutual_member_ids`,
  `total_interactions` when present, `linkedin_url`, title/company/location,
  enriched profile fields (`summary`, `experiences`, `education`, `enriched`)
  when available, artifact/source metadata, and seen counts.
- `mutuals.jsonl` — internal handoff, one row per lead↔mutual edge with
  `lead_member_id`, `mutual_member_id`, mutual LinkedIn URL if resolved,
  `total_interactions` when present, operator/source metadata, and
  artifact/source metadata.
- `member_urls.json` — `member_id -> LinkedIn URL` results from
  `sales_nav_resolve_member_ids`.
- `manifest.json` / `state.json` — paths, counts, artifact ids, pages.
- `exports/leads.csv` and `exports/mutuals.csv` — final user-facing CSVs written
  only when you run `export`.

Initialize once per search:

```bash
uv run --project powerpacks python powerpacks/packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py init \
  --query "<user query>" \
  --set-id "<set_id>" \
  --conversation-id "<conversation_id>"
```

Do not ingest compact MCP preview rows when full artifact content is available.
After each MCP `sales_nav_search`, save the response for audit, then immediately
call `get_artifact(include_content=true)` for the returned `artifact_id`, save
that full artifact response, and ingest with `--prefer-content`:

```bash
uv run --project powerpacks python powerpacks/packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py ingest-page \
  --state .powerpacks/sales-nav/runs/<run>/state.json \
  --response .powerpacks/sales-nav/runs/<run>/pages/artifact-full-000.json \
  --prefer-content
```

When the user asks for mutual LinkedIn URLs, first get pending IDs from the
local file store:

```bash
uv run --project powerpacks python powerpacks/packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py pending-mutual-ids \
  --state .powerpacks/sales-nav/runs/<run>/state.json --limit 100
```

Pass those IDs to MCP `sales_nav_resolve_member_ids`, save the MCP response,
then merge it:

```bash
uv run --project powerpacks python powerpacks/packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py ingest-member-urls \
  --state .powerpacks/sales-nav/runs/<run>/state.json \
  --response .powerpacks/sales-nav/runs/<run>/member_urls.response.json
```

After `enrich_extended_profiles`, call `get_artifact(include_content=true)` again
and ingest with `--prefer-content` so `leads.jsonl` contains the full enriched
profile fields (`summary`, `experiences`, `education`).

For final files:

```bash
uv run --project powerpacks python powerpacks/packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py export \
  --state .powerpacks/sales-nav/runs/<run>/state.json
```

Do not use local lookup to answer search/refinement requests. The lookup
primitive is only for explicit inspection of the current run's local files, not
for deciding whether to avoid a new Sales Nav search:

```bash
uv run --project powerpacks python powerpacks/packs/sales-nav/primitives/sales_nav_artifacts/sales_nav_artifacts.py lookup \
  --state .powerpacks/sales-nav/runs/<run>/state.json --query "<name/company/title>"
```

For explicit current-run analysis requests (for example "score these current
results for real estate exposure"), use the scoring primitive instead of
grepping manually. For search/refinement requests, run a new Sales Nav search:

```bash
uv run --project powerpacks python powerpacks/packs/sales-nav/primitives/score_sales_nav_leads/score_sales_nav_leads.py \
  --state .powerpacks/sales-nav/runs/<run>/state.json \
  --criteria "real estate exposure" \
  --threshold 0.7
```

It fans out over `leads.jsonl`, includes joined mutual context, writes only
matching leads to `scores/<criteria>/matches.jsonl`, and writes the user-facing
`matches.csv`. Non-matches are not written unless `--dump-debug` is passed.

## Manual workflow / debugging reference

The normal workflow is the orchestrator loop above. The lower-level sequence
below documents what the orchestrator is doing and is useful when a block fails
or a power user wants to call primitives directly.

### Step 0 — Confirm prereqs

```bash
uv run --project powerpacks python powerpacks/packs/powerset/primitives/mcp_install/mcp_install.py status --host all
```

If `installed: false` for the host the user is on, route to
`$powerset setup` first.

### Step 0b — Resolve set scope

If the user provided a `set_id`, use it. Otherwise run:

```bash
uv run --project powerpacks python powerpacks/packs/search/primitives/resolve_set_operators/resolve_set_operators.py \
  --env-file .env
```

Use the returned `set_id` in Sales Nav MCP calls. The returned
`operator_ids` are useful for parity with local TurboPuffer searches, but the
Sales Nav MCP is still scoped by `set_id`.

### Step 1 — Decide whether to resolve filters

If the user gave you free-text companies or titles, call
`sales_nav_resolve` first to convert them into stable IDs / canonical
strings. If there is a single obvious recommended company/title match, proceed
without asking. Ask the user only when multiple plausible matches are genuinely
ambiguous (for example many unrelated "Cisco" entities and no exact/obvious
match):

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

Resolution robustness:

- LinkedIn company URLs should be passed directly to `sales_nav_resolve`; the
  server can resolve vanity slugs such as `linkedin.com/company/genies-inc`.
- Endowment / investment office / foundation phrases often name a team rather
  than a LinkedIn company. Resolve the parent institution too. Example:
  `"Dartmouth endowment"` should be tried with `"Dartmouth"` /
  `"Dartmouth College"`, then the search plan should use the parent org ID plus
  finance/investment keywords and a parent company-only fallback.
- If the first resolve call returns no company matches, do not stop. Retry once
  in the same turn with cleaned parent/name variants before telling the user it
  cannot be resolved.
- If a current-company search returns 0 for a resolved org, keep the same org
  ID and run a past-company query next. Only use keyword fallback after the
  structured current/past company paths are empty or unavailable.

Mode defaults:

- "who works at Brookfield" / "who's in my extended network at Cisco" → company
  search. Resolve company, search it, ingest full artifact content, enrich the
  visible page, export.
- "who works at Brookfield in the finance department" → same company search plus
  finance intent. Use reliable Sales Nav filters if known (`function_ids`), and
  also pass targeted `keywords`/`title` when helpful. If the first query is too
  sparse, retry once or twice by relaxing the weakest text filter while keeping
  the company/set fixed.
- "show me people with real estate exposure" after a Sales Nav run → run a new
  Sales Nav search with the updated criteria. Do not scan previous `leads.jsonl`
  or local artifacts for refinement.

### Step 2 — Run the search (always with persistence)

Initialize the local file store before the MCP call if you have not already.

```text
Tool: sales_nav_search
Args: {
  "set_id":         "<the user's set>",
  "conversation_id":"<UUID — see conversation_id playbook>",
  "persist_artifact": true,
  "company_ids":    [...],         // from sales_nav_resolve
  "company_names":  {"<org_id>": "<display name>"},
  "past_company_ids": [...],       // alumni / worked-at searches
  "past_company_names": {"<org_id>": "<display name>"},
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

Save the MCP response for audit, but ingest the full artifact content: call
`get_artifact` with `include_content: true`, save that response, then run
`sales_nav_artifacts ingest-page --prefer-content` on it. The primitive records
`artifact_id`, lead rows, mutual edges, and source/operator metadata in local
handoff files.

Then enrich the visible leads by default:

```text
Tool: enrich_extended_profiles
Args: {
  "conversation_id": "<same conversation_id>",
  "member_ids": [<member_ids from this visible page>],
  "set_id": "<same set_id>"
}
```

After enrichment, call `get_artifact(include_content=true)` again and ingest
with `--prefer-content`. This is what puts full profile data (`summary`,
`experiences`, `education`) into `leads.jsonl`.

Every follow-up retrieval (next page, mutual lookup, re-display) should use the
local state path plus `get_artifact` / `sales_nav_resolve_member_ids` as needed.

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
`get_artifact`, not a fresh `sales_nav_search`. Save each page response and
append it to the same local files with `sales_nav_artifacts ingest-page`:

```text
Tool: get_artifact
Args: {
  "artifact_id":     "<from step 2>",
  "offset":          25,
  "limit":           25,
  "include_content": true    // prefer full accumulated lead shape for local files
}
```

This avoids re-running an expensive Sales Nav fetch and keeps the
filters/result-set stable across pages. For each retrieved page, prefer
`include_content=true` + `ingest-page --prefer-content` so local files keep the
full accumulated lead shape. The local primitive dedupes by `member_id` and
appends new lead↔mutual edges to `mutuals.jsonl`.

### Step 5 — Refining filters

Refining upstream Sales Nav filters is a **new search**, not pagination. Re-call
`sales_nav_search` with the new args, the same `conversation_id`, and
`persist_artifact: true`. You'll get back a new `artifact_id`. Tell the
user the artifact id changed.

Refining over already-loaded people is a **new Sales Nav search**. For
questions like "which of these have real estate exposure", "who is in NYC", or
"show the Brookfield VPs with mutuals", do not scan local state first. Re-call
`sales_nav_search` with the updated filters and `persist_artifact: true`.

| User says | What changes |
| --- | --- |
| "next page" | `get_artifact` with `offset += 25` |
| "narrow to NYC" | new `sales_nav_search` with `geography_ids` |
| "only senior+ folks" | new `sales_nav_search` with `seniority_ids` |
| "filter to mid-stage companies" | new `sales_nav_search` with `headcount_ids` |
| "show me their mutual" | use current-run `mutual_member_ids`; do not scan prior artifacts |

### Step 6 — Resuming a previous search

Do not resume previous searches. If the user references a prior `artifact_id`
while asking for search results, run a new search instead. Only call
`get_artifact` for an old artifact when the user explicitly asks to inspect that
artifact rather than run a search.

## What this skill does NOT do

- It does not write leads to your local `contacts.csv`. Lead → contact
  promotion is a separate (future) `import-sales-nav-leads` skill, likely
  also under `packs/sales-nav`.
- It does not promote leads into the Powerset main set. That UI affordance
  is on the web app today.
- It does not run a sequence of pages on its own. Pagination is always
  user-confirmed and goes through `get_artifact`.
