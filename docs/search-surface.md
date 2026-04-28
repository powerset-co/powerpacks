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
- "people at ai startups in san francisco"

## Public Vertical Model

- `people_by_role`
  Start with role/title intent and optional location/company constraints.

- `people_by_company`
  Start with company criteria, then retrieve matching people with optional
  person-level constraints.

## Public Primitive Flow

1. `decompose_search_request`
2. `resolve_companies` if needed
3. `count_candidates`
4. `search_people_roles` or `search_people_company_signals`
5. `hydrate_people`

## Explicitly Out Of Scope In V1

- Sales Nav
- private internal joins
- broad enrichment
- undisclosed private schemas
