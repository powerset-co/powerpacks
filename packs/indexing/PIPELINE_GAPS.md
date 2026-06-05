# Local Processing Pipeline – Gaps vs Aleph MVP

This document tracks known gaps between the Powerpacks local processing pipeline
(`packs/indexing/primitives/build_processing_pipeline/`) and the production Aleph
pipeline (`aleph-mvp/data_pipeline_v2/`).

Last updated: 2026-06-02

## Architecture

Both pipelines follow the same core shape:

1. **Flatten people** → one row per person with work experiences
2. **Extract/dedupe roles** by `title_hash = md5(title + "|" + description[:500])[:16]`
3. **LLM-enrich unique roles** → role_ids, seniority_band, role_track, role_type,
   specialization, doc2query, inferred_skills, dense_text
4. **Embed roles** → one `text-embedding-3-small` call per unique `title_hash`
5. **Company enrichment** → entity_types, sector_types, semantic_text, doc2query
6. **Embed companies** → one embedding per unique company
7. **Summary/profile records** → per-person summary text + embedding
8. **Education/schools** → lookup tables for school-based prefiltering
9. **Join role enrichment + embeddings back to positions** → final people records
10. **Validate contracts** → strict schema check
11. **Materialize DuckDB** → local search index

## Implemented and matching Aleph

- [x] Role dedupe by `title_hash` (1 LLM call + 1 embedding per unique hash)
- [x] Role enrichment prompt (role_ids, seniority, track, doc2query, skills)
- [x] Role embedding via `text-embedding-3-small` (1536 dims)
- [x] Dense text generation from LLM semantic_text or fallback
- [x] Company entity/sector classification via OpenAI
- [x] Company embedding from semantic_text
- [x] Summary text + embedding per person
- [x] Education/school corpus
- [x] Join role enrichment back to position records with trace fields
  (`title_hash`, `raw_title`, `role_type_category`, `person_id`, `position_id`)
- [x] Fixed-output incremental processing: reruns upsert into canonical stage
  artifacts and fill missing role/company/embedding rows from those artifacts
- [x] Checkpointed role/company/embedding stages
- [x] Strict contract validation (zero extra/missing fields)

## Implemented (ported from Aleph)

### CEO/Founder detection ✅

Ported from `aleph-mvp/data_pipeline_v2/pipelines/people/processing/detect_ceo_founders.py`.

Local primitive: `packs/indexing/primitives/detect_ceo_founders/detect_ceo_founders.py`

Pipeline step `detect_ceo_founders` runs after company embedding, before
`build_people_records`. Reads `flattened_people.jsonl`, finds current CEO/CTO
positions without "founder" in title, calls LLM to classify, writes
`founder_enrichment.jsonl`. The `step_people` join reads founder IDs and injects
`"founder co-founder startup"` into d2q_tokens and `"founder"` into role_ids.

### Inferred birth year / age ✅

Ported from `aleph-mvp/data_pipeline_v2/pipelines/people/processing/infer_ages.py`.
LLM-only (no rule-based fallback).

Local primitive: `packs/indexing/primitives/infer_ages/infer_ages.py`

Pipeline step `infer_ages` runs after CEO/founder detection, before
`build_people_records`. Reads `flattened_people.jsonl`, calls LLM to estimate
birth year from education and work timelines, writes `inferred_ages.jsonl`.
The `step_people` join applies `inferred_birth_year` to people records.

## Known bugs

### Gmail unresolved count doesn't reflect completed Parallel runs

The UI shows "N contacts need email→LinkedIn resolution via Parallel" even after
Parallel already ran on those contacts. The status display reads the original
unresolved queue count without checking whether the resolution step completed.
This causes users to re-run Parallel on contacts that were already resolved
(wasting ~$3/run on duplicate work).

**Fix**: The enrichment status computation should check whether
`gmail_linkedin_resolution` step completed for the current unresolved queue. If
it did, show the post-resolution counts (found/not-found) instead of the
pre-resolution unresolved count.

## Remaining gaps

### Company base data (priority: low, acceptable gap)

**Aleph**: Company records include headcount, funding_total, founded_year,
valuation, investor_urns from Harmonic API.

**Local**: These default to 0/empty because local company data comes from
person position context (RapidAPI), not a dedicated company API. The LLM
classifier still produces entity_types, sector_types, semantic_text, doc2query.

**Impact**: Company numeric filters (headcount range, funding range) won't work.
Company semantic/classification search works fine.

### Investor data (priority: low, acceptable gap)

**Aleph**: `investor_urns` populated from Harmonic company API.

**Local**: Empty arrays. Investor-based company filters won't return results.

### Company signals namespace (priority: low)

**Aleph**: Separate `signals_semantic_text` + `signals_doc2query` embeddings
uploaded to `aleph_company_signals_v1`.

**Local**: Not implemented. Only the main company namespace is used.

### Dedup/merge detection (priority: low)

**Aleph**: `get_merged_delete_ids()` skips positions for people that were
merged away in dedup reconciliation.

**Local**: Not implemented. No impact at typical local network scale (< 1000
people).

## Not applicable to local

- Supabase sync (`sync_persons_to_supabase`) — local uses DuckDB
- Harmonic import (`import_linkedin`) — local uses RapidAPI/CSV
- Operator mapping from Supabase — local uses single default operator
- TurboPuffer upload — local uses DuckDB
- DuckDB people table migration — local creates fresh

## Field notes

- `company_name` is NOT in the people namespace contract. It's a DuckDB-level
  field populated by `postprocess_cross_tables` joining positions to companies.
  This matches Aleph, which also doesn't upload `company_name` to TurboPuffer
  people namespace.
- All five namespace contracts (people, companies, summaries, education, schools)
  now produce zero validation errors against the pipeline output.
