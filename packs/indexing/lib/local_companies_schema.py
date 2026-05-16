"""Helpers for local company-directory rows returned by the Powerpacks app."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

COMPANY_COLUMNS = [
    "id",
    "name",
    "description",
    "sector_types",
    "entity_types",
    "stage",
    "headcount",
    "funding_total",
    "city",
    "state",
    "country",
    "logo_url",
    "linkedin_url",
    "people_count",
    "people",
    "people_offset",
    "people_limit",
    "people_has_more",
]
COMPANY_PERSON_COLUMNS = [
    "id",
    "name",
    "public_identifier",
    "position_title",
    "position_description",
    "seniority_band",
    "headline",
    "is_current",
    "start_date",
    "end_date",
    "tenure_years",
    "positions_count",
    "all_positions",
]
REQUIRED_COMPANY_COLUMNS = ["id", "name"]
REQUIRED_COMPANY_PERSON_COLUMNS = ["id", "name"]


def validate_columns(columns: list[str] | tuple[str, ...], required: list[str] | tuple[str, ...]) -> dict[str, Any]:
    seen = set(columns)
    req = set(required)
    return {
        "ok": req.issubset(seen),
        "missing": [c for c in required if c not in seen],
        "extra": [c for c in columns if c not in req],
    }


def validate_company_columns(columns):
    return validate_columns(columns, REQUIRED_COMPANY_COLUMNS)


def validate_company_person_columns(columns):
    return validate_columns(columns, REQUIRED_COMPANY_PERSON_COLUMNS)


def nullable_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.lower() in {"none", "null", "nan"} else text


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 9007199254740991 else str(value)
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() and abs(int(value)) <= 9007199254740991 else str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(value).hex()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, Iterable):
        return [json_safe(v) for v in value]
    return str(value)


def parse_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, (tuple, set)):
        return [json_safe(v) for v in value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                return parse_list(json.loads(text))
            except Exception:
                pass
        return [p.strip() for p in text.split(",") if p.strip()]
    return [json_safe(value)]


def coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y"}


def normalize_company_person_row(row: dict[str, Any]) -> dict[str, Any]:
    name = nullable_string(row.get("name") or row.get("full_name") or row.get("display_name"))
    pid = nullable_string(
        row.get("id") or row.get("person_id") or row.get("base_id") or row.get("public_identifier") or name
    )
    return json_safe(
        {
            "id": pid,
            "name": name,
            "public_identifier": nullable_string(row.get("public_identifier") or row.get("linkedin_slug")),
            "position_title": nullable_string(row.get("position_title") or row.get("title")),
            "position_description": nullable_string(row.get("position_description") or row.get("description")),
            "seniority_band": nullable_string(row.get("seniority_band")),
            "headline": nullable_string(row.get("headline") or row.get("summary")),
            "is_current": coerce_bool(row.get("is_current")),
            "start_date": nullable_string(row.get("start_date") or row.get("starts_at")),
            "end_date": nullable_string(row.get("end_date") or row.get("ends_at")),
            "tenure_years": coerce_float(row.get("tenure_years")),
            "positions_count": coerce_int(row.get("positions_count")) or 1,
            "all_positions": parse_list(row.get("all_positions")),
        }
    )


def normalize_company_row(row: dict[str, Any]) -> dict[str, Any]:
    name = nullable_string(row.get("name") or row.get("company_name") or row.get("display_name"))
    cid = nullable_string(row.get("id") or row.get("company_id") or row.get("canonical_company_id") or name)
    out = {
        "id": cid,
        "name": name,
        "description": nullable_string(row.get("description") or row.get("summary")),
        "sector_types": parse_list(row.get("sector_types") or row.get("sectors") or row.get("entity_sector_text")),
        "entity_types": parse_list(row.get("entity_types") or row.get("entity_type")),
        "stage": nullable_string(row.get("stage")),
        "headcount": coerce_int(row.get("headcount") or row.get("employee_count")),
        "funding_total": coerce_float(row.get("funding_total") or row.get("total_funding")),
        "city": nullable_string(row.get("city")),
        "state": nullable_string(row.get("state")),
        "country": nullable_string(row.get("country")),
        "logo_url": nullable_string(row.get("logo_url") or row.get("company_logo_url")),
        "linkedin_url": nullable_string(row.get("linkedin_url") or row.get("company_linkedin_url")),
        "people_count": coerce_int(row.get("people_count") or row.get("person_count")) or 0,
    }
    if "people" in row:
        out["people"] = [normalize_company_person_row(p) for p in parse_list(row.get("people")) if isinstance(p, dict)]
    for key in ("people_offset", "people_limit"):
        if key in row:
            out[key] = coerce_int(row.get(key)) or 0
    if "people_has_more" in row:
        out["people_has_more"] = coerce_bool(row.get("people_has_more"))
    return json_safe(out)
