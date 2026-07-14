# Recall Parity

> **Dated evaluation snapshot.** These May 2026 legacy-parity results are
> historical evidence, not a current quality claim or roadmap.

Last run: `2026-05-08T06:07:10Z`

Scope: legacy recall YAMLs executed through Powerpacks primitives with deterministic decomposition.

App dir: `/path/to/app-repo`
Run dir: `/path/to/app-repo/.powerpacks/runs/recall-parity`
Log dir: `/path/to/app-repo/.powerpacks/runs/recall-parity-logs`

Execution notes:

- Does not call hosted `/expand` or `/execute`.
- Ignores UUIDv4 expected IDs because those are staging/non-comparable to current UUIDv5 person IDs.
- Uses Powerpacks primitives for company, investor, education, prefilter, count, retrieval, hydration, and persistence.
- Failures are primitive/decomposition parity gaps, not LLM reranker failures.

| Bucket | Pass | Fail | Ignored | Cases |
|---|---:|---:|---:|---:|
| date_range | 0 | 1 | 0 | 1 |
| founders | 0 | 11 | 0 | 11 |

| Case | Bucket | Status | Count | Returned | Hydrated | Expected Hits | Recall | Ignored v4 | Artifact | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| date_range_founders_since_2018.yaml | date_range | fail | 24 | 24 | 24 | 0/8 | 0% | 0 | `/path/to/app-repo/.powerpacks/runs/recall-parity/artifacts/recall-date_range_founders_since_2018/recall-date_range_founders_since_2018.csv` | missed 8+ expected ids |
| founders_ai_ml_data_large_pool.yaml | founders | fail | 73 | 73 | 73 | 0/7 | 0% | 0 | `/path/to/app-repo/.powerpacks/runs/recall-parity/artifacts/recall-founders_ai_ml_data_large_pool/recall-founders_ai_ml_data_large_pool.csv` | missed 7+ expected ids |
| founders_argentina.yaml | founders | fail | 0 | 0 | 0 | 0/1 | 0% | 0 | `/path/to/app-repo/.powerpacks/runs/recall-parity/artifacts/recall-founders_argentina/recall-founders_argentina.csv` | missed 1+ expected ids |
| founders_backed_by_amplify.yaml | founders | fail | 0 | 0 | 0 | 0/13 | 0% | 0 | `/path/to/app-repo/.powerpacks/runs/recall-parity/artifacts/recall-founders_backed_by_amplify/recall-founders_backed_by_amplify.csv` | missed 13+ expected ids |
| founders_backed_by_elad_gil.yaml | founders | fail | 0 | 0 | 0 | 0/0 |  | 0 | `/path/to/app-repo/.powerpacks/runs/recall-parity/artifacts/recall-founders_backed_by_elad_gil/recall-founders_backed_by_elad_gil.csv` |  |
| founders_backed_by_naval_ravikant.yaml | founders | fail | 0 | 0 | 0 | 0/0 |  | 0 | `/path/to/app-repo/.powerpacks/runs/recall-parity/artifacts/recall-founders_backed_by_naval_ravikant/recall-founders_backed_by_naval_ravikant.csv` |  |
| founders_backed_by_peter_thiel.yaml | founders | fail | 0 | 0 | 0 | 0/0 |  | 0 | `/path/to/app-repo/.powerpacks/runs/recall-parity/artifacts/recall-founders_backed_by_peter_thiel/recall-founders_backed_by_peter_thiel.csv` |  |
| founders_backed_by_sam_altman.yaml | founders | fail | 1 | 1 | 1 | 0/0 |  | 0 | `/path/to/app-repo/.powerpacks/runs/recall-parity/artifacts/recall-founders_backed_by_sam_altman/recall-founders_backed_by_sam_altman.csv` |  |
| founders_backed_by_sequoia.yaml | founders | fail | 4 | 4 | 4 | 2/22 | 9% | 0 | `/path/to/app-repo/.powerpacks/runs/recall-parity/artifacts/recall-founders_backed_by_sequoia/recall-founders_backed_by_sequoia.csv` | missed 20+ expected ids |
| founders_database_companies.yaml | founders | fail | 73 | 73 | 73 | 0/2 | 0% | 0 | `/path/to/app-repo/.powerpacks/runs/recall-parity/artifacts/recall-founders_database_companies/recall-founders_database_companies.csv` | missed 2+ expected ids |
| founders_devtools_infra.yaml | founders | fail | 73 | 73 | 73 | 0/30 | 0% | 0 | `/path/to/app-repo/.powerpacks/runs/recall-parity/artifacts/recall-founders_devtools_infra/recall-founders_devtools_infra.csv` | missed 20+ expected ids |
| founders_fintech_california.yaml | founders | fail | 2 | 2 | 2 | 0/43 | 0% | 0 | `/path/to/app-repo/.powerpacks/runs/recall-parity/artifacts/recall-founders_fintech_california/recall-founders_fintech_california.csv` | missed 20+ expected ids |
