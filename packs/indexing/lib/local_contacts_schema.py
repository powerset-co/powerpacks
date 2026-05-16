"""Schema and normalization helpers for local Powerpacks contacts."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

LINKEDIN_CANDIDATE_COLUMNS = [
    "id",
    "operator_id",
    "gmail_token_id",
    "primary_email",
    "display_name",
    "first_name",
    "last_name",
    "all_emails",
    "domain",
    "total_messages",
    "pass1_status",
    "pass1_linkedin_url",
    "potential_linkedins",
    "candidate_count",
    "sources_searched",
    "llm_selected_linkedin",
    "llm_confidence",
    "llm_reasoning",
    "confirmed_linkedin_url",
    "confirmation_source",
    "verification_notes",
    "created_at",
    "updated_at",
]

LOCAL_CONTACT_COLUMNS = [
    "id",
    "operator_id",
    "gmail_token_id",
    "primary_email",
    "display_name",
    "first_name",
    "last_name",
    "all_emails",
    "domain",
    "headline",
    "location_raw",
    "linkedin_url",
    "x_url",
    "phone_numbers",
    "total_messages",
    "total_interactions",
    "pass1_status",
    "pass1_linkedin_url",
    "potential_linkedins",
    "candidate_count",
    "sources_searched",
    "llm_selected_linkedin",
    "llm_confidence",
    "llm_reasoning",
    "confirmed_linkedin_url",
    "confirmation_source",
    "verification_notes",
    "created_at",
    "updated_at",
]

TRUE_VALUES = {"1", "true", "t", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "f", "no", "n", "off"}


def validate_columns(columns: Iterable[str], required: Iterable[str]) -> dict[str, list[str] | bool]:
    actual = list(columns)
    required_list = list(required)
    missing = [col for col in required_list if col not in actual]
    extra = [col for col in actual if col not in required_list]
    return {"ok": not missing, "missing": missing, "extra": extra}


def validate_linkedin_candidate_columns(columns: Iterable[str]) -> dict[str, list[str] | bool]:
    return validate_columns(columns, LINKEDIN_CANDIDATE_COLUMNS)


def parse_jsonish(value: Any) -> Any:
    if value is None or isinstance(value, (list, dict, bool, int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text[0:1] in {"[", "{"}:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


def normalize_email(value: Any) -> str | None:
    if value is None:
        return None
    email = str(value).strip().lower()
    return email or None


def parse_emails(value: Any) -> list[str]:
    parsed = parse_jsonish(value)
    if parsed is None:
        return []
    if isinstance(parsed, str):
        raw = parsed.replace(";", ",").split(",")
    elif isinstance(parsed, Iterable):
        raw = list(parsed)
    else:
        raw = [parsed]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        email = normalize_email(item)
        if email and email not in seen:
            out.append(email)
            seen.add(email)
    return out


def nullable_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return str(value)


def to_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def to_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


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


def normalize_contact_row(row: dict[str, Any]) -> dict[str, Any]:
    emails = parse_emails(row.get("all_emails"))
    primary = normalize_email(row.get("primary_email")) or (emails[0] if emails else None)
    if primary and primary not in emails:
        emails.insert(0, primary)
    linkedin = nullable_str(
        row.get("linkedin_url")
        or row.get("confirmed_linkedin_url")
        or row.get("llm_selected_linkedin")
        or row.get("pass1_linkedin_url")
    )
    total = to_int(row.get("total_interactions", row.get("total_messages")))
    normalized = {
        "id": nullable_str(row.get("id")) or primary or nullable_str(row.get("display_name")) or "unknown",
        "operator_id": nullable_str(row.get("operator_id")),
        "gmail_token_id": nullable_str(row.get("gmail_token_id")),
        "primary_email": primary,
        "display_name": nullable_str(row.get("display_name"))
        or " ".join(filter(None, [nullable_str(row.get("first_name")), nullable_str(row.get("last_name"))]))
        or primary,
        "first_name": nullable_str(row.get("first_name")),
        "last_name": nullable_str(row.get("last_name")),
        "all_emails": emails,
        "domain": nullable_str(row.get("domain")) or (primary.split("@", 1)[1] if primary and "@" in primary else None),
        "headline": nullable_str(row.get("headline")),
        "location_raw": nullable_str(row.get("location_raw") or row.get("location")),
        "linkedin_url": linkedin,
        "x_url": nullable_str(row.get("x_url") or row.get("twitter_url")),
        "phone_numbers": parse_jsonish(row.get("phone_numbers")) or [],
        "total_messages": to_int(row.get("total_messages", total)),
        "total_interactions": total,
        "candidate_count": to_int(row.get("candidate_count")),
    }
    for col in LOCAL_CONTACT_COLUMNS:
        if col not in normalized:
            normalized[col] = json_safe(parse_jsonish(row.get(col)))
    if not isinstance(normalized["phone_numbers"], list):
        normalized["phone_numbers"] = [str(normalized["phone_numbers"])] if normalized["phone_numbers"] else []
    return json_safe(normalized)


def normalize_linkedin_candidate_row(row: dict[str, Any]) -> dict[str, Any]:
    return normalize_contact_row(row)
