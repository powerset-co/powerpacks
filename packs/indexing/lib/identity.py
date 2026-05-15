"""Stable local UUIDv5 identity helpers for indexing artifacts.

All IDs emitted by the local indexing pipeline are UUIDv5 strings under the
checked-in Powerpacks namespace below. Provider identifiers or their canonical
fallback keys are the only inputs to these helpers; no network lookups are
performed.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

try:
    from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url
except ModuleNotFoundError:  # pragma: no cover - direct script fallback
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url

# Fixed namespace for Powerpacks local indexing artifacts. Do not change: doing
# so would invalidate generated person/company/position/school/summary IDs.
POWERPACKS_INDEXING_NAMESPACE = uuid.UUID("7b6f8a9e-2d68-4a7b-8cf7-6d7f0f3f8a42")

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _digest(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _canonical(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_email(value: Any) -> str:
    return _canonical(value)


def normalize_phone(value: Any) -> str:
    return re.sub(r"\D+", "", "" if value is None else str(value))


def normalize_name(value: Any) -> str:
    value = _canonical(value)
    value = _NON_ALNUM.sub(" ", value)
    return " ".join(value.split())


def stable_uuid5(key: str) -> str:
    """Return the shared Powerpacks UUIDv5 for an already canonical key."""

    return str(uuid.uuid5(POWERPACKS_INDEXING_NAMESPACE, _canonical(key)))


def _company_key(company: Any) -> str:
    if isinstance(company, dict):
        for field in ("company_urn", "current_company_urn", "company_id", "rapidapi_company_id", "company_public_identifier"):
            value = _canonical(company.get(field))
            if value:
                return f"{field}:{value}"
        name = normalize_name(company.get("company_name") or company.get("company") or company.get("name") or company.get("current_company"))
        domain = _canonical(company.get("website_domain") or company.get("domain"))
        location = normalize_name(" ".join(str(company.get(k, "")) for k in ("city", "state", "country")))
        return "|".join(part for part in ["name:" + name, "domain:" + domain if domain else "", "location:" + location if location else ""] if part)
    return _canonical(company)


def _school_key(school: Any) -> str:
    if isinstance(school, dict):
        for field in ("school_id", "school_urn", "entity_urn", "linkedin_school_id"):
            value = _canonical(school.get(field))
            if value:
                return f"{field}:{value}"
        name = normalize_name(school.get("school_name") or school.get("school") or school.get("name"))
        location = normalize_name(" ".join(str(school.get(k, "")) for k in ("country", "state")))
        return "|".join(part for part in ["name:" + name, "location:" + location if location else ""] if part)
    return _canonical(school)


def person_uuid(row_or_key: Any) -> str:
    key = canonical_person_key(row_or_key) if isinstance(row_or_key, dict) else str(row_or_key or "")
    return stable_uuid5(f"person:{key}")


def company_uuid(company: Any) -> str:
    return stable_uuid5(f"company:{_company_key(company)}")


def school_uuid(school: Any) -> str:
    return stable_uuid5(f"school:{_school_key(school)}")


def position_uuid(person_id: str, position: Any) -> str:
    if isinstance(position, dict):
        title = normalize_name(position.get("title") or position.get("position_title") or position.get("role"))
        company = _company_key(position) or normalize_name(position.get("company_name") or position.get("company"))
        start = _canonical(position.get("starts_at") or position.get("start_date") or position.get("start_year"))
        end = _canonical(position.get("ends_at") or position.get("end_date") or position.get("end_year"))
        desc = _digest(_canonical(position.get("description") or position.get("summary")))
        position_key = "|".join([title, company, start, end, desc])
    else:
        position_key = str(position or "")
    return stable_uuid5(f"position:{person_id}:{position_key}")


def education_edge_uuid(person_id: str, school_id: str, education: Any = "") -> str:
    if isinstance(education, dict):
        parts = [education.get("degree") or education.get("degree_name"), education.get("field_of_study") or education.get("field") or education.get("major"), education.get("start_year") or education.get("starts_at"), education.get("end_year") or education.get("ends_at")]
        edge_key = "|".join(_canonical(p) for p in parts)
    else:
        edge_key = str(education or "")
    return stable_uuid5(f"education-edge:{person_id}:{school_id}:{edge_key}")


def summary_uuid(person_id: str) -> str:
    return stable_uuid5(f"summary:{person_id}")


def stable_person_id_from_key(key: str) -> str:
    return person_uuid(key)


def stable_company_id_from_key(key: str) -> str:
    return company_uuid(key)


def stable_school_id_from_key(key: str) -> str:
    return school_uuid(key)


def stable_education_edge_id_from_key(key: str) -> str:
    return stable_uuid5(f"education-edge:{key}")


def stable_location_id_from_key(key: str) -> str:
    return stable_uuid5(f"location:{key}")


def stable_summary_id_from_person_id(person_id: str) -> str:
    return summary_uuid(person_id)


def stable_person_id(*, public_identifier: str = "", linkedin_url: str = "", email: str = "", phone: str = "", name: str = "") -> str:
    public_identifier = _canonical(public_identifier) or extract_public_identifier(linkedin_url or "")
    if public_identifier:
        return person_uuid(f"linkedin:{public_identifier}")
    email = normalize_email(email)
    if email:
        return person_uuid(f"email:{email}")
    phone = normalize_phone(phone)
    if phone:
        return person_uuid(f"phone:{phone}")
    return person_uuid(f"person:{normalize_name(name)}")


def stable_company_id(key: str) -> str:
    return company_uuid(key) if (key or "").strip() else ""


def canonical_person_key(row: dict[str, Any]) -> str:
    """Return the best local stable provider key for a canonical people row."""

    public_id = _canonical(row.get("public_identifier"))
    if not public_id:
        public_id = extract_public_identifier(row.get("linkedin_url") or "")
    if public_id:
        return f"linkedin:{public_id}"

    row_id = str(row.get("id") or "").strip()
    if row_id:
        return f"id:{row_id}"

    email = normalize_email(row.get("primary_email") or (row.get("all_emails") or "").split(",")[0])
    if email:
        return f"email:{email}"

    phone = normalize_phone(row.get("primary_phone") or (row.get("all_phones") or "").split(",")[0])
    if phone:
        return f"phone:{phone}"

    name = normalize_name(row.get("full_name") or " ".join(str(row.get(k, "")) for k in ("first_name", "last_name")))
    context = normalize_name(" ".join(str(row.get(k, "")) for k in ("current_company", "city", "state", "country")))
    return f"person:{_digest('|'.join([name, context]))}"


__all__ = [
    "POWERPACKS_INDEXING_NAMESPACE",
    "canonical_person_key",
    "company_uuid",
    "education_edge_uuid",
    "extract_public_identifier",
    "normalize_email",
    "normalize_linkedin_url",
    "normalize_name",
    "normalize_phone",
    "person_uuid",
    "position_uuid",
    "school_uuid",
    "stable_company_id",
    "stable_company_id_from_key",
    "stable_education_edge_id_from_key",
    "stable_location_id_from_key",
    "stable_person_id",
    "stable_person_id_from_key",
    "stable_school_id_from_key",
    "stable_summary_id_from_person_id",
    "stable_uuid5",
    "summary_uuid",
]
