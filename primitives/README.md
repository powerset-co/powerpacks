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
- conservative LLM filtering after full-frontier hydration
- sharded agentic candidate review with deterministic final reduction
- artifact persistence for refinement and host UI integrations

Public V1 primitives:

- executable state/data primitives: `task_state`, `contracts`,
  `build_investor_index`, `resolve_education`, `resolve_investors`, `resolve_companies`, `apply_prefilters`,
  `count_candidates`, `execute_role_search`, `execute_search_slice`,
  `hydrate_people`, `llm_filter_candidates`, `agentic_candidate_review`,
  `persist_search_results`
- agent-planned reasoning primitives: `expand_search_request`,
  `plan_adjacency_search`, `decide_search_strategy`, `generate_search_slices`,
  `merge_candidate_frontier`, `assess_frontier`, `plan_candidate_review`
- planned follow-up data primitives: `refine_search_results`,
  `query_postgres_profiles`

Host-specific runtimes, CLIs, and test harnesses live under `adapters/`.
