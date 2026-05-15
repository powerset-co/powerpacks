#!/usr/bin/env python3
"""Resumable local Powerpacks search-index processing pipeline."""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
from argparse import Namespace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.artifacts import (  # noqa: E402
    build_company_corpus,
    build_education_corpus,
    build_location_corpus,
    build_summary_records,
)
from packs.indexing.lib.contracts import (  # noqa: E402
    count_defaulted_numeric,
    load_search_contract,
    normalize_record_for_contract,
    validate_jsonl,
)
from packs.indexing.lib.io import emit_json, read_jsonl, write_json, write_jsonl  # noqa: E402
from packs.indexing.lib.ledger import load_ledger, mark_step, save_ledger  # noqa: E402
from packs.indexing.lib.people import build_people_records, build_unified_profiles, flatten_people  # noqa: E402
from packs.indexing.primitives.enrich_roles_checkpointed import enrich_roles_checkpointed  # noqa: E402
from packs.indexing.primitives.embed_records_checkpointed import embed_records_checkpointed  # noqa: E402

STEPS = [
    "flatten_people",
    "build_roles",
    "embed_role_positions",
    "build_company_corpus",
    "embed_companies",
    "build_education_corpus",
    "build_location_corpus",
    "build_people_records",
    "build_unified_profiles",
    "build_summary_records",
    "embed_summaries",
    "build_vectors",
    "validate_contracts",
]


def run_dir(output_dir: Path, run_id: str) -> Path:
    return output_dir / run_id


def paths(rd: Path) -> dict[str, Path]:
    return {
        "ledger": rd / "ledger.json",
        "flattened": rd / "unified/flattened_people.jsonl",
        "unified_csv": rd / "unified/unified_person.csv",
        "profiles": rd / "profiles/hydrated_profiles.jsonl",
        "raw_titles": rd / "roles/raw_titles.jsonl",
        "role_mapping": rd / "roles/role_mapping.csv",
        "roles_dense": rd / "roles/roles_with_dense_text.jsonl",
        "roles_embeddings": rd / "roles/roles_with_embeddings.jsonl",
        "companies_corpus": rd / "company/companies_corpus_v3.jsonl",
        "companies_corpus_v3": rd / "company/companies_corpus_v3.jsonl",
        "schools_corpus": rd / "education/schools_corpus.jsonl",
        "people_education": rd / "education/people_education.jsonl",
        "locations_corpus": rd / "location/locations_corpus.jsonl",
        "summary_internal": rd / "summaries/summary_records.jsonl",
        "people_records": rd / "records/people.records.jsonl",
        "companies_records": rd / "records/companies.records.jsonl",
        "company_embeddings": rd / "company/company_embeddings_v3.jsonl",
        "schools_records": rd / "records/schools.records.jsonl",
        "education_records": rd / "records/education.records.jsonl",
        "summaries_records": rd / "records/summaries.records.jsonl",
        "vector_checkpoint": rd / "vectors/checkpoint.json",
        "summary_embeddings": rd / "unified/summary_embeddings.jsonl",
        "person_tech_skills": rd / "unified/person_tech_skills.jsonl",
        "summary_embeddings_legacy": rd / "summaries/summary_embeddings.jsonl",
        "person_tech_skills_legacy": rd / "summaries/person_tech_skills.jsonl",
        "aleph_roles_dir": rd / "unified/roles",
        "aleph_roles_dense": rd / "unified/roles/roles_with_dense_text_remapped.jsonl",
        "aleph_roles_embeddings": rd / "unified/roles/roles_with_embeddings.jsonl",
    }


def stats_path(ledger: dict[str, Any], name: str) -> Path:
    return Path(ledger["run_dir"]) / "stats" / f"{name}.json"


def write_stats(ledger: dict[str, Any], name: str, payload: dict[str, Any]) -> None:
    write_json(stats_path(ledger, name), payload)


def default_ledger(
    input_path: Path,
    rd: Path,
    run_id: str,
    default_operator_id: str | None = None,
    limit: int | None = None,
    *,
    checkpoint_every: int = 1000,
    role_provider: str = "local",
    allow_paid_role_provider: bool = False,
    embedding_provider: str = "local-fake",
    allow_paid_embeddings: bool = False,
) -> dict[str, Any]:
    return {
        "primitive": "build_processing_pipeline",
        "version": 1,
        "status": "pending",
        "run_id": run_id,
        "run_dir": str(rd),
        "input": str(input_path),
        "default_operator_id": default_operator_id,
        "limit": limit,
        "checkpoint_every": checkpoint_every,
        "role_provider": role_provider,
        "allow_paid_role_provider": allow_paid_role_provider,
        "embedding_provider": embedding_provider,
        "allow_paid_embeddings": allow_paid_embeddings,
        "steps": [{"id": step, "status": "pending"} for step in STEPS],
        "artifacts": {},
    }


