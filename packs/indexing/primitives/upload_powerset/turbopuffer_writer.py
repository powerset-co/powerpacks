#!/usr/bin/env python3
"""TurboPuffer side of the Powerset upload: every call takes a namespace object.

Flow: read helpers first (which doc ids the cloud already holds for a person,
which company/school ids already exist) -> upsert_docs writes whole documents in
500-row batches for people the cloud has never seen -> patch_allowed_operator_ids
rewrites ONLY allowed_operator_ids on the documents of people the cloud already
has, so nothing cloud-enriched is regressed and an un-share just drops this
operator out of the list.

WRITE_SCHEMA is copied from the cloud writers rather than derived from the
namespace contracts: the contracts carry logical types, while a live namespace's
schema also fixes BM25 tokenizers and the uint/float widths, and a write with a
different schema is rejected. Sources:
  people     upload_people_turbopuffer.py
  summaries  upload_summaries_turbopuffer.py
  education  education/upload_people_education_to_turbopuffer.py
  schools    education/upload_to_turbopuffer.py
  companies  company/upload_companies_to_turbopuffer.py

Changelog:
  2026-09-24: created; the namespace table lives here.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

import turbopuffer

from packs.indexing.primitives.upload_powerset.models import Namespace

BATCH_SIZE = 500
QUERY_PAGE_SIZE = 1000
STRONG_CONSISTENCY = {"level": "strong"}
DISTANCE_METRIC = "cosine_distance"

ALLOWED_OPERATOR_IDS_SCHEMA = {"allowed_operator_ids": {"type": "[]string"}}

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


# WRITE_SCHEMA stays here because this writer owns it; importing it into models
# would make the namespace table depend on this writer in both directions.
# doc_key is how a person's documents are addressed, read off the live
# namespaces: aleph_people_v1 keys positions by base_id, aleph_summaries_v1's
# document id IS the person id, aleph_people_education_v1 carries person_id.
NAMESPACES = (
    Namespace("people", "local_people_positions", "base_id", WRITE_SCHEMA["people"], True),
    Namespace("summaries", "local_summaries", "id", WRITE_SCHEMA["summaries"], True),
    Namespace("education", "local_people_education", "person_id", WRITE_SCHEMA["education"], True),
    Namespace("companies", "local_companies", None, WRITE_SCHEMA["companies"], False),
    Namespace("schools", "local_education", None, WRITE_SCHEMA["schools"], False),
)
NAMESPACE_BY_LOGICAL = {namespace.logical: namespace for namespace in NAMESPACES}


def _chunks(values: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def live_attributes(ns: Any) -> frozenset[str]:
    """Attribute names the live namespace actually holds.

    The checked-in contracts are a superset of what the namespaces carry, so a
    write built from the contract alone would add attributes to a shared index.
    A namespace that does not exist yet (a fresh `_dev` twin) has none.
    """
    try:
        return frozenset(ns.schema().keys())
    except turbopuffer.NotFoundError:
        return frozenset()


def fetch_person_doc_ids(ns: Any, logical: str, person_ids: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Cloud document ids per person, read from the namespace itself.

    Reading the cloud (not deriving from the local index) is what makes an
    un-share reach documents this laptop never held.
    """
    key = NAMESPACE_BY_LOGICAL[logical].doc_key
    by_person: dict[str, list[str]] = {}
    for chunk in _chunks(list(person_ids), 200):
        last_id: str | None = None
        while True:
            base = (key, "In", list(chunk))
            filters = base if last_id is None else ("And", [base, ("id", "Gt", last_id)])
            response = ns.query(
                rank_by=["id", "asc"],
                filters=filters,
                top_k=QUERY_PAGE_SIZE,
                include_attributes=sorted({"id", key}),
                consistency=STRONG_CONSISTENCY,
            )
            rows = list(response.rows or [])
            for row in rows:
                by_person.setdefault(str(getattr(row, key)), []).append(str(row.id))
            if len(rows) < QUERY_PAGE_SIZE:
                break
            last_id = str(rows[-1].id)
    return {person_id: tuple(sorted(doc_ids)) for person_id, doc_ids in by_person.items()}


def fetch_present_ids(ns: Any, ids: Sequence[str]) -> frozenset[str]:
    present: set[str] = set()
    for chunk in _chunks(list(ids), BATCH_SIZE):
        response = ns.query(
            rank_by=["id", "asc"],
            filters=("id", "In", list(chunk)),
            top_k=len(chunk),
            include_attributes=["id"],
            consistency=STRONG_CONSISTENCY,
        )
        present.update(str(row.id) for row in (response.rows or []))
    return frozenset(present)


def upsert_docs(ns: Any, logical: str, rows: Sequence[dict[str, Any]]) -> int:
    for batch in _chunks(list(rows), BATCH_SIZE):
        ns.write(upsert_rows=list(batch), schema=NAMESPACE_BY_LOGICAL[logical].write_schema,
                 distance_metric=DISTANCE_METRIC)
    return len(rows)


def patch_allowed_operator_ids(ns: Any, allowed_by_doc_id: dict[str, tuple[str, ...]]) -> int:
    rows = [{"id": doc_id, "allowed_operator_ids": list(operators)}
            for doc_id, operators in sorted(allowed_by_doc_id.items())]
    for batch in _chunks(rows, BATCH_SIZE):
        ns.write(patch_rows=list(batch), schema=ALLOWED_OPERATOR_IDS_SCHEMA)
    return len(rows)
