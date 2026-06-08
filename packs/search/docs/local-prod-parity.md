# Local/prod search parity tracker

This table is backed by `packs/search/tasks/local-prod-parity.task.json`. Status values are `parity`, `partial`, `gap`, `needs_runtime_data`, and `accepted_non_parity`.

## Data pipeline

| ID | Dimension | Status | Priority | Target |
| --- | --- | --- | --- | --- |
| DP-01 | field | partial | P0 | Load `records/person_source_summary.records.jsonl` into optional local DuckDB source-summary tables and hydrate `total_interactions`. |
| DP-02 | field | gap | P0 | Carry Gmail/source-account aggregate provenance through discovery/import. |
| DP-03 | artifact | gap | P0 | Produce inclusion/exclusion manifest from raw artifacts to `people.csv` and DuckDB IDs. |
| DP-04 | validation | needs_runtime_data | P1 | Expose Gmail named work-email inclusion gates. |
| DP-05 | validation | partial | P1 | Validate DuckDB freshness against manifest, record JSONLs, and optional `_local_record_hashes`. |
| DP-06 | field | partial | P2 | Measure company fact coverage against prod contracts. |

## Local search

| ID | Dimension | Status | Priority | Target |
| --- | --- | --- | --- | --- |
| LS-01 | pipeline_stage | partial | P0 | Hydrate local `total_interactions`; interaction prefilters remain future work. |
| LS-02 | algorithm | gap | P1 | Track adjacency/interaction extraction, title clustering, and role-id expansion gaps. |
| LS-03 | pipeline_stage | partial | P1 | Measure role/summary/company-signal vertical parity. |
| LS-04 | prompt | gap | P1 | Behavior-match prod rerank bulk scoring, grouped evidence, and micro-sort. |
| LS-05 | algorithm | partial | P2 | Record local/prod LLM filter defaults and caps. |
| LS-06 | artifact | partial | P1 | Persist result provenance fields in local result artifacts. |
| LS-07 | validation | needs_runtime_data | P2 | Preserve filter state across iterative refinements. |
| LS-08 | validation | partial | P1 | Extend existing local/prod recall harness with parity metadata. |

## First fix included

The first concrete parity fix wires the local DuckDB hydration path to a prod-shaped interaction summary table:

- `records/person_source_summary.records.jsonl`
- `local_person_source_summary` or `person_source_summary`
- fields: `person_id`, `operator_id`, `source_channel`, `source_account`, `total_interactions`

This does not yet generate Gmail aggregates; it makes the local search side able to consume them once the data pipeline emits them.
