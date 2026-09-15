# Pond expansion audit — 2026-09-15

Audited `beaff8c7` (PR #519). Restored the local SQL candidate-merge and
zero-result diagnostic instructions removed by the skill cleanup. Removed stale
deep-mode permission to retune seniority or embellish generated traits.

Live calls used `expand_query_parallel`, `gpt-5.6-luna`, reasoning `medium`, the
same expansion settings as `search_harness.compile_pond`. No retrieval,
candidate filtering, reranking, or cross-encoder calls were made.

Six saved JD pond queries: Braintrust backend, Icarus flight test, Fortuna
management consultant, Lovable web designer/frontend hybrid, tldraw software
engineer/frontend, and Pylon account manager/mortgage operations. These were
saved queries, not freshly generated JD-to-pond outputs.

The initial production-settings run incorrectly classified Pylon's Account
Manager as manager seniority. The corrected seniority prompt distinguishes
managing accounts/products from managing people. The final full-expansion run
passed all 12 cases in `seniority_extraction.csv`: exact seniority sets,
preserved geography, and no invented founder/C-suite roles. All six JD ponds
retained a simple role trait and at most one experience trait. Explicit senior,
lead, head, and staff/principal controls retained their requested levels.

Raw local outputs: `/tmp/search-expansion-audit-fixed-20260915/` (one JSON per
case, including source, query, prompt hash, model/settings, and full output).

Limitations: this is one sample per final case, not a reliability estimate.
An earlier non-production `reasoning=none` smoke dropped London's geography.
Both production-settings runs preserved it. Saved broad ponds omit JD title
levels; this audit does not establish end-to-end JD seniority preservation.

Repeat just the seniority regression (paid API calls):

```sh
uv run --project . python packs/search/evals/extractor_eval.py \
  --extractor seniority --dataset-dir packs/search/evals/pond-seniority \
  --model gpt-5.6-luna --reasoning-effort medium --env-file .env
```

The command writes `extractor_eval.md`; preserve any previous report first.
`--dry-run` validates the dataset without API calls or report changes.

Offline validation: 162 tests pass when these modules run in separate Python
processes: `test_expand_search_request`, `test_search_harness`,
`test_agentic_sql_fanin`, `test_turbopuffer_primitives`,
`test_seniority_band_pinning`, and `test_core_layout`. The combined-process run
fails `test_social_and_interaction_prefilters_are_postgres_backed`; that module
passes alone. This audit does not claim a clean combined/full-suite run.
