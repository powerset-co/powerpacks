# Primitives

This directory holds the public V1 Powerpacks primitive surface.

V1 focuses on:

- decomposing free text into schema-valid filters
- generating multiple bounded retrieval slices
- simple people search by role
- optional company constraints inside role search
- recall-style filters such as education, tenure, yoe, and age
- candidate-frontier review planning without expensive scoring
- hydration via Postgres after retrieval

Public V1 primitives:

- `expand_search_request`
- `decide_search_strategy`
- `generate_search_slices`
- `resolve_companies`
- `count_candidates`
- `execute_search_slice`
- `merge_candidate_frontier`
- `assess_frontier`
- `plan_candidate_review`
- `execute_role_search`
- `hydrate_people`
- `query_postgres_profiles`
