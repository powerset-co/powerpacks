"""Checked-in Powerpacks data contracts.

These constants are intentionally local and explicit so primitives do not need
to introspect Postgres or TurboPuffer schemas on every run.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any


POSTGRES_TABLES = {
    "persons": {
        "primary_key": "id",
        "required_columns": [
            "id",
            "public_identifier",
            "public_profile_url",
            "provider_entity_urn",
            "full_name",
            "headline",
            "summary",
            "profile_picture_url",
            "location_raw",
            "city",
            "state",
            "country",
            "hydrated_context",
            "x_twitter_handle",
            "x_twitter_followers",
            "linkedin_followers",
            "linkedin_connections",
            "ig_handle",
            "ig_followers",
            "inferred_birth_year",
        ],
    },
    "companies": {
        "primary_key": "id",
        "required_columns": [
            "id",
            "name",
            "harmonic_urn",
            "domain",
            "website_domain",
            "linkedin_url",
            "description",
            "headcount",
            "funding_total",
            "funding_stage",
            "entity_types",
            "sector_types",
            "founded_year",
            "location",
            "li_company_id",
        ],
    },
    "person_source_summary": {
        "required_columns": ["person_id", "operator_id", "total_interactions"],
        "optional": True,
    },
    "sets": {
        "primary_key": "id",
        "required_columns": ["id", "name", "created_by", "is_active", "is_personal"],
    },
    "set_members": {
        "primary_key": "id",
        "required_columns": ["id", "set_id", "user_id", "role", "joined_at"],
    },
    "users": {
        "primary_key": "id",
        "required_columns": ["id", "user_id", "email", "name"],
    },
}


TURBOPUFFER_NAMESPACES = {
    "people": "aleph_people_v1",
    "schools": "aleph_education_v1",
    "education": "aleph_people_education_v1",
    "summaries": "aleph_summaries_v1",
    "companies": "aleph_companies_v1",
    "investors": "aleph_investors_v1",
}


TURBOPUFFER_PEOPLE_ATTRIBUTES = [
    "id",
    "base_id",
    "position_title",
    "description",
    "dense_text",
    "word_tokens",
    "char_tokens",
    "d2q_tokens",
    "phrase_tokens",
    "city",
    "state",
    "country",
    "macro_region",
    "metro_areas",
    "seniority_band",
    "company_id",
    "company_domain",
    "company_linkedin_url",
    "company_description",
    "company_sector_types",
    "company_entity_types",
    "company_headcount",
    "company_funding_total",
    "company_stage",
    "investor_names",
    "is_current",
    "total_years_experience",
    "start_date_epoch",
    "end_date_epoch",
    "role_track",
    "allowed_operator_ids",
    "role_ids",
    "inferred_birth_year",
    "x_twitter_followers",
    "linkedin_followers",
    "linkedin_connections",
    "ig_followers",
]


TURBOPUFFER_FILTER_OPERATORS = {
    "id": ["In", "Eq", "Gt"],
    "city": ["In"],
    "state": ["In"],
    "country": ["In"],
    "macro_region": ["In"],
    "metro_areas": ["ContainsAny"],
    "seniority_band": ["In", "NotIn"],
    "company_id": ["In"],
    "is_current": ["Eq"],
    "total_years_experience": ["Gte", "Lte"],
    "start_date_epoch": ["Lte"],
    "end_date_epoch": ["Gte", "Eq"],
    "role_track": ["In"],
    "allowed_operator_ids": ["ContainsAny"],
    "role_ids": ["ContainsAny"],
    "base_id": ["In"],
    "inferred_birth_year": ["Gte", "Lte"],
    "x_twitter_followers": ["Gte", "Lte"],
    "linkedin_followers": ["Gte", "Lte"],
    "linkedin_connections": ["Gte", "Lte"],
    "ig_followers": ["Gte", "Lte"],
    # Education prefilter namespace.
    "person_id": ["In"],
    "canonical_education_id": ["In"],
    "school_name": ["ContainsAllTokens", "Eq"],
    "degree_normalized": ["In"],
    "field_of_study": ["ContainsAllTokens"],
    "graduation_year": ["Gte", "Lte"],
    # Summary prefilter namespace.
    "tech_skills": ["ContainsAny"],
    # Company namespace.
    "company_name": ["Eq", "In"],
    "funding_stage": ["Eq", "Gt", "Gte", "Lte", "In"],
    "funding_total": ["Gt", "Gte", "Lte"],
    "headcount": ["Gt", "Gte", "Lte"],
    "founded_year": ["Gt", "Gte", "Lte"],
    "last_funding_at": ["Gte", "Lte"],
    "valuation": ["Gt", "Gte", "Lte"],
    "entity_types": ["ContainsAny"],
    "sector_types": ["ContainsAny"],
    "technology_types": ["ContainsAny"],
    "customer_type": ["ContainsAny"],
    "investor_urns": ["ContainsAny"],
    "accelerators": ["ContainsAny"],
    "yc_batches": ["ContainsAny"],
    "stage": ["Eq", "In"],
    "company_city": ["In"],
    "company_state": ["In"],
    "company_country": ["In"],
    "company_metro_area": ["In"],
    "company_macro_region": ["In"],
    "metro_area": ["In"],
    # Investor resolver namespace.
    "investor_name": ["Eq"],
    "investor_name_tokens": ["ContainsAllTokens"],
    "investor_type": ["Eq", "In"],
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def contracts_dir() -> Path:
    return repo_root() / "contracts"


def contract_path(relative_path: str) -> Path:
    path = contracts_dir() / relative_path
    if not path.exists():
        raise FileNotFoundError(f"missing Powerpacks contract: {relative_path}")
    return path


def load_contract(relative_path: str) -> dict[str, Any]:
    return json.loads(contract_path(relative_path).read_text())


def postgres_table_contract(table_name: str) -> dict[str, Any]:
    return load_contract(f"postgres/{table_name}.table.json")


def postgres_required_columns(table_name: str) -> list[str]:
    contract = postgres_table_contract(table_name)
    return [str(column["name"]) for column in contract.get("columns", []) if column.get("required")]


def assert_columns_in_contract(table_name: str, columns: list[str]) -> None:
    allowed = set(postgres_required_columns(table_name))
    missing = [column for column in columns if column not in allowed]
    if missing:
        raise RuntimeError(f"{table_name} columns are not declared in Powerpacks contract: {missing}")


def validate_hydrated_profile(profile: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in ["person_id", "name", "positions", "education"]:
        if field not in profile:
            errors.append(f"missing required hydrated profile field: {field}")
    if "positions" in profile and not isinstance(profile.get("positions"), list):
        errors.append("hydrated profile positions must be a list")
    if "education" in profile and not isinstance(profile.get("education"), list):
        errors.append("hydrated profile education must be a list")
    if profile.get("person_id") is not None and not isinstance(profile.get("person_id"), str):
        errors.append("hydrated profile person_id must be a string")
    return errors


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date(int(text[:4]), 1, 1)
    except (ValueError, TypeError):
        return None


def compute_years_of_experience(positions: list[dict[str, Any]]) -> float | None:
    intervals: list[tuple[date, date]] = []
    today = date.today()
    for pos in positions:
        start = parse_date(pos.get("start_date"))
        if not start:
            continue
        end = parse_date(pos.get("end_date")) or today
        if end < start:
            continue
        intervals.append((start, min(end, today)))
    if not intervals:
        return None

    intervals.sort()
    merged: list[tuple[date, date]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        elif end > merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
    days = sum((end - start).days for start, end in merged)
    return round(days / 365.25, 1)


def normalize_hydrated_context(row: dict[str, Any]) -> dict[str, Any]:
    context = row.get("hydrated_context") or {}
    if not isinstance(context, dict):
        context = {}

    positions = context.get("positions") or []
    if not isinstance(positions, list):
        positions = []
    education = context.get("education") or []
    if not isinstance(education, list):
        education = []

    inferred_birth_year = row.get("inferred_birth_year") or context.get("inferred_birth_year")
    inferred_age = None
    if inferred_birth_year:
        try:
            inferred_age = date.today().year - int(inferred_birth_year)
        except (TypeError, ValueError):
            inferred_age = None

    city_state_country = ", ".join(str(row.get(k)) for k in ["city", "state", "country"] if row.get(k))
    location = context.get("location") or row.get("location_raw") or city_state_country or None

    return {
        "person_id": str(row.get("id") or context.get("person_id") or ""),
        "name": context.get("name") or row.get("full_name") or "",
        "location": location,
        "headline": context.get("headline") or row.get("headline"),
        "summary": row.get("summary") or context.get("summary"),
        "positions": positions,
        "education": education,
        "tech_skills": context.get("tech_skills") or [],
        "linkedin_url": context.get("linkedin_url") or row.get("public_profile_url"),
        "profile_picture_url": context.get("profile_picture_url") or row.get("profile_picture_url"),
        "linkedin_followers": row.get("linkedin_followers"),
        "linkedin_connections": row.get("linkedin_connections"),
        "x_twitter_handle": row.get("x_twitter_handle"),
        "x_twitter_followers": row.get("x_twitter_followers"),
        "instagram_handle": row.get("ig_handle"),
        "instagram_followers": row.get("ig_followers"),
        "inferred_age": inferred_age,
        "years_of_experience": context.get("years_of_experience") or compute_years_of_experience(positions),
        "total_interactions": row.get("total_interactions"),
        "base_score": context.get("base_score", 0.0),
        "matched_position_indexes": context.get("matched_position_indexes") or [],
        "trait_scores": context.get("trait_scores") or {},
        "vertical_sources": context.get("vertical_sources") or [],
    }
