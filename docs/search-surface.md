# Search Surface

`powerpacks` V1 exposes a narrow search surface designed to succeed on simple
requests without leaking private internal systems.

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
2. `resolve_companies` if needed
3. `count_candidates`
4. `execute_role_search`
5. `hydrate_people`

## Expand Step

The expand step should:

- normalize the user request
- extract role/title constraints
- extract company-name constraints
- extract company attribute constraints such as headcount, funding, sector, and
  company geography
- extract recall-style constraints such as education, tenure, years of
  experience, and age
- produce a schema-valid role-search payload

It should not run retrieval.

## Execute Step

The execute step should:

- accept only a schema-valid role-search payload
- optionally resolve company names to `company_ids`
- run one bounded TurboPuffer role search
- hydrate the result IDs after retrieval

It should not redo query decomposition from raw prose.

## Explicitly Out Of Scope In V1

- Sales Nav
- private internal joins
- broad enrichment
- undisclosed private schemas
- separate summary search
- separate company-signal search
