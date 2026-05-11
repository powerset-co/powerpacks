# Pipeline Eval

Last run: `2026-05-11T19:36:17Z`

Scope: recall YAMLs → agent extraction → search_network_pipeline (direct mode, no slicing).

LLM rerank skipped: `True`

| Bucket | Pass | Fail | Ignored | Cases |
|---|---:|---:|---:|---:|
| education | 4 | 2 | 0 | 6 |

| Case | Bucket | Status | Returned | Hydrated | Hits/Expected | Recall | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| education_field_psych_stanford.yaml | education | pass | 30 | 30 | 7/7 | 100% |  |
| education_graduation_year.yaml | education | fail | 1000 | 1000 | 1/7 | 14% | missed 6+ expected |
| education_recent_grads.yaml | education | fail | 398 | 398 | 0/4 | 0% | missed 4+ expected |
| education_stanford_and_cal.yaml | education | pass | 166 | 166 | 5/5 | 100% |  |
| education_stanford_grads_2014_2018.yaml | education | pass | 542 | 542 | 8/8 | 100% |  |
| education_stanford_recent_grads.yaml | education | pass | 203 | 203 | 6/6 | 100% |  |
