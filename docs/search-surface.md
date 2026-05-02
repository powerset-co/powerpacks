# Search Surface

`powerpacks` V1 exposes a narrow search surface designed to succeed on simple
requests without leaking private internal systems.

The intended user-facing entrypoints are:

- `/search-network <query>`
- `/search-company <query>`

## Supported Inputs

- natural-language search query
- job description text
- URL with role or company context

## Supported User Stories

People search:

- "who are software engineers in sf"
- "product managers at stripe"
- "people with 3-5 yoe at stripe"
- "stanford engineers in sf"
- "founders under 35"
- "people who worked at meta after 2020"
- "people at box between 2019 and 2022"
- "adjacent infra people"
- "engineers at infrastructure companies"
- "senior engineers at series a fintech companies"
- "operators at developer tools companies with 50-200 employees"

Company lookup:

- "look up facebook"
- "database companies"
- "developer tools companies"
- "series a startups in san francisco"
- "fintech companies with 50 to 200 employees"
- "companies backed by sequoia"
- "yc ai seed companies"

## Public Execution Model

- `people_by_role`
  Start with role/title intent and optional location/company constraints.
  This is the only public execution vertical in V1.
- `companies`
  Resolve exact company names, aliases, vertical/domain descriptions, sectors,
  funding/headcount constraints, geography, YC batch, and investor-backed
  filters into canonical company IDs for inspection or downstream people
  search.

The role vertical may use company-domain adjacency inside the same role-search
contract. It should not call separate summary or company-signal search in V1.

The company lookup surface should use the same resolver primitives that
`search-network` uses for company handoffs. It should not invent a separate
company schema.

## Public Primitive Flow

People search:

1. `expand_search_request`
2. `decide_search_strategy`
3. optionally `plan_adjacency_search`
4. run direct search, count-first search, or slice search
5. `assess_frontier`
6. `plan_candidate_review`
7. `hydrate_people` for the full candidate frontier
8. optionally `llm_filter_candidates` to remove clear non-matches
9. `persist_search_results`
10. host UI or CLI tooling reads persisted artifacts for review/refinement

Company lookup:

1. extract company fields from the query
2. optionally `resolve_investors`
3. `resolve_companies`
4. persist or present company IDs, sample companies, sector strategy, and
   resolver artifacts
5. optionally hand company IDs to `search-network`

## Expand Step

The expand step should:

- normalize the user request
- write a dense 2-3 sentence `semantic_query` for vector retrieval. This should
  describe the target person's work, responsibilities, and relevant experience,
  not just a title. Keep short title aliases in `bm25_queries`. If examples
  are needed, consult `semantic-query-examples.md` and adapt the closest
  pattern without copying irrelevant details.
- extract role/title constraints
- extract company-name constraints
- extract company attribute constraints such as headcount, funding, sector, and
  company geography
- extract recall-style constraints such as education, tenure, years of
  experience, and age
- identify explicit adjacency requests such as "adjacent people" or implied
  domain intent such as "infra people"
- make seniority and geography explicit
- produce a schema-valid role-search seed payload plus planning notes

It should not run retrieval.

## Strategy Step

The strategy step should:

- choose `direct_execute`, `count_then_execute`, `generate_slices`, or
  `ask_for_clarification`
- explain the decision
- decide whether adjacency is off, needs user confirmation, or should be
  included because the user requested it
- preserve all hard-filter requirements as expressions and identify which ones
  require prefilter stages
- estimate broadness and ambiguity
- choose a bounded initial limit

It should use the decomposed request and schema, not raw prose alone.

## Slice Generation Step

The slice generation step should:

- turn one decomposed request into multiple bounded retrieval slices
- vary title phrasing, geography strictness, seniority emphasis, or currentness
  only when there is a clear reason
- optionally include an adjacency slice when domain-adjacent recall is requested
  or confirmed
- usually produce 3-8 slices
- keep each slice valid against the role-search schema
- explain why each slice exists

It should not score people.

It is optional. Use it only when the query is broad enough that one retrieval
pass is likely to miss good variants or produce an unreviewable frontier.

## Execute Step

The execute step should:

- accept only one schema-valid single-slice payload
- optionally resolve company names to `company_ids`
- apply base-ID prefilters before role retrieval when present
- apply tenure/date windows as overlapping-position filters
- run one bounded TurboPuffer role search and return slice-local candidate IDs
  and counts

It should not redo query decomposition from raw prose.

If the strategy is direct search, the same role-search contract can be executed
without generating slices first.

## Adjacency

Adjacency is a planning choice, not a default.

- Strict role search finds people whose titles/roles match the query.
- Title adjacency widens title patterns only.
- Company-domain adjacency finds people at companies matching the domain, even
  when their title is not a literal match.

If the user asks for adjacent people, include it. If the user says "infra
engineers" and it is unclear whether they mean title-level infra engineers or
engineers at infra companies, ask or run strict first and propose adjacency as a
second slice.

## Tenure And Prefilters

Tenure windows use overlap semantics:

- `position_after_date=2019-01-01`
- `position_before_date=2022-12-31`
- match positions that overlapped that period, including current roles

Some hard filters become prefilters because they produce/intersect a base-ID
candidate set before role retrieval. Examples include education, tech skills,
social metrics, interaction metrics, and large company intersections. Other hard
filters are applied directly in TurboPuffer role search, such as location,
seniority, currentness, tenure, YOE, age, role taxonomy, and small company-ID
filters.

Represent hard filters as expressions, not category labels. A slice should carry
the executable `hard_filters` expression plus a `prefilters` plan when a stage
must compute base IDs before role retrieval.

For example, "SWE who went to Stanford" should first narrow candidate base IDs
by Stanford with `resolve_education` plus `apply_prefilters`, then run role
search against that base set.

## Frontier Review Step

The frontier review step should:

- merge and dedupe candidates across slices
- preserve slice provenance on every candidate
- report per-slice yield and overlap
- assess whether the frontier is too broad, too narrow, or coherent
- recommend whether to narrow, widen, filter, or stop
- include a short decision trace
- avoid expensive scoring in V1

## Result Persistence Step

The result persistence step should:

- write CSV and JSONL artifacts for every useful run
- include hydrated profiles before LLM filtering
- preserve unhydrated frontier IDs with `hydrated=false`
- write a manifest and attach artifact paths to task state
- enable later refinement requests to reference the prior run instead of
  re-searching from scratch

## Explicitly Out Of Scope In V1

- Sales Nav
- private internal joins
- broad enrichment
- undisclosed private schemas
- separate summary search
- separate company-signal search
- expensive candidate scoring
