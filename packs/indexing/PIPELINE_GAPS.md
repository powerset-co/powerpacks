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
- [x] Incremental processing with `--limit-mode missing`
- [x] Checkpointed role/company/embedding stages
- [x] Strict contract validation (zero extra/missing fields)

## Gaps to implement

### CEO/Founder detection (priority: high)

**Aleph**: `detect_ceo_founders` step runs LLM-based founder detection for
CEO/CTO positions. When detected, injects `"founder co-founder startup"` into
d2q_tokens and adds `"founder"` to `role_ids`. This is important because many
founders have titles like "CEO" without "founder" in the title.

**Local**: Not implemented. Founders without "founder" in their title won't get
founder d2q boost or `founder` role_id.

**Source**: `aleph-mvp/data_pipeline_v2/pipelines/people/processing/detect_ceo_founders.py`

**Lift plan**: Port the detection prompt and inject logic into a new step between
`build_people_records` and `build_vectors`. The detection reads flattened people,
checks CEO/CTO titles, calls LLM to determine if they are founders, and outputs
`founder_enrichment.jsonl`. The `step_people` join should then read founder IDs
and inject d2q + role_ids like Aleph's `upload_people_turbopuffer.py` does.

### Inferred birth year / age (priority: low)

**Aleph**: Dedicated `infer_ages` step uses LLM (`gpt-5.4-mini`, structured
output) to estimate birth year from education and work experience timelines.
The LLM prompt handles non-traditional students, internship vs full-time
distinctions, high school anchoring, and cross-validation of multiple signals.
A rule-based heuristic in `birth_year.py` serves as fallback only when the LLM
stage hasn't run.

**Local**: Uses `inferred_birth_year` from CSV if available (usually empty for
RapidAPI-sourced people).

**Source**: `aleph-mvp/data_pipeline_v2/pipelines/people/processing/infer_ages.py`

**Lift plan**: Port the LLM stage only. Uses `gpt-5.4-mini` with Pydantic
structured output (`AgeInference` model), async batching, and checkpointing.
Cost is ~$18 for 34K people at flex pricing. Do not port the rule-based
heuristic fallback in `birth_year.py`.

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
