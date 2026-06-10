# Local/prod search parity tracker

This table is backed by `packs/search/tasks/local-prod-parity.task.json`. Status values are `parity`, `partial`, `gap`, `needs_runtime_data`, and `accepted_non_parity`.

## Current session focus: local search parity

Interaction-count handling is being resolved separately. For this session, use the tracker to drive search behavior parity in this order:

| Order | ID | Area | Status | Priority | Next measurable work |
| --- | --- | --- | --- | --- | --- |
| 1 | LS-08 | parity harness | partial | P0 | Extend existing `run_local_prod_parity.py` output with request payload, expansion, verticals, hydration fields, filter/rerank stage status, and artifact paths. |
| 2 | LS-03 | search verticals | partial | P0 | Measure local role-only retrieval against prod role + summary + company-signal retrieval. |
| 3 | LS-02 | query expansion | gap | P0 | Diff local/prod expansion payloads for fixed queries: adjacency, interaction extraction, title clustering, role IDs, trait/domain intent. |
| 4 | LS-04 | rerank | gap | P0 | Run behavior-level eval before porting prod bulk scoring/grouped evidence/micro-sort. |
| 5 | LS-05 | LLM filter | partial | P1 | Record local/prod filter defaults, caps, skip behavior, and thresholds from code. |
| 6 | LS-06 | result provenance | partial | P1 | Persist enough local result evidence to explain vertical source, scores, and hydrated fields. |

## Local search backlog

| ID | Dimension | Status | Priority | Session scope | Target |
| --- | --- | --- | --- | --- | --- |
| LS-01 | pipeline_stage | partial | P2 | tracked_not_active | Interaction hydration/prefilters are tracked, but not the active search-parity focus. |
| LS-02 | algorithm | gap | P0 | active | Compare local/prod expansion payloads and close or explicitly accept extractor gaps. |
| LS-03 | pipeline_stage | partial | P0 | active | Measure role/summary/company-signal vertical parity. |
| LS-04 | prompt | gap | P0 | active | Behavior-match or explicitly accept non-parity for prod rerank bulk scoring, grouped evidence, and micro-sort. |
| LS-05 | algorithm | partial | P1 | active | Record and compare LLM filter defaults and caps. |
| LS-06 | artifact | partial | P1 | active | Persist result provenance fields in local result artifacts. |
| LS-07 | validation | needs_runtime_data | P2 | tracked_not_active | Preserve filter state across iterative refinements. |
| LS-08 | validation | partial | P0 | active | Extend existing local/prod recall harness with parity metadata. |

## Data pipeline backlog

Data-pipeline rows remain tracked for parity accounting, but are not the current session focus.

| ID | Dimension | Status | Priority | Session scope | Target |
| --- | --- | --- | --- | --- | --- |
| DP-01 | field | partial | P2 | tracked_not_active | Keep source-summary consumption tracked; interaction counts are not the primary search-parity milestone. |
| DP-02 | field | gap | P2 | tracked_not_active | Carry Gmail/source-account aggregate provenance through discovery/import. |
| DP-03 | artifact | gap | P2 | tracked_not_active | Produce inclusion/exclusion manifest from raw artifacts to `people.csv` and DuckDB IDs. |
| DP-04 | validation | needs_runtime_data | P2 | tracked_not_active | Expose Gmail named work-email inclusion gates. |
| DP-05 | validation | partial | P2 | tracked_not_active | Validate DuckDB freshness against manifest, record JSONLs, and optional `_local_record_hashes`. |
| DP-06 | field | partial | P2 | tracked_not_active | Measure company fact coverage against prod contracts. |