def step_flatten(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    people = flatten_people(ledger["input"])
    if ledger.get("limit") is not None:
        people = people[: int(ledger["limit"])]
    write_jsonl(ps["flattened"], people)
    stats = {"people": len(people)}
    write_stats(ledger, "flatten_people", stats)
    return {"flattened_people": str(ps["flattened"])}, stats


class PipelinePartial(Exception):
    def __init__(self, step_id: str, artifacts: dict[str, str], stats: dict[str, Any]) -> None:
        super().__init__(f"partial step: {step_id}")
        self.step_id = step_id
        self.artifacts = artifacts
        self.stats = stats


def step_roles(ledger: dict[str, Any], ps: dict[str, Path], runtime: dict[str, Any] | None = None) -> tuple[dict[str, str], dict[str, Any]]:
    """Run mandatory checkpointed role enrichment; no scaffold fallback."""

    runtime = runtime or {}
    role_provider = str(ledger.get("role_provider") or "local")
    if role_provider != "local":
        if not ledger.get("allow_paid_role_provider"):
            raise SystemExit(f"role provider '{role_provider}' requires --allow-paid-role-provider; no paid API was called")
        raise SystemExit(f"role provider '{role_provider}' is not implemented in Powerpacks yet; no paid API was called")

    roles_dir = ps["roles_dense"].parent
    manifest = enrich_roles_checkpointed.run(
        Namespace(
            flattened=str(ps["flattened"]),
            output_dir=str(roles_dir),
            checkpoint_every=int(ledger.get("checkpoint_every") or 1000),
            provider=role_provider,
            force=False,
            stop_after_chunks=runtime.get("stop_after_role_chunks"),
        )
    )

    artifacts_out = {
        "roles_with_dense_text": str(ps["roles_dense"]),
        "roles_with_dense_text_remapped": str(roles_dir / "roles_with_dense_text_remapped.jsonl"),
        "raw_titles": str(ps["raw_titles"]),
        "role_mapping": str(ps["role_mapping"]),
        "role_checkpoint": str(roles_dir / "checkpoint.json"),
    }
    if manifest.get("status") == "partial":
        stats = {
            "status": "partial",
            "checkpointed": True,
            "checkpoint": manifest.get("checkpoint"),
            "chunks_written": int(manifest.get("chunks_written_total", 0) or 0),
            "input_rows_processed": int(manifest.get("input_rows_processed", 0) or 0),
            "checkpoint_every": int(ledger.get("checkpoint_every") or 1000),
            "provider": role_provider,
        }
        write_stats(ledger, "build_roles", stats)
        raise PipelinePartial("build_roles", artifacts_out, stats)

    remapped = roles_dir / "roles_with_dense_text_remapped.jsonl"
    if remapped.exists() and remapped != ps["roles_dense"]:
        shutil.copyfile(remapped, ps["roles_dense"])
    ps["aleph_roles_dir"].mkdir(parents=True, exist_ok=True)
    if remapped.exists():
        shutil.copyfile(remapped, ps["aleph_roles_dense"])

    counts = manifest.get("counts", {}) if isinstance(manifest.get("counts"), dict) else {}
    stats = {
        "status": "completed",
        "roles": int(counts.get("unique_roles", 0) or 0),
        "positions_seen": int(counts.get("positions_seen", 0) or 0),
        "input_rows_processed": int(counts.get("input_rows_processed", 0) or 0),
        "chunks_written": int(counts.get("chunks_written", 0) or 0),
        "checkpoint_every": int(ledger.get("checkpoint_every") or 1000),
        "provider": role_provider,
        "checkpointed": True,
        "provider_equivalence": manifest.get("provider_equivalence", "shape_compatible_not_tlm_equivalent"),
    }
    write_stats(ledger, "build_roles", stats)
    return artifacts_out, stats


def _word_tokenize(text: str) -> list[str]:
    import re

    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    return tokens + [f"{tokens[idx]} {tokens[idx + 1]}" for idx in range(len(tokens) - 1)]


def _embedding_provider_args(ledger: dict[str, Any]) -> tuple[str, bool]:
    provider = str(ledger.get("embedding_provider") or "local-fake")
    allow_paid = bool(ledger.get("allow_paid_embeddings"))
    if provider != "local-fake":
        if not allow_paid:
            raise SystemExit(f"embedding provider '{provider}' requires --allow-paid-embeddings; no paid API was called")
        raise SystemExit(f"embedding provider '{provider}' is not implemented in Powerpacks yet; no paid API was called")
    return provider, allow_paid


def _run_embedding_stage(
    ledger: dict[str, Any],
    input_path: Path,
    output_path: Path,
    checkpoint_dir: Path,
    id_field: str,
    text_fields: str,
    copy_fields: str,
    runtime: dict[str, Any],
    stop_key: str,
) -> dict[str, Any]:
    provider, allow_paid = _embedding_provider_args(ledger)
    return embed_records_checkpointed.run(
        Namespace(
            input=str(input_path),
            output=str(output_path),
            output_dir=str(checkpoint_dir),
            id_field=id_field,
            text_fields=text_fields,
            copy_fields=copy_fields,
            checkpoint_every=int(ledger.get("checkpoint_every") or 1000),
            provider=provider,
            allow_paid=allow_paid,
            force=False,
            stop_after_chunks=runtime.get(stop_key),
        )
    )


def _embedding_stats(result: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    counts = result.get("counts", {}) if isinstance(result.get("counts"), dict) else {}
    return {
        "status": result.get("status", "completed"),
        "provider": ledger.get("embedding_provider") or "local-fake",
        "dimension": 1536,
        "embeddings": int(counts.get("embeddings", result.get("embeddings_written", 0)) or 0),
        "input_rows_processed": int(counts.get("input_rows_processed", result.get("input_rows_processed", 0)) or 0),
        "chunks_written": int(counts.get("chunks_written", result.get("chunks_written_total", 0)) or 0),
        "checkpoint_every": int(ledger.get("checkpoint_every") or 1000),
    }


def step_role_embeddings(ledger: dict[str, Any], ps: dict[str, Path], runtime: dict[str, Any] | None = None) -> tuple[dict[str, str], dict[str, Any]]:
    runtime = runtime or {}
    result = _run_embedding_stage(
        ledger,
        ps["roles_dense"],
        ps["roles_embeddings"],
        ps["roles_dense"].parent / "embedding_checkpoints",
        "title_hash",
        "dense_text,raw_title,description",
        "title_hash,raw_title,description,dense_text,doc2query,inferred_skills,role_ids,role_track,seniority_band,cluster,role_type,specialization",
        runtime,
        "stop_after_embedding_chunks",
    )
    if result.get("status") == "partial":
        stats = _embedding_stats(result, ledger)
        write_stats(ledger, "embed_role_positions", stats)
        raise PipelinePartial("embed_role_positions", {"role_embeddings_checkpoint": str(ps["roles_dense"].parent / "embedding_checkpoints/checkpoint.json")}, stats)
    # Aleph upload contract names this field dense_embedding and keys by title_hash.
    rows = []
    for row in read_jsonl(ps["roles_embeddings"]):
        shaped = {key: row.get(key) for key in ["cluster", "dense_text", "description", "doc2query", "inferred_skills", "raw_title", "role_ids", "role_track", "role_type", "seniority_band", "specialization", "title_hash"] if key in row}
        shaped["dense_embedding"] = row.get("embedding", [])
        rows.append(shaped)
    write_jsonl(ps["roles_embeddings"], rows)
    ps["aleph_roles_dir"].mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ps["roles_embeddings"], ps["aleph_roles_embeddings"])
    stats = _embedding_stats(result, ledger)
    write_stats(ledger, "embed_role_positions", stats)
    return {"roles_with_embeddings": str(ps["roles_embeddings"])}, stats


def _load_by_id(path: Path, key: str = "id") -> dict[str, dict[str, Any]]:
    return {str(row.get(key)): row for row in read_jsonl(path) if row.get(key)}


def _role_hashes_for_flattened(people: list[dict[str, Any]]) -> list[str]:
    hashes: list[str] = []
    for person in people:
        for exp in person.get("work_experiences") or []:
            if not isinstance(exp, dict):
                continue
            title = str(exp.get("title") or exp.get("position_title") or exp.get("role") or "").strip()
            description = str(exp.get("description") or exp.get("summary") or "").strip()
            if title:
                hashes.append(enrich_roles_checkpointed.title_hash(title, description))
    return hashes


def _funding_stage_to_int(value: Any) -> int:
    mapping = {
        "PRE_SEED": 1, "SEED": 2, "SERIES_A": 3, "SERIES_B": 4, "SERIES_C": 5,
        "SERIES_D": 6, "SERIES_E": 7, "SERIES_F": 8, "SERIES_G": 9, "SERIES_H": 10,
        "SERIES_I": 11, "LATE_STAGE": 50, "IPO": 90, "PUBLIC": 91, "EXITED": 99,
        "VENTURE_UNKNOWN": 0,
    }
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return mapping.get(str(value).strip().upper(), 0)


def _company_corpus_to_aleph(row: dict[str, Any]) -> dict[str, Any]:
    name = str(row.get("company_name") or "")
    semantic = str(row.get("semantic_text") or row.get("description") or name)
    return {
        "company_urn": str(row.get("id") or row.get("company_urn") or ""),
        "company_name": name,
        "original_name": name,
        "name_aliases": [name] if name else [],
        "description": str(row.get("description") or ""),
        "city": str(row.get("city") or ""),
        "state": str(row.get("state") or ""),
        "country": str(row.get("country") or ""),
        "metro_area": str(row.get("metro_area") or ""),
        "macro_region": str(row.get("macro_region") or ""),
        "headcount": row.get("headcount"),
        "founded_year": row.get("founded_year"),
        "linkedin_url": str(row.get("linkedin_url") or ""),
        "logo_url": str(row.get("logo_url") or ""),
        "website_domain": str(row.get("website_domain") or ""),
        "funding_total": row.get("funding_total"),
        "funding_stage": row.get("funding_stage") or "VENTURE_UNKNOWN",
        "last_funding_at": row.get("last_funding_at"),
        "valuation": row.get("valuation"),
        "investor_urns": row.get("investor_urns") or [],
        "customer_type": row.get("customer_type") or "",
        "ownership_status": row.get("ownership_status") or "",
        "company_type": row.get("company_type") or "",
        "entity_types": row.get("entity_types") or [],
        "sector_types": row.get("sector_types") or [],
        "word_text": str(row.get("entity_sector_text") or row.get("word_text") or ""),
        "char_text": " ".join([name, str(row.get("website_domain") or "")]).strip(),
        "d2q_text": str(row.get("doc2query_text") or row.get("d2q_text") or ""),
        "doc2query": row.get("doc2query") or [],
        "semantic_text": semantic,
        "confidence_score": row.get("confidence_score") or 0.0,
    }


def _company_corpus_to_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["company_urn"],
        "company_name": row.get("company_name", ""),
        "name_aliases_text": " ".join(row.get("name_aliases") or []),
        "semantic_text": row.get("semantic_text", ""),
        "entity_sector_text": row.get("word_text", ""),
        "doc2query_text": row.get("d2q_text", ""),
        "website_domain": row.get("website_domain", ""),
        "linkedin_url": row.get("linkedin_url", ""),
        "description": row.get("description", ""),
        "headcount": row.get("headcount"),
        "funding_total": row.get("funding_total"),
        "funding_stage": _funding_stage_to_int(row.get("funding_stage")),
        "city": row.get("city", ""),
        "state": row.get("state", ""),
        "country": row.get("country", ""),
        "metro_area": row.get("metro_area", ""),
        "macro_region": row.get("macro_region", ""),
        "entity_types": row.get("entity_types") or [],
        "sector_types": row.get("sector_types") or [],
        "technology_types": row.get("technology_types") or [],
        "customer_type": row.get("customer_type") if isinstance(row.get("customer_type"), list) else ([row.get("customer_type")] if row.get("customer_type") else []),
        "investor_urns": row.get("investor_urns") or [],
        "yc_batches": row.get("yc_batches") or [],
        "founded_year": row.get("founded_year"),
        "last_funding_at": 0,
        "valuation": row.get("valuation"),
        "logo_url": row.get("logo_url", ""),
        "allowed_operator_ids": row.get("allowed_operator_ids") or [],
    }


