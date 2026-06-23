# Pipeline Eval Results

Run date: 2026-05-11
Set: **Powerset** (`2663f70d`, 5 members, 39K people)
Mode: `--search-only` (no LLM filter/rerank)
Extraction: `expand_search_request` parallel extractors (gpt-4.1, 7 domain-specific)
Source: recall YAMLs from `network-search-api/tests/recall/`
Flow: parallel extractors → search_network_pipeline (direct, no slicing)

---

## Summary

| Bucket | Pass | Fail | Total | Pass Rate | vs Codex (round 1) |
|---|---:|---:|---:|---:|---|
| founders | 8 | 3 | 11 | 73% | same |
| date_range | 6 | 2 | 8 | **75%** | was 63% |
| education | 3 | 3 | 6 | 50% | same (namespace gap) |
| role | 6 | 3 | 9 | **67%** | was 22% |
| **Total** | **23** | **11** | **34** | **68%** | was 53% |

**+15pp overall** from Codex round 1. Key improvements:
- date_range: 63% → 75% (CRO false-positive fix + is_current date-range fix)
- role: 22% → 67% (dropped role_tracks + proper BM25 queries + degree case fix)
- ai_researcher_phd: 14% → 75% (degree_normalized case mismatch fixed)

---

## Founders (8/11 pass)

| Case | Returned | Recall | Status |
|---|---:|---|---|
| argentina | 22 | 1/1 (100%) | ✅ |
| backed_by_amplify | 64 | 13/13 (100%) | ✅ |
| backed_by_elad_gil | 92 | ≥5 count | ✅ |
| backed_by_naval_ravikant | 100 | ≥10 count | ✅ |
| backed_by_peter_thiel | 43 | ≥5 count | ✅ |
| backed_by_sam_altman | 59 | ≥5 count | ✅ |
| backed_by_sequoia | 442 | 22/22 (100%) | ✅ |
| database_companies | 500 | 1/2 (50%) | ✅ |
| ai_ml_data_large_pool | 906 | 3/7 (43%) | ❌ broad sector, CE needed |
| devtools_infra | 924 | 19/30 (63%) | ❌ broad sector, limit cap |
| fintech_california | 238 | 19/43 (44%) | ❌ broad sector + location |

**Remaining failures**: all broad-sector company resolution (9-10K companies).
Needs CE or sub-sector taxonomy.

---

## Date Range (6/8 pass) — up from 5/8

| Case | Returned | Recall | Status |
|---|---:|---|---|
| airbnb_2021 | 81 | 1/1 (100%) | ✅ |
| google_2020_2022 | 470 | 6/6 (100%) | ✅ |
| thumbtack_2012_2015 | 472 | 6/6 (100%) | ✅ |
| thumbtack_2012_2015_verbose | 472 | 6/6 (100%) | ✅ |
| vercel_2020_2024 | 24 | 1/1 (100%) | ✅ |
| founders_since_2022 | 500 | 4/8 (50%) | ✅ |
| ai_engineers_around_2022 | 201 | 7/30 (23%) | ❌ large pool, weak ranking |
| meta_after_2020 | 100 | 1/12 (8%) | ❌ large pool, weak ranking |

**founders_since_2022**: passes at 50% but the test is too broad (500 SF
founders since 2022). The YAML should scope tighter — expected people are
needles in a huge haystack.

**Remaining failures**: large pools where expected people don't surface in
top-K. Not extraction issues — ranking/pool-size problems.

---

## Education (3/6 pass)

| Case | Returned | Recall | Status |
|---|---:|---|---|
| field_psych_stanford | 30 | 7/7 (100%) | ✅ |
| stanford_and_cal | 86 | 5/5 (100%) | ✅ |
| stanford_recent_grads | 80 | 4/6 (67%) | ✅ |
| graduation_year (MBA 2020-2023) | 1000 | 1/7 (14%) | ❌ |
| recent_grads | 0 | 0/4 (0%) | ❌ |
| stanford_grads_2014_2018 | 150 | 0/8 (0%) | ❌ |

**stanford_grads_2014_2018**: All 8 expected people are in the education
prefilter (543 base candidates) but only 150 survive into the people namespace
for the Powerset set. Education→people `allowed_operator_ids` coverage gap.

**recent_grads**: 0 returned. Broad query with no school, graduation year only.
Likely the prefilter found candidates but none matched in the people namespace.

**graduation_year**: MBA + graduation year is sparsely populated in the index.

---

## Role (5/9 pass) — up from 2/9

| Case | Returned | Recall | Status |
|---|---:|---|---|
| data_scientist | 1000 | 26/30 (87%) | ✅ was 0% |
| operations_people | 1000 | 8/11 (73%) | ✅ was 0% |
| ops_sf_startup_seniority | 1000 | ≥count | ✅ was 0% |
| devops_engineers | 1000 | 12/19 (63%) | ✅ |
| robotics_people | 1001 | 12/14 (86%) | ✅ was 14% |
| ai_engineers | 1001 | 17/35 (49%) | ❌ close, large pool |
| ai_researcher_phd | ~800 | 21/28 (75%) | ✅ degree case fix |
| data_infrastructure_expert | 1001 | 2/11 (18%) | ❌ broad domain |
| finance_team | 1000 | 5/14 (36%) | ❌ |

**Big wins**: data_scientist (0→87%), operations_people (0→73%),
robotics_people (14→86%) — all from dropping role_tracks.

**ai_engineers (49%)**: just below 50% threshold. Large pool, expected people
at edge of top-1000.

