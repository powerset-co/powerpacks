#!/usr/bin/env python3
"""The index's namespaces: each one's local DuckDB table, how a person's documents are
addressed in it, and the cloud's field types for its documents.

`local_index` reads this table to shape the documents the cloud sync sends through the Powerset
API. Nothing here talks to TurboPuffer.

WRITE_SCHEMA is copied from the cloud writers rather than derived from the namespace contracts:
the contracts carry logical types, while the cloud's schema also fixes BM25 tokenizers and the
uint/float widths.

Changelog:
  2026-11-17: was turbopuffer_writer.py; the TurboPuffer reads and writes are gone (the cloud sync
      goes through the Powerset API), leaving the namespace table.
  2026-10-08: company_funding_total is declared int, as in v3, for a fresh namespace.
  2026-09-24: created; the namespace table lives here.
"""

from __future__ import annotations

from typing import Any

from packs.indexing.primitives.upload_powerset.models import Namespace

WRITE_SCHEMA: dict[str, dict[str, Any]] = {
    "people": {
        "word_tokens": {"type": "[]string", "full_text_search": {"tokenizer": "pre_tokenized_array"}},
        "char_tokens": {"type": "[]string", "full_text_search": {"tokenizer": "pre_tokenized_array"}},
        "d2q_tokens": {"type": "[]string", "full_text_search": {"tokenizer": "pre_tokenized_array"}},
        "phrase_tokens": {"type": "[]string", "full_text_search": {"tokenizer": "pre_tokenized_array"}},
        "position_title": {"type": "string"},
        "seniority_band": {"type": "string"},
        "company_id": {"type": "string"},
        "city": {"type": "string"},
        "state": {"type": "string"},
        "country": {"type": "string"},
        "macro_region": {"type": "string"},
        "is_current": {"type": "bool"},
        "total_years_experience": {"type": "float"},
        "start_date_epoch": {"type": "uint"},
        "end_date_epoch": {"type": "uint"},
        "tenure_years": {"type": "float"},
        "base_id": {"type": "string"},
        "role_track": {"type": "string"},
        "inferred_birth_year": {"type": "uint"},
        # int, as in the v3 namespace; local_index sends the local DOUBLE (0.0) as an integer.
        "company_funding_total": {"type": "int"},
        "metro_areas": {"type": "[]string"},
        "allowed_operator_ids": {"type": "[]string"},
        "role_ids": {"type": "[]string"},
    },
    "summaries": {
        "summary": {"type": "string"},
        "summary_tokens": {"type": "[]string", "full_text_search": {"tokenizer": "pre_tokenized_array"}},
        "tech_skills": {"type": "[]string"},
        "allowed_operator_ids": {"type": "[]string"},
    },
    "education": {
        "person_id": {"type": "string"},
        "education_id": {"type": "string"},
        "canonical_education_id": {"type": "string"},
        "degree_normalized": {"type": "string"},
        "field_of_study": {"type": "string", "full_text_search": True},
        "graduation_year": {"type": "uint"},
        "allowed_operator_ids": {"type": "[]string"},
    },
    "schools": {
        "school_name": {"type": "string", "full_text_search": {"stemming": False}},
    },
    "companies": {
        "company_name": {"type": "string", "full_text_search": {"stemming": False}, "filterable": True},
        "name_aliases_text": {"type": "string", "full_text_search": True},
        "semantic_text": {"type": "string", "full_text_search": True},
        "entity_sector_text": {"type": "string", "full_text_search": True},
        "doc2query_text": {"type": "string", "full_text_search": True},
        "funding_total": {"type": "float"},
        "headcount": {"type": "float"},
        "founded_year": {"type": "uint"},
        "last_funding_at": {"type": "uint"},
        "valuation": {"type": "float"},
        "allowed_operator_ids": {"type": "[]string"},
    },
}


# doc_key is how a person's documents are addressed: positions by base_id, a summary's
# document id IS the person id, education carries person_id.
NAMESPACES = (
    Namespace("people", "local_people_positions", "base_id", WRITE_SCHEMA["people"], True),
    Namespace("summaries", "local_summaries", "id", WRITE_SCHEMA["summaries"], True),
    Namespace("education", "local_people_education", "person_id", WRITE_SCHEMA["education"], True),
    Namespace("companies", "local_companies", None, WRITE_SCHEMA["companies"], False),
    Namespace("schools", "local_education", None, WRITE_SCHEMA["schools"], False),
)
NAMESPACE_BY_LOGICAL = {namespace.logical: namespace for namespace in NAMESPACES}
