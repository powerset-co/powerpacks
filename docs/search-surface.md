# Search Surface

`powerpacks` V1 exposes a narrow search surface designed to succeed on simple
requests without leaking private internal systems.

The intended user-facing entrypoint is:

- `$search-network <query>`

## Supported Inputs

- natural-language search query
- job description text
- URL with role or company context

## Supported User Stories

- "who are software engineers in sf"
- "product managers at stripe"
- "people with 3-5 yoe at stripe"
- "stanford engineers in sf"
- "founders under 35"
- "people who worked at meta after 2020"
- "senior engineers at series a fintech companies"
- "operators at developer tools companies with 50-200 employees"

## Public Execution Model

- `people_by_role`
  Start with role/title intent and optional location/company constraints.
  This is the only public execution vertical in V1.

## Public Primitive Flow

1. `expand_search_request`
2. choose a strategy based on the expanded request
3. run direct search, count-first search, or slice search
4. `plan_candidate_review`
5. `hydrate_people` only for the selected frontier

## Expand Step

The expand step should:

- normalize the user request
- extract role/title constraints
- extract company-name constraints
- extract company attribute constraints such as headcount, funding, sector, and
  company geography
- extract recall-style constraints such as education, tenure, years of
  experience, and age
- make seniority and geography explicit
- produce a schema-valid role-search seed payload plus planning notes

It should not run retrieval.

## Slice Generation Step

The slice generation step should:

- turn one decomposed request into multiple bounded retrieval slices
- vary title phrasing, geography strictness, seniority emphasis, or currentness
  only when there is a clear reason
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
- run one bounded TurboPuffer role search and return slice-local candidate IDs
  and counts

It should not redo query decomposition from raw prose.

If the strategy is direct search, the same role-search contract can be executed
without generating slices first.

## Frontier Review Step

The frontier review step should:

- merge and dedupe candidates across slices
- preserve slice provenance on every candidate
- report per-slice yield and overlap
- recommend whether to narrow, widen, hydrate, or stop
- avoid expensive scoring in V1

## Explicitly Out Of Scope In V1

- Sales Nav
- private internal joins
- broad enrichment
- undisclosed private schemas
- separate summary search
- separate company-signal search
- expensive candidate scoring
