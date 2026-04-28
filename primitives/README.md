# Primitives

This directory holds the public V1 Powerpacks primitive surface.

V1 focuses on:

- decomposing free text into schema-valid filters
- simple people search by role
- optional company constraints inside role search
- recall-style filters such as education, tenure, yoe, and age
- hydration via Postgres after retrieval

Public V1 primitives:

- `expand_search_request`
- `resolve_companies`
- `count_candidates`
- `execute_role_search`
- `hydrate_people`
- `query_postgres_profiles`
