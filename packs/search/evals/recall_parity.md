# Recall Parity

Last run: `2026-04-30T22:19:17Z`

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
| mixed | 2 | 2 | 1 | 5 |

| Case | Bucket | Status | Count | Returned | Hydrated | Expected Hits | Recall | Ignored v4 | Artifact | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| mixed_banking_data_scientists_yoe.yaml | mixed | fail | 429 | 50 | 50 | 0/2 | 0% | 0 | `/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/recall-parity/artifacts/recall-mixed_banking_data_scientists_yoe/recall-mixed_banking_data_scientists_yoe.csv` | missed 2+ expected ids |
| mixed_blockchain_engineers_nyc.yaml | mixed | fail | 4 | 4 | 4 | 2/15 | 13% | 0 | `/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/recall-parity/artifacts/recall-mixed_blockchain_engineers_nyc/recall-mixed_blockchain_engineers_nyc.csv` | missed 13+ expected ids |
| mixed_data_scientists_banks_usa_yoe_skills.yaml | mixed | pass | 478 | 478 | 478 | 0/0 |  | 0 | `/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/recall-parity/artifacts/recall-mixed_data_scientists_banks_usa_yoe_skills/recall-mixed_data_scientists_banks_usa_yoe_skills.csv` |  |
| mixed_stanford_engineers_sf.yaml | mixed | pass | 541 | 416 | 415 | 3/3 | 100% | 0 | `/Users/arthur/workspace/aleph-mvp/.powerpacks/runs/recall-parity/artifacts/recall-mixed_stanford_engineers_sf/recall-mixed_stanford_engineers_sf.csv` |  |
| mixed_tech_startup_accounting_finance_sf_nyc.yaml | mixed | ignored |  |  |  | 0/0 |  | 0 | `` | no comparable v5 expected IDs or expected_count after ignoring v4 IDs |
