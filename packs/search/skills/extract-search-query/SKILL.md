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
- For common known company domains, also include the corresponding
  `sector_types` when obvious so company resolution can use backend-parity
  staged/soft-union behavior. Examples: fintech → `fintech`, database/data
  infrastructure → `data`, AI/ML → `ai_ml`, developer tools/infrastructure →
  `infra_devtools`, healthcare/mental health → `healthcare`, cybersecurity →
  `security`, crypto/web3 → `crypto_web3`, climate → `climate`, logistics →
  `logistics`, SaaS → `enterprise_saas`, semiconductors → `semiconductors`.
- Always choose `company_sector_strategy` explicitly when `sector_types` are
  present with `company_semantic_queries`; default to `staged` for normal domain
  searches unless the user asks for strict (`hard_filter`) or recall-heavy
  broadening (`soft_union`).
- Treat tenure/date constraints as overlapping-position windows:
  `position_after_date` means the position overlaps after that date, not merely
  that it started after that date.
- Treat broad graduation ranges as education filters, not position-tenure
  filters.
- Use explicit split currentness fields only:
  - `is_current_role`: currentness for role/title matching positions
  - `is_current_company`: currentness for company membership filters
- Do not emit legacy `is_current` in `role_search_filters`.
- Treat query words like "current" or "currently" as current only for the
  mentioned dimension. Example: "current engineers at OpenAI" usually sets both
  `is_current_role: true` and `is_current_company: true`; "people currently at
  OpenAI who used to be PMs" sets `is_current_company: true` and leaves role
  currentness false/past if encoded separately.
- For simple "ROLE at COMPANY/domain" recruiting queries, interpret `at` as
  current by default and emit both `is_current_role: true` and
  `is_current_company: true`, unless the user asks for past/all-time experience.
- For role or company queries, include a `notes` entry that makes currentness
  semantics explicit: current-only, all-time, or past-only for each dimension.
  If a note says current-only, the matching `is_current_role` and/or
  `is_current_company` fields must be present. If the user's wording does not
  make currentness clear, do not choose silently; leave a note for the
  orchestrator to ask before retrieval.
- Infer seniority intent instead of leaving broad role searches unbounded:
  - For hands-on IC role queries such as engineers, AI/ML engineers, data
    scientists, designers, product managers, analysts, or marketers, emit IC
    `seniority_bands` unless the user asks for leadership. Default broad IC
    bands to `["mid", "senior", "staff", "principal"]`; use
    `["entry", "junior"]` only when the query says intern, new grad, junior, or
    entry-level.
  - For leadership queries containing terms such as leader, leadership, manager,
    head, director, VP, executive, C-suite, or chief, emit leadership bands such
    as `["manager", "director", "vice_president", "c_suite"]`, narrowed when
    the user names a specific level.
  - If the user explicitly says all seniorities or broad recall, omit
    `seniority_bands` and note that seniority is intentionally unbounded.
- Founder shortcut: for founder/co-founder/founding-team queries, include
  canonical `role_ids: ["founder"]`, include founder title aliases in
  `bm25_queries`, and do not add `seniority_bands` unless the user explicitly
  asks for a non-founder seniority constraint.
- C-suite shortcut: for CEO/CTO/CFO/CMO/COO/CPO/CRO/CISO queries, include the
  canonical role ID when known (for example `chief_technology_officer` for CTO),
  and use `seniority_bands: ["c_suite"]` unless the user asks for all executive
  levels.

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
