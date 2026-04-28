# Add Enrichment

Install and maintain person/company enrichment capabilities.

## Intent

- normalize source records
- enrich people and companies
- record confidence and provenance
- separate deterministic transforms from model judgment

## Expected Primitives

- `extract_entities`
- `enrich_person`
- `enrich_company`
- `score_entity_match`
