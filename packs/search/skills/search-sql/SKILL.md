---
name: search-sql
description: "Agentic SQL search vertical over the local search DuckDB. Use for relational/aggregate people queries the structured filter DSL cannot express (per-person aggregates, overlap joins, career-shape predicates), or as a sub-agent fan-out from search. Read-only; local only."
---

# Search SQL (agentic SQL vertical)

> Created: 2026-06-11
>
> Change log:
> - 2026-06-11: Initial version.

Run read-only SQL against the local search DuckDB to answer people-search
questions that row-at-a-time filters and BM25/vector/regex retrieval cannot
express. This skill is used two ways:

1. **Sub-agent vertical** — `search` (local mode) fans out to a
   sub-agent running this skill when the query has a relational or aggregate
   component. The sub-agent returns candidate `person_id`s + evidence, which
   the parent fans in to the main candidate pool before reranking.
2. **Direct** — the user asks a relational question outright
   (`$search-sql who overlapped with <person> at <company>?`).

## When this vertical applies

- **Per-person aggregates / career shape**: "2+ stints at seed-stage
  companies", "average tenure under 2 years", "was an engineer before
  becoming a PM", "promoted internally at the same company".
- **Person-to-person joins**: "people who overlapped with X at a company",
  "schoolmates of X", set algebra across two sub-populations.
- **Safety net / diagnostics**: re-derive a population from raw columns when
  the main pipeline's extraction/filters may have missed (only when asked to
  debug, on the zero-result fallback, or when fan-out is requested).
- **Network analytics**: aggregate questions about the network itself, not a
  people list — "which companies do I know the most people at", "how many of
  my contacts changed jobs in the last 6 months", "what share of my network
  is in SF". Answer with the aggregate table, not candidates.
- **Cohort/event queries**: "everyone who left Acme Corp in 2023"
  (`end_date_epoch` windows per company), "YC founders in my network"
  (`local_companies.yc_batches` joined to founder-titled positions).
- **Intro pathfinding (experimental)**: "who can intro me to John Doe" —
  resolve John Doe's companies and date ranges, then overlap self-join to
  find shared-tenure colleagues. Flag clearly that this is overlap-based
  inference, not a verified relationship graph.

Plain row-level searches ("senior PMs in SF") do NOT need this vertical —
the main retrieval stages own those.

## The only tool

```bash
uv run --project . python packs/search/primitives/local_duckdb_query/local_duckdb_query.py schema
uv run --project . python packs/search/primitives/local_duckdb_query/local_duckdb_query.py schema --format markdown
uv run --project . python packs/search/primitives/local_duckdb_query/local_duckdb_query.py query --sql "<one SELECT/WITH statement>" [--max-rows N]
```

- DB path defaults to `.powerpacks/search-index/local-search.duckdb`; use
  `--db` to select another local index explicitly.
- Read-only connection; only a single SELECT/WITH statement is accepted.
- Default row cap 200 (`truncated: true` signals more rows exist).
- Do not write files, do not open the DuckDB any other way, and do not
  `SELECT *` on tables with `vector` / `*_tokens` columns — project explicit
  columns.

Before generating any SQL, run `schema --format markdown` against the selected
DB. Treat that output as authoritative for available tables, column names,
grains, and DuckDB types. Never generate SQL from remembered or documented
schema. Use the default/explicit JSON format when structured schema output is
more convenient.

## Schema-inspection-driven worked patterns

The examples below are patterns only. Use one only after the schema output for
the selected DB confirms every referenced table and column, then probe the
relevant value spaces and semantics before relying on literals or sentinel
values. If the inspected schema differs, adapt the pattern rather than guessing.

Per-person aggregate (≥2 startup stints):

```sql
SELECT person_id, count(*) AS startup_stints,
       list(DISTINCT company_name) AS companies
FROM local_people_positions
WHERE company_stage IN ('seed', 'series_a')   -- verify values with a DISTINCT probe first
GROUP BY person_id
HAVING count(*) >= 2
```

Coverage warning: company enrichment columns (`company_stage`,
`company_funding_total`, `company_headcount`) can be entirely empty in a
given index. Probe coverage first; if empty, approximate via
`local_companies` (stage, funding_stage, headcount, yc_batches) joined on
`local_people_positions.company_id = local_companies.id` (both UUID;
`company_urn` is a separate VARCHAR urn), or via sector/headcount proxies,
and say so in `notes`.

Overlap self-join (worked with person Y at the same company at the same
time; `end_date_epoch = 0` means still there — treat as open interval):

