# Pipeline Eval

Last run: `2026-05-12T20:02:15Z`

Scope: recall YAMLs → expand_search_request → search_network_pipeline (direct mode, no slicing).

LLM rerank skipped: `True`

| Bucket | Pass | Fail | Ignored | Cases |
|---|---:|---:|---:|---:|
| role | 8 | 1 | 0 | 9 |

| Case | Bucket | Status | Returned | Hydrated | Hits/Expected | Recall | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| role_ai_engineers.yaml | role | pass | 8491 | 8490 | 34/35 | 97% | missed 1+ expected |
| role_ai_researcher_phd.yaml | role | pass | 2170 | 2170 | 25/28 | 89% | missed 3+ expected |
| role_data_infrastructure_expert.yaml | role | fail | 9487 | 9486 | 7/11 | 64% | missed 4+ expected |
| role_data_scientist.yaml | role | pass | 9781 | 9779 | 28/30 | 93% | missed 2+ expected |
| role_devops_engineers.yaml | role | pass | 8706 | 8705 | 16/19 | 84% | missed 3+ expected |
| role_finance_team.yaml | role | pass | 10000 | 9999 | 14/14 | 100% |  |
| role_operations_people.yaml | role | pass | 5692 | 5692 | 10/11 | 91% | missed 1+ expected |
| role_ops_sf_startup_seniority.yaml | role | pass | 10000 | 10000 | 0/0 |  |  |
| role_robotics_people.yaml | role | pass | 10000 | 9998 | 14/14 | 100% |  |
