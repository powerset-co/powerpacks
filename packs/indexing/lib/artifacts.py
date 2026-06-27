"""Local company, education, location, and summary artifact builders."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from packs.ingestion.schemas.company_identity import resolve_company_identity
from packs.ingestion.schemas.people_schema import parse_jsonish
from packs.indexing.lib.identity import (
    canonical_person_key,
    company_uuid,
    education_edge_uuid,
    person_uuid,
    school_uuid,
    stable_location_id_from_key,
    summary_uuid,
)


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _key_text(value: Any) -> str:
    return re.sub(r"\s+", " ", _clean(value).lower()).strip()


def _json_list(value: Any) -> list[Any]:
    parsed = parse_jsonish(value, value)
    return parsed if isinstance(parsed, list) else []


def _first(mapping: dict[str, Any], *fields: str) -> str:
    for field in fields:
        value = _clean(mapping.get(field))
        if value:
            return value
    return ""


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        value = value.get("year") or value.get("start_year") or value.get("end_year")
    try:
        return int(value)
    except (TypeError, ValueError):
        match = re.search(r"\b(19\d{2}|20\d{2})\b", str(value))
        return int(match.group(1)) if match else None


def _year(mapping: dict[str, Any], *fields: str) -> int | None:
    for field in fields:
        year = _int_or_none(mapping.get(field))
        if year is not None:
            return year
    return None


def stable_person_uuid(row: dict[str, Any]) -> str:
    """Return the deterministic Powerpacks UUIDv5 for a person row.

    Imported rows may carry legacy IDs (including valid non-v5 UUIDs). The local
    indexing pipeline should not preserve those as primary IDs; it always derives
    person IDs from the canonical person key so artifact builders stay consistent
    with ``flatten_people()`` and the stable UUIDv5 contract.
    """

    return person_uuid(canonical_person_key(row))


def company_canonical_key(data: dict[str, Any]) -> str:
    for field in ("rapidapi_company_id", "company_id", "companyId"):
        value = _clean(data.get(field))
        if value and not value.lower().startswith("urn:harmonic:"):
            return f"rapidapi:{value}"
    company_key = _clean(data.get("company_key"))
    if company_key.startswith("rapidapi:") or company_key.startswith("linkedin_company:"):
        return company_key
    for field in ("company_public_identifier", "public_identifier", "linkedin_slug"):
        value = _key_text(data.get(field))
        if value:
            return f"linkedin_company:{value}"
    identity = resolve_company_identity(data)
    if identity.get("rapidapi_company_id"):
        return f"rapidapi:{identity['rapidapi_company_id']}"
    if identity.get("company_public_identifier"):
        return f"linkedin_company:{identity['company_public_identifier']}"
    name = _key_text(data.get("company_name") or data.get("company") or data.get("organization") or data.get("name"))
    return f"name:{name}"


def stable_company_uuid(data: dict[str, Any]) -> str:
    return company_uuid(company_canonical_key(data))


def school_canonical_key(data: dict[str, Any]) -> str:
    for field in ("school_id", "school_urn", "entity_urn", "linkedin_school_id"):
        value = _clean(data.get(field))
        if value and not value.lower().startswith("urn:harmonic:"):
            return f"provider:{value}"
    name = _key_text(data.get("school_name") or data.get("school") or data.get("name"))
    return f"name:{name}"


def stable_school_uuid(data: dict[str, Any]) -> str:
    return school_uuid(school_canonical_key(data))


def stable_education_edge_uuid(person_id: str, school_id: str, degree: str = "", field_of_study: str = "", start_year: Any = "", end_year: Any = "") -> str:
    key = "|".join(_clean(v) for v in (degree, field_of_study, start_year, end_year))
    return education_edge_uuid(person_id, school_id, key)


def stable_location_uuid(location: dict[str, Any]) -> str:
    key = "|".join(_key_text(location.get(k)) for k in ("city", "state", "country", "location_raw"))
    return stable_location_id_from_key(key)


def stable_summary_uuid(person_id: str) -> str:
    return summary_uuid(person_id)


def _company_from_experience(exp: dict[str, Any]) -> dict[str, Any]:
    identity = resolve_company_identity(exp)
    key_data = identity if (identity.get("rapidapi_company_id") or identity.get("company_public_identifier")) else exp
    name = identity.get("company_name") or _first(exp, "company_name", "company", "organization", "name")
    description = _first(exp, "description", "summary")
    return {
        "id": stable_company_uuid(key_data),
        "company_name": name,
        "name_aliases_text": name,
        "semantic_text": " ".join(part for part in [name, description, _first(exp, "industry", "sector")] if part),
        "entity_sector_text": _first(exp, "industry", "sector", "entity_sector_text"),
        "doc2query_text": "",
        "website_domain": _first(exp, "website_domain", "domain"),
        "linkedin_url": identity.get("company_linkedin_url") or _first(exp, "company_linkedin_url", "linkedin_url"),
        "description": description,
        "city": _first(exp, "city"),
        "state": _first(exp, "state"),
        "country": _first(exp, "country"),
        "metro_area": _first(exp, "metro_area"),
        "macro_region": _first(exp, "macro_region"),
        "entity_types": [],
        "sector_types": [],
        "technology_types": [],
        "customer_type": [],
        "investor_urns": [],
        "yc_batches": [],
        "allowed_operator_ids": [],
        "rapidapi_company_id": identity.get("rapidapi_company_id", ""),
        "company_public_identifier": identity.get("company_public_identifier", ""),
        "company_key": identity.get("company_key", ""),
        "canonical_key": company_canonical_key(key_data),
    }


def build_company_corpus(people_rows: list[dict[str, Any]], default_operator_id: str | None = None) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    for row in people_rows or []:
        for exp in _json_list(row.get("work_experiences")):
            if not isinstance(exp, dict):
                continue
            company = _company_from_experience(exp)
            company["allowed_operator_ids"] = _allowed_operator_ids(row, default_operator_id)
            if not company["company_name"] and not (company["rapidapi_company_id"] or company["linkedin_url"]):
                continue
            cid = company["id"]
            counts[cid] += 1
            existing = by_id.setdefault(cid, company)
            if existing is not company:
                for key, value in company.items():
                    if value not in (None, "", []) and existing.get(key) in (None, "", []):
                        existing[key] = value
    return [{**by_id[cid], "person_count": counts[cid]} for cid in sorted(by_id)]


def _allowed_operator_ids(row: dict[str, Any], default_operator_id: str | None = None) -> list[str]:
    parsed = parse_jsonish(row.get("allowed_operator_ids") or row.get("operator_ids"), None)
    if isinstance(parsed, list):
        return [_clean(v) for v in parsed if _clean(v)]
    text = _clean(parsed or "")
    ids = [part.strip() for part in text.split(",") if part.strip()]
    return ids or [default_operator_id or "local:user"]


def _school_name(edu: dict[str, Any]) -> str:
    school = edu.get("school")
    if isinstance(school, dict):
        return _first(school, "name", "school_name")
    return _first(edu, "school_name", "school", "name")


def _degree_normalized(degree: str) -> str:
    compact = re.sub(r"[^a-z]", "", degree.lower())
    if compact in {"bs", "bsc", "ba", "bachelors", "bachelor", "bachelorofscience"}:
        return "Bachelors"
    if compact == "mba":
        return "MBA"
    if compact in {"ms", "msc", "ma", "masters", "master", "masterofscience"}:
        return "Masters"
    if compact in {"phd", "doctorate"}:
        return "PhD"
    return degree.strip().title() if degree else ""


def _education_record(row: dict[str, Any], edu: dict[str, Any], default_operator_id: str | None = None) -> dict[str, Any] | None:
    school_name = _school_name(edu)
    if not school_name:
        return None
    person_uuid = stable_person_uuid(row)
    school_payload = {**edu, "school_name": school_name}
    school_uuid = stable_school_uuid(school_payload)
    degree = _first(edu, "degree", "degree_name")
    field = _first(edu, "field_of_study", "field", "major")
    start_year = _year(edu, "start_year", "starts_at", "start_date")
    end_year = _year(edu, "end_year", "ends_at", "end_date")
    graduation_year = _year(edu, "graduation_year", "graduated_at") or end_year
    return {
        "id": stable_education_edge_uuid(person_uuid, school_uuid, degree, field, start_year or "", end_year or ""),
        "person_id": person_uuid,
        "base_id": person_uuid,
        "canonical_education_id": school_uuid,
        "school_name": school_name,
        "degree": degree,
        "degree_normalized": _degree_normalized(degree),
        "field_of_study": field,
        "start_year": start_year,
        "end_year": end_year,
        "graduation_year": graduation_year,
        "allowed_operator_ids": _allowed_operator_ids(row, default_operator_id),
        "school_canonical_key": school_canonical_key(school_payload),
    }


def build_education_corpus(people_rows: list[dict[str, Any]], default_operator_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    education_by_id: dict[str, dict[str, Any]] = {}
    schools: dict[str, dict[str, Any]] = {}
    school_people: dict[str, set[str]] = {}
    for row in people_rows or []:
        for edu in _json_list(row.get("education")):
            if not isinstance(edu, dict):
                continue
            rec = _education_record(row, edu, default_operator_id)
            if not rec:
                continue
            education_by_id[rec["id"]] = rec
            sid = rec["canonical_education_id"]
            schools.setdefault(sid, {"id": sid, "school_name": rec["school_name"], "person_count": 0, "canonical_key": rec["school_canonical_key"]})
            school_people.setdefault(sid, set()).add(rec["person_id"])
    for sid, people in school_people.items():
        schools[sid]["person_count"] = len(people)
    return {"education": [education_by_id[k] for k in sorted(education_by_id)], "schools": [schools[k] for k in sorted(schools)]}


def build_location_corpus(people_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    people: dict[str, set[str]] = {}
    for row in people_rows or []:
        loc = {
            "city": _first(row, "city"),
            "state": _first(row, "state"),
            "country": _first(row, "country"),
            "location_raw": _first(row, "location_raw", "location"),
            "metro_area": _first(row, "metro_area"),
            "macro_region": _first(row, "macro_region"),
        }
        if not any(loc.values()):
            continue
        lid = stable_location_uuid(loc)
        by_id.setdefault(lid, {"id": lid, **loc, "person_count": 0})
        people.setdefault(lid, set()).add(stable_person_uuid(row))
    for lid, person_ids in people.items():
        by_id[lid]["person_count"] = len(person_ids)
    return [by_id[k] for k in sorted(by_id)]


def _summary_text(row: dict[str, Any]) -> str:
    parts = [_first(row, "full_name"), _first(row, "headline"), _first(row, "summary")]
    experiences: list[str] = []
    for exp in _json_list(row.get("work_experiences"))[:3]:
        if isinstance(exp, dict):
            title = _first(exp, "title", "position", "role")
            company = _first(exp, "company_name", "company", "organization")
            if title or company:
                experiences.append(" at ".join(p for p in [title, company] if p))
    if experiences:
        parts.append("Experience: " + "; ".join(experiences))
    schools = [_school_name(edu) for edu in _json_list(row.get("education"))[:2] if isinstance(edu, dict) and _school_name(edu)]
    if schools:
        parts.append("Education: " + "; ".join(schools))
    return "\n".join(part for part in parts if part).strip()


def _tech_skills(row: dict[str, Any], text: str) -> list[str]:
    parsed = parse_jsonish(row.get("tech_skills"), None)
    if isinstance(parsed, list):
        return [_clean(v) for v in parsed if _clean(v)]
    skills: list[str] = []
    labels = {"python": "Python", "javascript": "JavaScript", "typescript": "TypeScript", "react": "React", "ai": "AI", "ml": "ML", "machine learning": "Machine Learning", "infrastructure": "Infrastructure", "security": "Security"}
    lowered = text.lower()
    for needle, label in labels.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", lowered) and label not in skills:
            skills.append(label)
    return skills


def build_summary_records(people_rows: list[dict[str, Any]], default_operator_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    internal: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for row in people_rows or []:
        pid = _first(row, "base_id", "person_id", "id") or stable_person_uuid(row)
        text = _summary_text(row)
        internal.append({"id": stable_summary_uuid(pid), "person_id": pid, "base_id": pid, "text": text})
        summaries.append({"id": pid, "tech_skills": _tech_skills(row, text), "allowed_operator_ids": _allowed_operator_ids(row, default_operator_id)})
    return {"internal_text": sorted(internal, key=lambda r: r["id"]), "summaries": sorted(summaries, key=lambda r: r["id"])}


def jsonl_dumps(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows)