def step_company(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    raw_corpus = build_company_corpus(read_jsonl(ps["flattened"]), ledger.get("default_operator_id"))
    aleph_corpus = [_company_corpus_to_aleph(row) for row in raw_corpus]
    record_inputs = []
    for raw, aleph in zip(raw_corpus, aleph_corpus):
        record = _company_corpus_to_record(aleph)
        record["allowed_operator_ids"] = raw.get("allowed_operator_ids") or [ledger.get("default_operator_id") or "local:user"]
        record_inputs.append(record)
    contract = load_search_contract("turbopuffer/companies.namespace.json")
    records = [normalize_record_for_contract(row, contract) for row in record_inputs]
    write_jsonl(ps["companies_corpus"], aleph_corpus)
    write_jsonl(ps["companies_corpus_v3"], aleph_corpus)
    write_jsonl(ps["companies_records"], records)
    stats = {"companies": len(records), "aleph_shape": "companies_corpus_v3", "defaulted_numeric_fields": {"companies": count_defaulted_numeric(record_inputs, contract)}}
    write_stats(ledger, "build_company_corpus", stats)
    return {"companies_corpus_v3": str(ps["companies_corpus_v3"]), "companies": str(ps["companies_records"])}, stats

def step_company_embeddings(ledger: dict[str, Any], ps: dict[str, Path], runtime: dict[str, Any] | None = None) -> tuple[dict[str, str], dict[str, Any]]:
    runtime = runtime or {}
    result = _run_embedding_stage(
        ledger,
        ps["companies_corpus_v3"],
        ps["company_embeddings"],
        ps["companies_corpus_v3"].parent / "embedding_checkpoints",
        "company_urn",
        "semantic_text,word_text,d2q_text,company_name,description",
        "company_urn,company_name,semantic_text",
        runtime,
        "stop_after_embedding_chunks",
    )
    if result.get("status") == "partial":
        stats = _embedding_stats(result, ledger)
        write_stats(ledger, "embed_companies", stats)
        raise PipelinePartial("embed_companies", {"company_embeddings_checkpoint": str(ps["companies_corpus"].parent / "embedding_checkpoints/checkpoint.json")}, stats)
    shaped_embeddings = []
    for row in read_jsonl(ps["company_embeddings"]):
        shaped_embeddings.append({
            "company_urn": row.get("id") or row.get("company_urn"),
            "company_name": row.get("company_name", ""),
            "semantic_text": row.get("semantic_text", ""),
            "embedding": row.get("embedding", []),
        })
    write_jsonl(ps["company_embeddings"], shaped_embeddings)
    embeddings = _load_by_id(ps["company_embeddings"], "company_urn")
    rows = []
    for row in read_jsonl(ps["companies_records"]):
        emb = embeddings.get(str(row.get("id")), {}).get("embedding")
        if emb:
            row["vector"] = emb
        rows.append(row)
    write_jsonl(ps["companies_records"], rows)
    stats = _embedding_stats(result, ledger)
    write_stats(ledger, "embed_companies", stats)
    return {"company_embeddings": str(ps["company_embeddings"]), "companies": str(ps["companies_records"])}, stats


def step_education(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    result = build_education_corpus(read_jsonl(ps["flattened"]), ledger.get("default_operator_id"))
    school_contract = load_search_contract("turbopuffer/schools.namespace.json")
    education_contract = load_search_contract("turbopuffer/education.namespace.json")
    school_records = [normalize_record_for_contract(row, school_contract) for row in result["schools"]]
    education_records = [normalize_record_for_contract(row, education_contract) | {"id": row["id"]} for row in result["education"]]
    aleph_schools = [
        {
            "entity_urn": row.get("id"),
            "school_name": row.get("school_name", ""),
            "linkedin_url": row.get("linkedin_url", ""),
            "logo_url": row.get("logo_url", ""),
            "person_count": row.get("person_count", 0),
            "degree_examples": [],
            "field_examples": [],
            "sparse_embedding": {},
            "duplicate_metadata": {},
        }
        for row in result["schools"]
    ]
    aleph_people_education = [
        {
            "person_id": row.get("person_id"),
            "education_id": row.get("canonical_education_id"),
            "degree_normalized": row.get("degree_normalized", ""),
            "field_of_study": row.get("field_of_study") or None,
            "graduation_year": row.get("graduation_year") or row.get("end_year"),
        }
        for row in result["education"]
    ]
    write_jsonl(ps["schools_corpus"], aleph_schools)
    write_jsonl(ps["people_education"], aleph_people_education)
    write_jsonl(ps["schools_records"], school_records)
    write_jsonl(ps["education_records"], education_records)
    stats = {
        "schools": len(school_records),
        "education": len(education_records),
        "aleph_shape": "schools_corpus/people_education",
        "defaulted_numeric_fields": {
            "schools": count_defaulted_numeric(result["schools"], school_contract),
            "education": count_defaulted_numeric(result["education"], education_contract),
        },
    }
    write_stats(ledger, "build_education_corpus", stats)
    return {"schools_corpus": str(ps["schools_corpus"]), "people_education": str(ps["people_education"]), "schools": str(ps["schools_records"]), "education": str(ps["education_records"])}, stats

def step_location(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    locations = build_location_corpus(read_jsonl(ps["flattened"]) + read_jsonl(ps["companies_corpus"]))
    write_jsonl(ps["locations_corpus"], locations)
    stats = {"locations": len(locations), "internal_only": True}
    write_stats(ledger, "build_location_corpus", stats)
    return {"locations": str(ps["locations_corpus"])}, stats


def step_people(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    people = read_jsonl(ps["flattened"])
    records = build_people_records(people, default_operator_id=ledger.get("default_operator_id"))
    role_data = _load_by_id(ps["roles_dense"], "title_hash")
    role_embeddings = _load_by_id(ps["roles_embeddings"], "title_hash")
    hashes = _role_hashes_for_flattened(people)
    enriched = []
    for idx, record in enumerate(records):
        role_hash = hashes[idx] if idx < len(hashes) else ""
        role = role_data.get(role_hash, {})
        embedding_row = role_embeddings.get(role_hash, {})
        if role:
            record["seniority_band"] = role.get("seniority_band") or record.get("seniority_band", "")
            record["role_track"] = role.get("role_track") or record.get("role_track", "")
            record["role_ids"] = role.get("role_ids") or record.get("role_ids", [])
            d2q_parts = []
            for value in role.get("doc2query") or []:
                if value:
                    d2q_parts.append(str(value))
            for value in role.get("inferred_skills") or []:
                if value:
                    d2q_parts.append(str(value))
            if record.get("role_track"):
                d2q_parts.append(str(record["role_track"]))
            record["d2q_tokens"] = _word_tokenize(" ".join(d2q_parts)) if d2q_parts else record.get("d2q_tokens", [])
        vector = embedding_row.get("dense_embedding") or embedding_row.get("embedding")
        if vector:
            record["vector"] = vector
        enriched.append(record)
    contract = load_search_contract("turbopuffer/people.namespace.json")
    normalized = [normalize_record_for_contract(row, contract) for row in enriched]
    for out, src in zip(normalized, enriched):
        if isinstance(src.get("vector"), list):
            out["vector"] = src["vector"]
    write_jsonl(ps["people_records"], normalized)
    stats = {
        "people_records": len(normalized),
        "with_vectors": sum(1 for row in normalized if row.get("vector")),
        "defaulted_numeric_fields": {"people": count_defaulted_numeric(enriched, contract)},
        "allowed_operator_ids_default": ledger.get("default_operator_id") or "local:user",
    }
    write_stats(ledger, "build_people_records", stats)
    return {"people": str(ps["people_records"])}, stats


def step_profiles(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    profiles = build_unified_profiles(read_jsonl(ps["flattened"]))
    write_jsonl(ps["profiles"], profiles)
    ps["unified_csv"].parent.mkdir(parents=True, exist_ok=True)
    with ps["unified_csv"].open("w", newline="", encoding="utf-8") as handle:
        fieldnames = ["id", "full_name", "linkedin_url", "headline", "summary", "location_raw"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for profile in profiles:
            writer.writerow(
                {
                    "id": profile["id"],
                    "full_name": profile.get("name", ""),
                    "linkedin_url": profile.get("linkedin_url") or "",
                    "headline": profile.get("headline") or "",
                    "summary": profile.get("summary") or "",
                    "location_raw": profile.get("location") or "",
                }
            )
    stats = {"profiles": len(profiles)}
    write_stats(ledger, "build_unified_profiles", stats)
    return {"profiles": str(ps["profiles"]), "unified_person": str(ps["unified_csv"])}, stats


def step_summary(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    result = build_summary_records(read_jsonl(ps["profiles"]), ledger.get("default_operator_id"))
    contract = load_search_contract("turbopuffer/summaries.namespace.json")
    records = [normalize_record_for_contract(row, contract) for row in result["summaries"]]
    write_jsonl(ps["summary_internal"], result["internal_text"])
    skills_rows = [{"person_id": row["id"], "tech_skills": row.get("tech_skills", [])} for row in records]
    write_jsonl(ps["person_tech_skills"], skills_rows)
    write_jsonl(ps["person_tech_skills_legacy"], skills_rows)
    write_jsonl(ps["summaries_records"], records)
    stats = {"summaries": len(records), "person_tech_skills": len(records), "defaulted_numeric_fields": {"summaries": count_defaulted_numeric(result["summaries"], contract)}}
    write_stats(ledger, "build_summary_records", stats)
    return {"summaries": str(ps["summaries_records"])}, stats


def step_summary_embeddings(ledger: dict[str, Any], ps: dict[str, Path], runtime: dict[str, Any] | None = None) -> tuple[dict[str, str], dict[str, Any]]:
    runtime = runtime or {}
    result = _run_embedding_stage(
        ledger,
        ps["summary_internal"],
        ps["summary_embeddings"],
        ps["summary_internal"].parent / "embedding_checkpoints",
        "person_id",
        "text",
        "person_id,base_id,text",
        runtime,
        "stop_after_embedding_chunks",
    )
    if result.get("status") == "partial":
        stats = _embedding_stats(result, ledger)
        write_stats(ledger, "embed_summaries", stats)
        raise PipelinePartial("embed_summaries", {"summary_embeddings_checkpoint": str(ps["summary_internal"].parent / "embedding_checkpoints/checkpoint.json")}, stats)
    shaped_embeddings = []
    for row in read_jsonl(ps["summary_embeddings"]):
        shaped_embeddings.append({"person_id": row.get("id") or row.get("person_id"), "embedding": row.get("embedding", [])})
    write_jsonl(ps["summary_embeddings"], shaped_embeddings)
    shutil.copyfile(ps["summary_embeddings"], ps["summary_embeddings_legacy"])
    embeddings = _load_by_id(ps["summary_embeddings"], "person_id")
    text_by_person = {str(row.get("person_id")): str(row.get("text") or "") for row in read_jsonl(ps["summary_internal"]) if row.get("person_id")}
    rows = []
    for row in read_jsonl(ps["summaries_records"]):
        pid = str(row.get("id"))
        text = text_by_person.get(pid, "")
        row["summary"] = text
        row["summary_tokens"] = _word_tokenize(text)
        emb = embeddings.get(pid, {}).get("embedding")
        if emb:
            row["vector"] = emb
        rows.append(row)
    write_jsonl(ps["summaries_records"], rows)
    stats = _embedding_stats(result, ledger)
    write_stats(ledger, "embed_summaries", stats)
    return {"summary_embeddings": str(ps["summary_embeddings"]), "person_tech_skills": str(ps["person_tech_skills"]), "summaries": str(ps["summaries_records"])}, stats


def step_vectors(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    """Compatibility aggregate vector checkpoint after per-surface embedding stages."""

    def count_vectors(path: Path) -> int:
        return sum(1 for row in read_jsonl(path) if isinstance(row.get("vector"), list) and len(row.get("vector")) == 1536)

    counts = {
        "people": count_vectors(ps["people_records"]),
        "summaries": count_vectors(ps["summaries_records"]),
        "companies": count_vectors(ps["companies_records"]),
    }
    checkpoint = {
        "status": "completed",
        "stage": "build_vectors",
        "provider": "local_deterministic_no_spend",
        "dimension": 1536,
        "checkpoint_every": int(ledger.get("checkpoint_every") or 1000),
        "counts": counts,
        "sources": {
            "people": str(ps["roles_embeddings"]),
            "summaries": str(ps["summary_embeddings"]),
            "companies": str(ps["company_embeddings"]),
        },
    }
    write_json(ps["vector_checkpoint"], checkpoint)
    stats = {"status": "completed", "checkpointed": True, "provider": "local_deterministic_no_spend", "dimension": 1536, "counts": counts, "checkpoint": str(ps["vector_checkpoint"])}
    write_stats(ledger, "build_vectors", stats)
    return {"vector_checkpoint": str(ps["vector_checkpoint"])}, stats


def _allow_vector_only_validation(result: dict[str, Any]) -> dict[str, Any]:
    """Allow vector-backed local records even when checked-in TP contracts lag.

    Some local contracts in older commits do not declare vector metadata yet.
    The processing pipeline still needs to emit vector-backed records, so only
    suppress validation failures whose sole issue is an extra `vector` field.
    """

    errors = result.get("errors") or []
    if not errors:
        return result
    filtered = []
    for error in errors:
        extra = set(error.get("extra") or [])
        other = {key: value for key, value in error.items() if key not in {"line", "ok", "missing", "extra", "errors"}}
        nested_errors = error.get("errors") or []
        missing = error.get("missing") or []
        if extra == {"vector"} and not missing and not nested_errors and not other:
            continue
        filtered.append(error)
    if len(filtered) != len(errors):
        result = dict(result)
        result["errors"] = filtered
        result["ok"] = not filtered
        result["vector_extra_allowed"] = True
    return result


def step_validate(ledger: dict[str, Any], ps: dict[str, Path]) -> tuple[dict[str, str], dict[str, Any]]:
    validation = {}
    for name, path, contract in [
        ("people", ps["people_records"], "turbopuffer/people.namespace.json"),
        ("companies", ps["companies_records"], "turbopuffer/companies.namespace.json"),
        ("schools", ps["schools_records"], "turbopuffer/schools.namespace.json"),
        ("education", ps["education_records"], "turbopuffer/education.namespace.json"),
        ("summaries", ps["summaries_records"], "turbopuffer/summaries.namespace.json"),
    ]:
        validation[name] = _allow_vector_only_validation(validate_jsonl(path, contract))
    validation["locations"] = {"internal_only": True, "path": str(ps["locations_corpus"])}
    write_stats(ledger, "validate_contracts", validation)
    return {}, {"validation": validation}


STEP_FUNCTIONS = {
    "flatten_people": step_flatten,
    "build_roles": step_roles,
    "embed_role_positions": step_role_embeddings,
    "build_company_corpus": step_company,
    "embed_companies": step_company_embeddings,
    "build_education_corpus": step_education,
    "build_location_corpus": step_location,
    "build_people_records": step_people,
    "build_unified_profiles": step_profiles,
    "build_summary_records": step_summary,
    "embed_summaries": step_summary_embeddings,
    "build_vectors": step_vectors,
    "validate_contracts": step_validate,
}


def execute(ledger_path: Path, runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = runtime or {}
    ledger = load_ledger(ledger_path)
    existing_steps = {str(item.get("id")) for item in ledger.get("steps", [])}
    for step in STEPS:
        if step not in existing_steps:
            ledger.setdefault("steps", []).append({"id": step, "status": "pending"})
    ps = paths(Path(ledger["run_dir"]))
    for step in STEPS:
        current = next(item for item in ledger["steps"] if item["id"] == step)
        if current.get("status") == "completed":
            continue
        try:
            if step == "build_roles":
                artifacts, stats = step_roles(ledger, ps, runtime)
            elif step in {"embed_role_positions", "embed_companies", "embed_summaries"}:
                artifacts, stats = STEP_FUNCTIONS[step](ledger, ps, runtime)
            else:
                artifacts, stats = STEP_FUNCTIONS[step](ledger, ps)
        except PipelinePartial as partial:
            ledger = mark_step(ledger_path, ledger, partial.step_id, "partial", artifacts=partial.artifacts, stats=partial.stats)
            ledger["status"] = "partial"
            save_ledger(ledger_path, ledger)
            return ledger
        ledger = mark_step(ledger_path, ledger, step, "completed", artifacts=artifacts, stats=stats)
    ledger["status"] = "completed"
    save_ledger(ledger_path, ledger)
    return ledger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    plan = sub.add_parser("plan")
    plan.add_argument("--input", required=True)
    plan.add_argument("--output-dir", required=True)
    plan.add_argument("--run-id", required=True)
    run = sub.add_parser("run")
    run.add_argument("--input", required=True)
    run.add_argument("--output-dir", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--default-operator-id", default=None)
    run.add_argument("--limit", type=int)
    run.add_argument("--checkpoint-every", type=int, default=1000)
    run.add_argument("--role-provider", choices=["local", "tlm"], default="local")
    run.add_argument("--allow-paid-role-provider", action="store_true")
    run.add_argument("--embedding-provider", choices=["local-fake", "openai"], default="local-fake")
    run.add_argument("--allow-paid-embeddings", action="store_true")
    run.add_argument("--stop-after-role-chunks", type=int, help="Test hook: stop after N role chunks and leave the run resumable")
    run.add_argument("--stop-after-embedding-chunks", type=int, help="Test hook: stop after N embedding chunks and leave the run resumable")
    run.add_argument("--force", action="store_true")
    cont = sub.add_parser("continue")
    cont.add_argument("--ledger", required=True)
    status = sub.add_parser("status")
    status.add_argument("--ledger", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.cmd == "plan":
        rd = run_dir(Path(args.output_dir), args.run_id)
        ps = paths(rd)
        emit_json(
            {
                "run_dir": str(rd),
                "artifacts": {key: str(value) for key, value in ps.items()},
                "steps": STEPS,
                "dvc_scope_matrix": "ported local deterministic stages only",
                "disabled": ["remote writes", "network calls", "LLM spend", "paid embeddings"],
            }
        )
        return
    if args.cmd == "run":
        rd = run_dir(Path(args.output_dir), args.run_id)
        if rd.exists():
            if args.force:
                shutil.rmtree(rd)
            else:
                ledger_path = paths(rd)["ledger"]
                raise SystemExit(f"run directory already exists: {rd}. Use continue --ledger {ledger_path} or rerun with --force.")
        rd.mkdir(parents=True, exist_ok=True)
        ledger = default_ledger(
            Path(args.input),
            rd,
            args.run_id,
            args.default_operator_id,
            args.limit,
            checkpoint_every=args.checkpoint_every,
            role_provider=args.role_provider,
            allow_paid_role_provider=args.allow_paid_role_provider,
            embedding_provider=args.embedding_provider,
            allow_paid_embeddings=args.allow_paid_embeddings,
        )
        ledger_path = paths(rd)["ledger"]
        save_ledger(ledger_path, ledger)
        ledger = execute(ledger_path, {"stop_after_role_chunks": args.stop_after_role_chunks, "stop_after_embedding_chunks": args.stop_after_embedding_chunks})
        emit_json({"status": ledger["status"], "run_dir": str(rd), "counts": {step["id"]: step.get("stats", {}) for step in ledger["steps"]}})
        return
    if args.cmd == "continue":
        ledger = execute(Path(args.ledger))
        emit_json({"status": ledger["status"], "run_dir": ledger["run_dir"]})
        return
    if args.cmd == "status":
        emit_json(load_ledger(args.ledger))
        return
    build_parser().error("subcommand required")


if __name__ == "__main__":
    main()
