# Sales Nav Pack

`packs/sales-nav` is a Sales Navigator search workflow. The pipeline primitive makes its
own calls: with a set (`--set-id`, the set the chat's instructions name) it calls
the Powerset API's `/v2/sales-nav` routes, which search every Sales Nav seat in the set
and merge; without one it searches through the user's own LinkedIn session on this Mac
(`packs/ingestion/primitives/discover/linkedin/linkedin_session.py`). Either way the
leads land in file-backed handoffs so agents pass paths, not payloads.

## Why a separate pack

Sales Nav lives in its own pack instead of under `packs/powerset` because:

- It is a workflow / skill, not part of the auth + MCP install plumbing
  that `packs/powerset` provides.
- Future Sales Nav skills (e.g. `import-sales-nav-leads` for lead →
  contact promotion) belong here, not under `packs/powerset`.
- Users may install `packs/sales-nav` without taking on every other
  Powerset workflow.

## Dependencies

With a set, the pipeline needs `packs/powerset`'s `auth` for the bearer token (it refreshes
itself). Without a set it needs the LinkedIn Chrome profile the connections import signed
in to. No MCP install.

## Skill Surface

- `sales-nav-search` — Run a Sales Navigator search. Resolves company / title
  filters, runs a multi-query plan the agent can extend after reading each
  query's summary, enriches loaded leads and resolves mutual URLs (cloud), and
  writes `leads.jsonl` / `mutuals.jsonl` plus CSV exports. See
  [`skills/sales-nav-search/SKILL.md`](skills/sales-nav-search/SKILL.md).

## Calls the pipeline makes

| Step | With a set (Powerset API) | Without (own session) |
| --- | --- | --- |
| resolve names → ids | `POST /v2/sales-nav/resolve` | Sales Nav typeahead |
| lead search | `POST /v2/sales-nav/search`, all seats merged | `salesApiLeadSearch`, one seat |
| profile enrichment | `POST /v2/sales-nav/enrich` | skipped, reason recorded |
| mutual member ids → URLs | `POST /v2/sales-nav/member-urls` | skipped, reason recorded |

The routes live in `network-search-api/api_v2/routes/sales_nav.py` over the same functions
the `powerset-search` MCP tools call.

## Local primitives

| Primitive | Purpose |
| --- | --- |
| `sales_nav_pipeline` | Resumable run. Calls the backend the set decides, ingests every query's leads, enriches, resolves mutual URLs, exports CSVs, reports a summary per query, and approval-gates optional LLM scoring. |
| `sales_nav_artifacts` | Initialize a local run, ingest search responses into `leads.jsonl` / `mutuals.jsonl`, merge member URL resolutions, export CSVs, and answer lookup queries against the files. |
| `score_sales_nav_leads` | Fan-out LLM scoring over a run's `leads.jsonl` + mutual context, writing matching leads to `scores/<criteria>/matches.csv`. |

## Defaults

- `count: 25` per page (LinkedIn cap).
- Do not loop unbounded. A plan is a few deliberate queries (structured first:
  company, seniority, function, exact title; keywords last); read the per-query
  summaries, add queries by id if the pool is wrong or thin, and stop at about five.
- Local runs count against 100 LinkedIn requests a day: one per run plus one per query.

## Tasks

- [`tasks/sales-nav-search.task.json`](tasks/sales-nav-search.task.json)
  — orchestration manifest used by harnesses that drive the skill
  programmatically.
