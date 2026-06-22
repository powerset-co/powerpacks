# Harness Parity

Last run: `2026-04-30T06:37:22Z`

Scope: founder recall YAMLs, executed through Powerpacks `search-network` primitives.

App dir: `/path/to/app-repo`
Run dir: `/path/to/app-repo/.powerpacks/runs/founder-parity`
Log dir: `/path/to/app-repo/.powerpacks/runs/founder-parity-logs`

Execution notes:

- Uses packaged Powerpacks primitives, not external application code.
- Retrieval and hydration respect recall case limits up to `1000` people.
- LLM scoring/filtering and company signal summaries are disabled.
- `resolve_investors` resolves firm and person investors from the Powerpacks TurboPuffer investors namespace.

Summary: `10` pass, `5` fail, `0` unsupported.

| Case | Query | Status | Count | Returned | Hydrated | Expected Hits | Recall | Artifact | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---|---|
| founders_argentina | founders in argentina | pass | 26 | 26 | 26 | 1/1 | 100% | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-founders_argentina/harness-founder-founders_argentina.csv` |  |
| founders_devtools_infra | dev tooling or infrastructure software founders | pass | 1691 | 1000 | 997 | 23/30 | 77% | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-founders_devtools_infra/harness-founder-founders_devtools_infra.csv` | missed 7+ expected ids |
| founders_fintech_california | founders at fintech startups in California | fail | 366 | 366 | 364 | 19/43 | 44% | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-founders_fintech_california/harness-founder-founders_fintech_california.csv` | missed 20+ expected ids |
| founders_ai_ml_data_large_pool | dev tooling ai and data infrastructure founders | fail | 1854 | 1000 | 994 | 2/7 | 29% | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-founders_ai_ml_data_large_pool/harness-founder-founders_ai_ml_data_large_pool.csv` | missed 5+ expected ids |
| founders_backed_by_amplify | founders backed by amplify | pass | 46 | 46 | 45 | 13/13 | 100% | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-founders_backed_by_amplify/harness-founder-founders_backed_by_amplify.csv` |  |
| founders_backed_by_elad_gil | founders backed by elad gil | pass | 68 | 68 | 68 | 0/0 |  | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-founders_backed_by_elad_gil/harness-founder-founders_backed_by_elad_gil.csv` |  |
| founders_backed_by_naval_ravikant | founders backed by naval ravikant | pass | 56 | 56 | 56 | 0/0 |  | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-founders_backed_by_naval_ravikant/harness-founder-founders_backed_by_naval_ravikant.csv` |  |
| founders_backed_by_peter_thiel | founders backed by peter thiel | pass | 26 | 26 | 26 | 0/0 |  | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-founders_backed_by_peter_thiel/harness-founder-founders_backed_by_peter_thiel.csv` |  |
| founders_backed_by_sam_altman | founders backed by sam altman | pass | 29 | 29 | 29 | 0/0 |  | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-founders_backed_by_sam_altman/harness-founder-founders_backed_by_sam_altman.csv` |  |
| founders_backed_by_sequoia | founders backed by sequoia capital | pass | 229 | 229 | 226 | 17/22 | 77% | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-founders_backed_by_sequoia/harness-founder-founders_backed_by_sequoia.csv` | missed 5+ expected ids |
| founders_database_companies | founders of database companies | fail | 1572 | 500 | 497 | 0/2 | 0% | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-founders_database_companies/harness-founder-founders_database_companies.csv` | missed 2+ expected ids |
| date_range_founders_since_2018 | founders in San Francisco since 2022 | fail | 3515 | 500 | 497 | 1/8 | 12% | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-date_range_founders_since_2018/harness-founder-date_range_founders_since_2018.csv` | missed 7+ expected ids |
| staging_founders_basic | founders | pass | 14272 | 100 | 99 | 0/0 |  | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-staging_founders_basic/harness-founder-staging_founders_basic.csv` |  |
| staging_founders_in_san_francisco | founders in san francisco | fail | 2956 | 100 | 99 | 0/42 | 0% | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-staging_founders_in_san_francisco/harness-founder-staging_founders_in_san_francisco.csv` | missed 20+ expected ids |
| staging_founders_sequoia | founders who founded companies backed by sequoia capital | pass | 229 | 100 | 98 | 0/0 |  | `/path/to/app-repo/.powerpacks/runs/founder-parity/artifacts/harness-founder-staging_founders_sequoia/harness-founder-staging_founders_sequoia.csv` |  |

Open gaps:

- Keep the TurboPuffer investors namespace fresh as investor source data changes.
- Improve broad company-domain recall for devtools/infra and fintech with better company semantic queries or sliced company search.
- Add people-side slicing for broad founder/date/location pools instead of relying on one ranked frontier.
- Reconcile staging recall files that appear to use a different person ID namespace.
- Persist applied filters for every case in a compact report row.
