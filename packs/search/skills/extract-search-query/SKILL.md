---
name: extract-search-query
description: Decompose a recruiting search query, job description, or URL into the Powerpacks role-search filter schema without executing retrieval.
---

# Extract Search Query

Use this skill when the next step needs a schema-valid search payload before
running TurboPuffer or Postgres primitives.

This skill does not retrieve candidates. It only produces the
`expand_search_request` output that downstream primitives consume.

## Inputs

- natural-language recruiting query
- optional job description text
- optional URL content already fetched by the host
- optional prior task state or refinement instruction

## Output Contract

Return JSON shaped like:

```json
{
  "intent_type": "role_search",
  "source_type": "query",
  "normalized_query": "...",
  "vertical": "people_by_role",
  "role_search_filters": {
    "semantic_query": "...",
    "bm25_queries": [],
    "role_tracks": [],
    "role_ids": [],
    "seniority_bands": [],
    "cities": [],
    "states": [],
    "countries": [],
    "education_names": [],
    "company_names": [],
    "company_semantic_queries": [],
    "position_after_date": null,
    "position_before_date": null
  },
  "notes": []
}
```

The exact fields must validate against:

- `powerpacks/schemas/decomposed-query.schema.json`
- `powerpacks/schemas/role-search-filters.schema.json`

Omit null or empty fields in the final payload unless they clarify intent. For
filter-only searches where the user gives hard filters but no role/profile text
(e.g. "people who worked at Meta after 2020"), omit `semantic_query` and rely on
company/date/location/education filters.

## Rules

- Do not run retrieval, hydration, reranking, or persistence.
- Do not invent schema fields.
- Preserve hard constraints: role, location, seniority, currentness, education,
  YOE, age, tenure/date windows, company names, company domains, funding,
  headcount, investors, social thresholds, and `set_id`.
- Use `education_names` for unresolved school names. Do not invent school IDs.
- Use `company_names` for explicit companies. Do not invent company IDs.
- Use `set_id` only when the user provides one or a prior task state already
  has one. If no `set_id` is present, leave it unset; the orchestrating skill
  will run `resolve_set_operators` to use the env/default/personal set.
- Use `investor_names` for unresolved investors. Do not invent investor URNs.
- Use `company_semantic_queries` for vertical/domain company intent such as
  database companies, fintech startups, AI infrastructure, healthcare, or
  developer tooling.
- Always choose `company_sector_strategy` explicitly when both
  `company_semantic_queries` and `sector_types` are present.
- Treat tenure/date constraints as overlapping-position windows:
  `position_after_date` means the position overlaps after that date, not merely
  that it started after that date.
- Treat broad graduation ranges as education filters, not position-tenure
  filters.
- Treat query words like "current" or "currently" as `is_current: true`.
  Otherwise leave currentness unset unless the user clearly asks for past-only.
- For role or company queries, include a `notes` entry that makes currentness
  semantics explicit: current-only, all-time, or past-only. If the user's
  wording does not make currentness clear, do not choose silently; leave a note
  for the orchestrator to ask before retrieval. If `is_current` is set, say
  whether it is intended to mean current at the requested company, current in
  the requested role, or both on the same matched position row.
- Remember that `role_search_filters.is_current` applies to a matched position
  row. When company and role constraints are both present in one payload,
  `is_current: true` means the same position row must satisfy currentness and
  those company/role constraints. If the user's wording requires split
  semantics, such as current at a company but any past role, leave a note for
  the orchestrator to ask or plan separate filters/prefilters.
- For recall-style tests, do not add seniority unless the query or test
  explicitly requires it.

## Semantic Query Standard

`semantic_query` must be dense retrieval prose, not a bare title. Omit it only
for filter-only hard-filter searches with no role/profile semantics.

Good:

```json
{
  "semantic_query": "People who build, debug, and maintain production software systems. They write application, backend, frontend, mobile, platform, infrastructure, or systems code and have hands-on responsibility for shipping software in a professional engineering environment.",
  "bm25_queries": ["software engineer", "software developer", "SWE"]
}
```

Bad:

```json
{ "semantic_query": "software engineer" }
```

## Title Inspection

When role intent is broad, ambiguous, or recall-sensitive, the extraction step
should plan a follow-up title-inspection step before final retrieval.

Examples:

- `ai engineers`
- `data science leaders`
- `people with gtm experience`
- `finance, data, operations, or supply chain leaders`

The output should include a string note such as:

```json
{
  "notes": [
    "title_inspection_needed: The query names a broad role family; inspect matching indexed titles before choosing final BM25/title slices."
  ]
}
```

## Recording

After producing the JSON, the orchestrating skill or harness should record it:

```bash
python powerpacks/packs/search/primitives/task_state/task_state.py record-step \
  --state "$STATE" \
  --step-id expand_search_request \
  --output-json '<the extraction JSON>'
```

Downstream primitives read this state. They should not re-extract the query.
