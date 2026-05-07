# Recall Parity

Last run: `2026-05-07T22:07:13Z`

Scope: aleph recall YAMLs executed through Powerpacks primitives with deterministic decomposition.

App dir: `/Users/arthur/workspace/aleph-mvp`
Run dir: `/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/recall-parity`
Log dir: `/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/recall-parity-logs`

Execution notes:

- Does not call aleph `/expand` or `/execute`.
- Ignores UUIDv4 expected IDs because those are staging/non-comparable to current UUIDv5 person IDs.
- Uses Powerpacks primitives for company, investor, education, prefilter, count, retrieval, hydration, and persistence.
- Failures are primitive/decomposition parity gaps, not LLM reranker failures.

| Bucket | Pass | Fail | Ignored | Cases |
|---|---:|---:|---:|---:|
| education | 0 | 4 | 0 | 4 |
| mixed | 1 | 3 | 0 | 4 |

| Case | Bucket | Status | Count | Returned | Hydrated | Expected Hits | Recall | Ignored v4 | Artifact | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| education_field_psych_stanford.yaml | education | fail | 333 | 0 | 0 | 0/7 | 0% | 0 | `/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/recall-parity/artifacts/recall-education_field_psych_stanford/recall-education_field_psych_stanford.csv` | missed 7+ expected ids |
| education_stanford_and_cal.yaml | education | fail | 2 | 2 | 2 | 0/5 | 0% | 0 | `/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/recall-parity/artifacts/recall-education_stanford_and_cal/recall-education_stanford_and_cal.csv` | missed 5+ expected ids |
| education_stanford_grads_2014_2018.yaml | education | fail | 6 | 6 | 6 | 0/8 | 0% | 0 | `/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/recall-parity/artifacts/recall-education_stanford_grads_2014_2018/recall-education_stanford_grads_2014_2018.csv` | missed 8+ expected ids |
| education_stanford_recent_grads.yaml | education | fail | 2 | 2 | 2 | 0/6 | 0% | 0 | `/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/recall-parity/artifacts/recall-education_stanford_recent_grads/recall-education_stanford_recent_grads.csv` | missed 6+ expected ids |
| mixed_banking_data_scientists_yoe.yaml | mixed | fail | 33 | 33 | 33 | 0/2 | 0% | 0 | `/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/recall-parity/artifacts/recall-mixed_banking_data_scientists_yoe/recall-mixed_banking_data_scientists_yoe.csv` | missed 2+ expected ids |
| mixed_blockchain_engineers_nyc.yaml | mixed | fail | 8 | 0 | 0 | 0/15 | 0% | 0 | `/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/recall-parity/artifacts/recall-mixed_blockchain_engineers_nyc/recall-mixed_blockchain_engineers_nyc.csv` | missed 15+ expected ids |
| mixed_data_scientists_banks_usa_yoe_skills.yaml | mixed | pass | 35 | 35 | 35 | 0/0 |  | 0 | `/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/recall-parity/artifacts/recall-mixed_data_scientists_banks_usa_yoe_skills/recall-mixed_data_scientists_banks_usa_yoe_skills.csv` |  |
| mixed_stanford_engineers_sf.yaml | mixed | fail | 4 | 4 | 4 | 0/3 | 0% | 0 | `/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/recall-parity/artifacts/recall-mixed_stanford_engineers_sf/recall-mixed_stanford_engineers_sf.csv` | missed 3+ expected ids |