```sql
WITH target AS (
  SELECT company_id, start_date_epoch AS s,
         CASE WHEN end_date_epoch = 0 THEN 32503680000 ELSE end_date_epoch END AS e
  FROM local_people_positions
  WHERE person_id = '<target-person-id>' AND company_id IS NOT NULL AND start_date_epoch > 0
)
SELECT DISTINCT p.person_id, p.company_name, p.position_title
FROM local_people_positions p
JOIN target t ON p.company_id = t.company_id
WHERE p.person_id <> '<target-person-id>'
  AND p.start_date_epoch > 0
  AND p.start_date_epoch <= t.e
  AND (CASE WHEN p.end_date_epoch = 0 THEN 32503680000 ELSE p.end_date_epoch END) >= t.s
```

Overlap caveat: company resolution in imported data is noisy — a shared or
mis-mapped `company_id` can produce implausible overlaps (e.g. unrelated
titles under one small company). Include `company_name` and
`position_title` in the output, drop rows that are obviously implausible,
and keep the company name in each `evidence` line so the parent/rerank can
judge.

Career ordering (engineer before PM):

```sql
WITH ordered AS (
  SELECT person_id, role_track, start_date_epoch,
         min(CASE WHEN role_track ILIKE '%engineering%' THEN start_date_epoch END) OVER (PARTITION BY person_id) AS first_eng,
         min(CASE WHEN role_track ILIKE '%product%' OR role_track = 'pm' THEN start_date_epoch END) OVER (PARTITION BY person_id) AS first_pm
  FROM local_people_positions
  WHERE start_date_epoch > 0
)
SELECT DISTINCT person_id FROM ordered
WHERE first_eng IS NOT NULL AND first_pm IS NOT NULL AND first_eng < first_pm
```

Value-space probe before filtering on a categorical column (always do this
before trusting literals — e.g. `role_track` is a long, messy tail of ~95
values like `engineering`, `engineering_ic`, `technical_ic`,
`product_management`, `pm`; exact-match filters silently miss):

```sql
SELECT company_stage, count(*) FROM local_people_positions GROUP BY 1 ORDER BY 2 DESC
```

Resolve a person mentioned by name:

```sql
SELECT person_id, full_name, current_title, current_company
FROM local_person_profiles
WHERE full_name ILIKE '%<name>%'
```

(If not in profiles, fall back to a title/company probe on
`local_people_positions` — profiles cover an enriched subset.)

## Method

1. Interpret the request; identify the relational/aggregate core.
2. Inspect the selected DB with `schema --format markdown` before generating SQL.
3. Probe value spaces for any categorical literal you intend to filter on.
4. Build the query iteratively (max ~6 query calls); start narrow, inspect,
   refine. Join `local_person_profiles` at the end to attach names/URLs.
5. Dedupe to person grain when the inspected schema and query require it.

## Output contract (sub-agent mode)

Return ONLY this JSON object as the final message — no prose around it:

```json
{
  "vertical": "agentic_sql",
  "interpretation": "<one sentence: what was queried and why>",
  "sql": "<the final statement that produced the results>",
  "people": [
    {"person_id": "...", "base_id": "...", "full_name": "...", "evidence": "<one short factual line from the data>"}
  ],
  "notes": "<caveats: truncation, partial profile coverage, empty probes>"
}
```

- Cap `people` at 100; order by strength of evidence.
- This object is consumed verbatim: the parent writes it to a file and
  passes it to the local pipeline as `--extra-candidates-json`, which unions
  `people` into retrieval so they go through the same hydration and LLM
  filter/rerank as every other candidate. Optional extra keys per person
  (`position_title`, `company_name`, `city`, `seniority_band`, ...) are
  carried onto the candidate when present.
- `evidence` must come from queried columns (titles, companies, dates,
  counts) — never inferred.
- If the question has no relational/aggregate component or the data cannot
  answer it, return the object with an empty `people` list and say why in
  `notes`. Do not fall back to guessing.

## Hard rules

- For hiring/recruiting-intent queries, never include founder, co-founder,
  CEO, or chief-titled positions in technical-title patterns or candidate
  SQL by default — founders often carry technical evidence but are not
  recruitable for a role hire. Include them only when the parent search or
  user explicitly asks for founder-type profiles, and state the
  inclusion/exclusion assumption in `notes` either way. Do not
  blanket-exclude VP/director/manager titles; leave that judgment to the
  rerank.
- Read-only. Never modify the DuckDB, never write artifact files, never run
  other primitives or network calls from this skill.
- One statement per call; respect the guard errors instead of working around
  them.
- This vertical is additive evidence — never present it as a replacement for
  the main search results.