**ai_researcher_phd (14%)**: only 217 returned. Extraction may be too narrow
with PhD filter.

**finance_team (36%)**: "works on finance team" is ambiguous — extraction may
not produce the right BM25 keywords for finance operations vs FP&A vs
accounting.

---

## Bugs Fixed This Session

### 1. `role_tracks` dropped from extraction
role_tracks as a TurboPuffer hard filter was zeroing out results for
data_scientist, operations, and other role queries. Parallel extractors
never emit role_tracks.

### 2. `is_current` stripped when date range present
When `position_after_date` / `position_before_date` are set, `is_current_role`
and `is_current_company` are stripped from the merged payload. Date windows
already scope the time period; adding `is_current=false` was excluding current
positions that overlap the window.

### 3. `degree_normalized` case mismatch
Merge code lowercased degree_levels (`["bachelors"]`) but TurboPuffer index
has title-case (`"Bachelors"`). Education prefilter returned 0 for any degree
filter. Fixed: pass through as-is from extractor + normalize in
`apply_prefilters` with a case map.

### 4. CRO false-positive in `detect_csuite_shortcut`
`"cross-functional"` in semantic query → tokenize → strip trailing "s" →
`"cro"` → matched Chief Revenue Officer shortcut → injected
`role_ids: ["chief_revenue_officer"]` into every query mentioning
"cross-functional". Fixed by removing `rstrip("s")` from csuite detection
tokenization.

### 4. Parallel extractors ported from network-search-api
7 domain-specific extractors with battle-tested prompts (temporal: 197 lines,
company: 555 lines, etc.) running in parallel via OpenAI async. Uses gpt-4.1
(matching app defaults). Replaces single-prompt gpt-4o-mini approach.

---

## Remaining Issues

### 1. Broad-sector company resolution (CE needed)
database_companies, ai_ml_data_large_pool, fintech_california, devtools_infra.
`resolve_companies` returns 5-10K companies for broad sectors. Needs
cross-encoder or sub-sector taxonomy. **Parked.**

### 2. Education→people namespace coverage gap
stanford_grads_2014_2018: all 8 expected people are in education prefilter
(543 base candidates) but only 150 survive into the people namespace for the
Powerset set. Indexing/operator-visibility gap between namespaces.

### 3. Company extractor over-extracts domain for role queries
ai_researcher_phd: "ai researchers with phds" triggers `sector_types: ["ai_ml"]`
and `company_semantic_queries` which routes through company resolution (2500
companies) + COMPANY_UNION mode. The query is role + education, not a company
query. The company extractor prompt extracts sector_types whenever it sees a
domain keyword ("AI") regardless of whether the user is asking about companies.
Fix: tighten the company extractor to not extract sector_types for pure
role-intent queries.

### 4. Education extractor hallucinating degree_levels
education_recent_grads: "people who graduated college in the last 5 years"
got `degree_levels: ["bachelors"]`. "College" != "bachelors" — could be any
degree. This zeroed out results because bachelors may not match the
`degree_normalized` values in the education namespace.

### 5. YAML override filters not supported in pipeline eval
education_graduation_year: the YAML has user-supplied override filters
(`degree_levels: ["MBA"]`, `graduation_year_min/max`) that the deterministic
harness applies but our pipeline eval doesn't. The extraction for "business
leaders" correctly has no education filters. Either the eval should inject
YAML overrides or the test should be classified as requiring overrides.

### 6. Large-pool ranking
meta_after_2020, ai_engineers_around_2022, ai_engineers, finance_team.
Expected people are in the index but don't surface in top-K. Needs better
within-pool ranking or higher retrieval limits.

### 7. Test case scoping
founders_since_2022 has 500+ matching founders — expected 8 are needles in
a haystack. YAML should scope tighter.

---

## How to Run

```bash
# Uses expand_search_request parallel extractors (gpt-4.1)
POWERPACKS_PIPELINE_SET_ID=00000000-0000-0000-0000-000000000000 \
  scripts/test-search-network pipeline-eval --bucket founders

# All buckets:
for b in founders date_range education role; do
  POWERPACKS_PIPELINE_SET_ID=00000000-0000-0000-0000-000000000000 \
    scripts/test-search-network pipeline-eval --bucket $b
done

# List / dry-run:
scripts/test-search-network pipeline-eval-list --bucket founders
scripts/test-search-network pipeline-eval-dry-run --bucket founders
```

## Files Changed This Session

| File | Change |
|---|---|
| `packs/search/primitives/expand_search_request/expand_search_request.py` | Rewritten: parallel-only, no single-prompt fallback |
| `packs/search/primitives/expand_search_request/parallel_extractors.py` | **New**: 7 parallel extractors ported from app |
| `packs/search/primitives/expand_search_request/prompts/*.txt` | **New**: 6 prompts copied verbatim from app |
| `packs/search/primitives/lib/turbopuffer_client.py` | Fix: CRO false-positive in `detect_csuite_shortcut` |
| `packs/search/primitives/apply_prefilters/apply_prefilters.py` | Fix: degree_normalized case normalization |
| `packs/search/evals/run_pipeline_eval.py` | Uses expand primitive by default; per-case ledgers; set_id injection |
| `packs/search/evals/PIPELINE_EVAL_RESULTS.md` | This report |
| `scripts/test-search-network` | Added pipeline-eval, pipeline-eval-list, pipeline-eval-dry-run |
| `tests/test_pipeline_eval.py` | **New**: 12 unit tests for harness |
