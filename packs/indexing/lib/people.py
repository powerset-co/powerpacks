"""Local people transforms for the indexing pipeline.

The functions in this module are intentionally local and deterministic: they
read already-ingested Powerpacks people rows and derive the position-level
people namespace records, Postgres person contract records, and hydrated profile
artifacts without LLM or network calls.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from packs.indexing.lib.io import write_csv as _write_csv, write_jsonl as _write_jsonl

try:  # pragma: no cover - exercised by direct script execution paths
    from .identity import canonical_person_key, position_uuid, stable_company_id, stable_person_id_from_key
except ImportError:  # pragma: no cover
    from identity import canonical_person_key, position_uuid, stable_company_id, stable_person_id_from_key  # type: ignore

try:
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS as PEOPLE_CSV_COLUMNS,
        extract_public_identifier,
        normalize_linkedin_url,
        normalize_people_row,
        parse_jsonish,
    )
except Exception:  # pragma: no cover - fallback for copied primitive bundles
    PEOPLE_CSV_COLUMNS = [
        "id", "public_identifier", "linkedin_url", "first_name", "last_name", "full_name",
        "headline", "summary", "city", "state", "country", "location_raw", "profile_picture_url",
        "work_experiences", "education", "current_title", "current_company", "current_company_urn",
        "entity_urn", "enrichment_provider", "enriched_at", "harmonic_response", "harmonic_location",
        "rapidapi_response", "twitter_handle", "twitter_response", "primary_email", "all_emails",
        "primary_phone", "all_phones", "source_channels", "source_artifacts",
    ]

    def extract_public_identifier(linkedin_url: str) -> str:
        match = re.search(r"linkedin\.com/in/([^/?#]+)", linkedin_url or "", re.IGNORECASE)
        return match.group(1).strip().rstrip("/").lower() if match else ""

    def normalize_linkedin_url(value: str) -> str:
        url = (value or "").strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
        if url.startswith("linkedin.com/"):
            url = "https://www." + url
        if url.startswith("www.linkedin.com/"):
            url = "https://" + url
        public_id = extract_public_identifier(url)
        return f"https://www.linkedin.com/in/{public_id}" if public_id else url

    def normalize_people_row(row: dict[str, Any]) -> dict[str, str]:
        out = {column: "" for column in PEOPLE_CSV_COLUMNS}
        for column in PEOPLE_CSV_COLUMNS:
            value = row.get(column, "")
            out[column] = "" if value is None else (json.dumps(value) if isinstance(value, (dict, list)) else str(value))
        out["linkedin_url"] = normalize_linkedin_url(out.get("linkedin_url", ""))
        out["public_identifier"] = out.get("public_identifier") or extract_public_identifier(out.get("linkedin_url", ""))
        return out

    def parse_jsonish(value: Any, default: Any) -> Any:
        if value in (None, ""):
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(str(value))
        except Exception:
            return default


CONTRACT_PERSON_COLUMNS = [
    "id", "public_identifier", "public_profile_url", "provider_entity_urn", "full_name", "headline",
    "summary", "profile_picture_url", "location_raw", "city", "state", "country", "hydrated_context",
    "x_twitter_handle", "x_twitter_followers", "linkedin_followers", "linkedin_connections", "ig_handle",
    "ig_followers", "inferred_birth_year",
]

PEOPLE_NAMESPACE_COLUMNS = [
    "id", "base_id", "position_title", "word_tokens", "char_tokens", "d2q_tokens", "phrase_tokens", "city",
    "state", "country", "macro_region", "metro_areas", "seniority_band", "company_id", "is_current",
    "total_years_experience", "start_date_epoch", "end_date_epoch", "role_track", "allowed_operator_ids",
    "role_ids", "inferred_birth_year", "x_twitter_followers", "linkedin_followers", "linkedin_connections",
    "ig_followers",
]

_ZERO_EPOCH = 0
_DATE_RE = re.compile(r"^(\d{4})(?:[-/](\d{1,2}))?(?:[-/](\d{1,2}))?")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _string(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _first(row: dict[str, Any], fields: Iterable[str]) -> str:
    for field in fields:
        value = _string(row.get(field))
        if value:
            return value
    return ""


def _int_or_zero(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


def _json_list(value: Any) -> list[Any]:
    parsed = parse_jsonish(value, [])
    return parsed if isinstance(parsed, list) else []


def _json_object(value: Any) -> dict[str, Any]:
    parsed = parse_jsonish(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _date_parts(value: Any) -> tuple[int, int, int] | None:
    if isinstance(value, dict):
        year = value.get("year") or value.get("start_year") or value.get("end_year")
        if not year:
            return None
        try:
            return int(year), int(value.get("month") or 1), int(value.get("day") or 1)
        except (TypeError, ValueError):
            return None
    text = _string(value)
    if not text or text.lower() in {"present", "current", "now"}:
        return None
    match = _DATE_RE.match(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2) or 1), int(match.group(3) or 1)


def _date_text(value: Any) -> str | None:
    parts = _date_parts(value)
    if not parts:
        return None
    year, month, day = parts
    if month == 1 and day == 1:
        return f"{year:04d}"
    if day == 1:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}-{month:02d}-{day:02d}"


def epoch_seconds(value: Any, *, current_as_zero: bool = False) -> int:
    """Return UTC epoch seconds for date-like values.

    Current/open-ended end dates are encoded as 0 to match the checked-in
    TurboPuffer tenure-overlap contract.
    """

    if current_as_zero and (value in (None, "") or _string(value).lower() in {"present", "current", "now"}):
        return _ZERO_EPOCH
    parts = _date_parts(value)
    if not parts:
        return _ZERO_EPOCH
    year, month, day = parts
    try:
        return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())
    except ValueError:
        return _ZERO_EPOCH




def parse_date(value: Any) -> datetime | None:
    parts = _date_parts(value)
    if not parts:
        return None
    try:
        return datetime(parts[0], parts[1], parts[2], tzinfo=timezone.utc)
    except ValueError:
        return None


def date_to_epoch_seconds(value: Any) -> int | None:
    parsed = parse_date(value)
    return int(parsed.timestamp()) if parsed else None


def calculate_duration_years(start: Any, end: Any, is_current: bool = False) -> float | None:
    start_dt = parse_date(start)
    if not start_dt:
        return None
    end_dt = datetime.now(timezone.utc) if is_current else parse_date(end)
    if not end_dt or end_dt < start_dt:
        return None
    return round((end_dt - start_dt).days / 365.25, 1)


def iter_positions(person_row: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in _json_list(person_row.get("work_experiences")) if isinstance(item, dict)]


def iter_education(person_row: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in _json_list(person_row.get("education")) if isinstance(item, dict)]


def local_operator_ids(person_row: dict[str, Any], default_operator_id: str | None = None) -> list[str]:
    parsed = parse_jsonish(person_row.get("allowed_operator_ids") or person_row.get("operator_ids"), None)
    if isinstance(parsed, list):
        ids = [_string(v) for v in parsed if _string(v)]
    elif parsed:
        ids = [part.strip() for part in str(parsed).split(",") if part.strip()]
    else:
        ids = []
    if not ids:
        ids = [default_operator_id or "local:user"]
    return list(dict.fromkeys(ids))

def _exp_start(exp: dict[str, Any]) -> Any:
    return exp.get("starts_at") or exp.get("start_date") or exp.get("startDate") or exp.get("start") or exp.get("from") or exp.get("start_year")


def _exp_end(exp: dict[str, Any]) -> Any:
    return exp.get("ends_at") or exp.get("end_date") or exp.get("endDate") or exp.get("end") or exp.get("to") or exp.get("end_year")


def _is_current(exp: dict[str, Any]) -> bool:
    for field in ("is_current_position", "is_current", "current"):
        value = exp.get(field)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "1", "yes", "y"}:
            return True
        if isinstance(value, str) and value.strip().lower() in {"false", "0", "no", "n"}:
            return False
    return _exp_end(exp) in (None, "") or _string(_exp_end(exp)).lower() in {"present", "current", "now"}


def _title(exp: dict[str, Any]) -> str:
    return _first(exp, ("title", "position_title", "position", "role"))


def _company_name(exp: dict[str, Any]) -> str:
    value = _first(exp, ("company_name", "company", "organization", "employer"))
    if value and not value.startswith("{"):
        return value
    for key in ("company", "organization", "employer"):
        nested = exp.get(key)
        if isinstance(nested, dict):
            nested_name = _first(nested, ("name", "company_name", "companyName"))
            if nested_name:
                return nested_name
    return ""


def _company_key(exp: dict[str, Any]) -> str:
    for field in ("company_key", "rapidapi_company_id", "company_id", "company_urn", "company_public_identifier", "company_linkedin_url"):
        value = _string(exp.get(field))
        if value:
            if field == "rapidapi_company_id":
                return f"rapidapi:{value}"
            if field == "company_public_identifier":
                return f"linkedin_company:{value.lower()}"
            return value
    name = _company_name(exp)
    return f"name:{name.lower()}" if name else ""


def _role_track(title: str) -> str:
    t = title.lower()
    if any(word in t for word in ["engineer", "developer", "architect", "cto", "technology", "technical", "scientist", "data", "ai", "ml"]):
        return "engineering"
    if "product" in t or t in {"cpo"}:
        return "product"
    if any(word in t for word in ["sales", "revenue", "account executive", "cro", "business development"]):
        return "sales"
    if any(word in t for word in ["marketing", "growth", "demand gen", "brand", "cmo"]):
        return "marketing"
    if any(word in t for word in ["finance", "cfo", "accounting", "controller"]):
        return "finance"
    if any(word in t for word in ["operations", "operator", "coo", "chief of staff", "general manager"]):
        return "operations"
    if any(word in t for word in ["investor", "partner", "venture", "principal"]):
        return "investing"
    if any(word in t for word in ["founder", "co-founder", "cofounder", "ceo", "chief executive"]):
        return "founder"
    return ""


def _seniority_band(title: str) -> str:
    t = title.lower()
    if re.search(r"\b(co-?founder|cofounder|founder|owner)\b", t):
        return "owner"
    if re.search(r"\b(ceo|cto|cfo|coo|cpo|cro|cmo|chief|president)\b", t):
        return "c_suite"
    if re.search(r"\b(svp|evp|vp|vice president)\b", t):
        return "vice_president"
    if "director" in t or "head of" in t:
        return "director"
    if "manager" in t or "lead" in t:
        return "manager"
    if "senior" in t or "staff" in t or "principal" in t:
        return "senior_ic"
    if t:
        return "ic"
    return ""


def _role_ids(title: str) -> list[str]:
    t = title.lower()
    role_ids: list[str] = []
    shortcuts = [
        ("founder", r"\b(co-?founder|cofounder|founder|founding)\b"),
        ("chief_executive_officer", r"\b(ceo|chief executive officer)\b"),
        ("chief_technology_officer", r"\b(cto|chief technology officer)\b"),
        ("chief_financial_officer", r"\b(cfo|chief financial officer)\b"),
        ("chief_operating_officer", r"\b(coo|chief operating officer)\b"),
        ("chief_product_officer", r"\b(cpo|chief product officer)\b"),
        ("chief_revenue_officer", r"\b(cro|chief revenue officer)\b"),
        ("chief_marketing_officer", r"\b(cmo|chief marketing officer)\b"),
    ]
    for role_id, pattern in shortcuts:
        if re.search(pattern, t):
            role_ids.append(role_id)
    track = _role_track(title)
    if track and track not in {"founder", "investing"}:
        role_ids.append(track)
    return list(dict.fromkeys(role_ids))


def _tokens(*values: Any) -> list[str]:
    return list(dict.fromkeys(token for value in values for token in _TOKEN_RE.findall(_string(value).lower())))


def _ngrams(text: str, size: int) -> list[str]:
    compact = re.sub(r"\s+", " ", text.lower()).strip()
    if len(compact) < size:
        return [compact] if compact else []
    return list(dict.fromkeys(compact[idx : idx + size] for idx in range(0, len(compact) - size + 1)))


def _position_to_profile(exp: dict[str, Any], index: int) -> dict[str, Any]:
    title = _title(exp)
    start = _exp_start(exp)
    end = _exp_end(exp)
    company_name = _company_name(exp)
    company_id = stable_company_id(_company_key(exp)) if _company_key(exp) else ""
    profile = {
        "id": _string(exp.get("id") or exp.get("position_id") or exp.get("urn")) or f"position-{index}",
        "position_title": title or None,
        "title": title,
        "description": _first(exp, ("description", "summary")) or None,
        "dense_text": " ".join(part for part in [title, company_name, _first(exp, ("description", "summary"))] if part) or None,
        "seniority_band": _seniority_band(title) or None,
        "role_track": _role_track(title) or None,
        "company_name": company_name or None,
        "company_id": company_id or None,
        "company_domain": _first(exp, ("company_domain", "domain", "website_domain")) or None,
        "company_linkedin_url": _first(exp, ("company_linkedin_url", "linkedin_url", "company_url")) or None,
        "company_description": _first(exp, ("company_description",)) or None,
        "company_sector_types": exp.get("company_sector_types") or [],
        "company_entity_types": exp.get("company_entity_types") or [],
        "company_headcount": exp.get("company_headcount") or None,
        "company_funding_total": exp.get("company_funding_total") or None,
        "company_stage": exp.get("company_stage") or None,
        "investor_names": exp.get("investor_names") or [],
        "start_date": _date_text(start),
        "end_date": None if _is_current(exp) else _date_text(end),
        "is_current": _is_current(exp),
    }
    return profile


def _education_to_profile(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    starts_at = item.get("starts_at") if isinstance(item.get("starts_at"), dict) else {}
    ends_at = item.get("ends_at") if isinstance(item.get("ends_at"), dict) else {}
    return {
        "school_name": _first(item, ("school_name", "school", "name")) or None,
        "degree": _first(item, ("degree", "degree_name")) or None,
        "field_of_study": _first(item, ("field_of_study", "field", "major")) or None,
        "start_year": item.get("start_year") or starts_at.get("year"),
        "end_year": item.get("end_year") or ends_at.get("year"),
    }


def flatten_people(rows: Iterable[dict[str, Any]] | str | Path) -> list[dict[str, Any]]:
    """Normalize Powerpacks people.csv rows into deterministic local profiles."""

    if isinstance(rows, (str, Path)):
        with Path(rows).open(newline="", encoding="utf-8") as handle:
            raw_rows = list(csv.DictReader(handle))
    else:
        raw_rows = list(rows)

    flattened: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_rows:
        row = normalize_people_row(raw)
        public_id = row.get("public_identifier") or extract_public_identifier(row.get("linkedin_url", ""))
        linkedin_url = normalize_linkedin_url(row.get("linkedin_url", ""))
        full_name = row.get("full_name") or " ".join(part for part in [row.get("first_name"), row.get("last_name")] if part).strip()
        person_id = stable_person_id_from_key(canonical_person_key(row))
        if person_id in seen:
            continue
        seen.add(person_id)
        work_experiences = [item for item in _json_list(row.get("work_experiences")) if isinstance(item, dict)]
        education = [item for item in _json_list(row.get("education")) if isinstance(item, dict)]
        flattened.append({
            "id": person_id,
            "person_id": person_id,
            "public_identifier": public_id,
            "linkedin_url": linkedin_url,
            "public_profile_url": linkedin_url,
            "provider_entity_urn": row.get("entity_urn", ""),
            "first_name": row.get("first_name", ""),
            "last_name": row.get("last_name", ""),
            "full_name": full_name,
            "headline": row.get("headline", ""),
            "summary": row.get("summary", ""),
            "city": row.get("city", ""),
            "state": row.get("state", ""),
            "country": row.get("country", ""),
            "location_raw": row.get("location_raw", ""),
            "profile_picture_url": row.get("profile_picture_url", ""),
            "work_experiences": work_experiences,
            "education": education,
            "current_title": row.get("current_title", ""),
            "current_company": row.get("current_company", ""),
            "twitter_handle": row.get("twitter_handle", ""),
            "x_twitter_handle": row.get("twitter_handle", ""),
            "source_channels": row.get("source_channels", ""),
            "source_artifacts": row.get("source_artifacts", ""),
            "allowed_operator_ids": local_operator_ids(raw, None) if (raw.get("allowed_operator_ids") or raw.get("operator_ids")) else [],
            "raw": {**{key: row.get(key, "") for key in PEOPLE_CSV_COLUMNS}, "allowed_operator_ids": raw.get("allowed_operator_ids", ""), "operator_ids": raw.get("operator_ids", "")},
            "rapidapi_response": _json_object(row.get("rapidapi_response")),
            "twitter_response": _json_object(row.get("twitter_response")),
        })
    return flattened


def _years_of_experience(experiences: list[dict[str, Any]]) -> float:
    intervals: list[tuple[date, date]] = []
    today = date.today()
    for exp in experiences:
        parts = _date_parts(_exp_start(exp))
        if not parts:
            continue
        start = date(parts[0], parts[1], parts[2])
        end_parts = None if _is_current(exp) else _date_parts(_exp_end(exp))
        end = date(end_parts[0], end_parts[1], end_parts[2]) if end_parts else today
        if end >= start:
            intervals.append((start, min(end, today)))
    if not intervals:
        return 0.0
    intervals.sort()
    merged: list[tuple[date, date]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        elif end > merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
    return round(sum((end - start).days for start, end in merged) / 365.25, 1)


def build_roles(people: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build position-level records for the TurboPuffer people namespace."""

    records: list[dict[str, Any]] = []
    for person in people:
        experiences = person.get("work_experiences") or []
        total_years = _years_of_experience([exp for exp in experiences if isinstance(exp, dict)])
        for idx, exp in enumerate(exp for exp in experiences if isinstance(exp, dict)):
            title = _title(exp)
            company_key = _company_key(exp)
            company_id = stable_company_id(company_key) if company_key else ""
            word_tokens = _tokens(title, _company_name(exp), exp.get("description"), person.get("headline"))
            row = {
                "id": position_uuid(person["id"], _string(exp.get("id") or exp.get("position_id") or exp.get("urn")) or idx),
                "base_id": person["id"],
                "position_title": title,
                "word_tokens": word_tokens,
                "char_tokens": _ngrams(title, 3),
                "d2q_tokens": word_tokens,
                "phrase_tokens": [phrase for phrase in [title.lower().strip(), _company_name(exp).lower().strip()] if phrase],
                "city": person.get("city", ""),
                "state": person.get("state", ""),
                "country": person.get("country", ""),
                "macro_region": "",
                "metro_areas": [],
                "seniority_band": _seniority_band(title),
                "company_id": company_id,
                "is_current": _is_current(exp),
                "total_years_experience": total_years,
                "start_date_epoch": epoch_seconds(_exp_start(exp)),
                "end_date_epoch": epoch_seconds(_exp_end(exp), current_as_zero=True),
                "role_track": _role_track(title),
                "allowed_operator_ids": [],
                "role_ids": _role_ids(title),
                "inferred_birth_year": _int_or_zero(person.get("inferred_birth_year")),
                "x_twitter_followers": _int_or_zero(person.get("x_twitter_followers")),
                "linkedin_followers": _int_or_zero(person.get("linkedin_followers")),
                "linkedin_connections": _int_or_zero(person.get("linkedin_connections")),
                "ig_followers": _int_or_zero(person.get("ig_followers")),
            }
            records.append(row)
    return records


def _hydrated_context(person: dict[str, Any]) -> dict[str, Any]:
    positions = [_position_to_profile(exp, idx) for idx, exp in enumerate(person.get("work_experiences") or []) if isinstance(exp, dict)]
    education = [edu for edu in (_education_to_profile(item) for item in person.get("education") or []) if edu]
    location = person.get("location_raw") or ", ".join(part for part in [person.get("city"), person.get("state"), person.get("country")] if part) or None
    rapid = person.get("rapidapi_response") or {}
    twitter = person.get("twitter_response") or {}
    return {
        "person_id": person.get("id", ""),
        "name": person.get("full_name", ""),
        "location": location,
        "headline": person.get("headline") or None,
        "summary": person.get("summary") or None,
        "positions": positions,
        "education": education,
        "tech_skills": rapid.get("skills") or [],
        "linkedin_url": person.get("linkedin_url") or None,
        "profile_picture_url": person.get("profile_picture_url") or None,
        "x_twitter_handle": person.get("x_twitter_handle") or None,
        "x_twitter_followers": _int_or_zero(twitter.get("followers") or twitter.get("followers_count")),
        "linkedin_followers": _int_or_zero(rapid.get("follower_count") or rapid.get("followers")),
        "linkedin_connections": _int_or_zero(rapid.get("connection_count") or rapid.get("connections")),
        "instagram_handle": rapid.get("instagram_handle") or None,
        "instagram_followers": _int_or_zero(rapid.get("instagram_followers")),
        "years_of_experience": _years_of_experience([exp for exp in person.get("work_experiences") or [] if isinstance(exp, dict)]),
        "matched_position_indexes": [],
        "trait_scores": {},
        "vertical_sources": [value for value in str(person.get("source_channels") or "").split(",") if value],
    }


def build_people_records(people: Iterable[dict[str, Any]], roles: Iterable[dict[str, Any]] | None = None, default_operator_id: str | None = None) -> list[dict[str, Any]]:
    """Build one TurboPuffer people namespace record per actual position."""

    if roles is None:
        records = build_roles(people)
    else:
        records = list(roles)
    for record in records:
        record["allowed_operator_ids"] = record.get("allowed_operator_ids") or [default_operator_id or "local:user"]
    return records


def build_profile_contract_records(people: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build legacy hydrated person contract records for compatibility."""

    records: list[dict[str, Any]] = []
    for person in people:
        context = _hydrated_context(person)
        record = {
            "id": person.get("id", ""),
            "public_identifier": person.get("public_identifier", ""),
            "public_profile_url": person.get("public_profile_url") or person.get("linkedin_url", ""),
            "provider_entity_urn": person.get("provider_entity_urn", ""),
            "full_name": person.get("full_name", ""),
            "headline": person.get("headline", ""),
            "summary": person.get("summary", ""),
            "profile_picture_url": person.get("profile_picture_url", ""),
            "location_raw": person.get("location_raw", ""),
            "city": person.get("city", ""),
            "state": person.get("state", ""),
            "country": person.get("country", ""),
            "hydrated_context": context,
            "x_twitter_handle": person.get("x_twitter_handle", ""),
            "x_twitter_followers": context.get("x_twitter_followers", 0),
            "linkedin_followers": context.get("linkedin_followers", 0),
            "linkedin_connections": context.get("linkedin_connections", 0),
            "ig_handle": context.get("instagram_handle") or "",
            "ig_followers": context.get("instagram_followers", 0),
            "inferred_birth_year": _int_or_zero(person.get("inferred_birth_year")),
        }
        records.append(record)
    return records


def build_unified_profiles(people: Iterable[dict[str, Any]], roles: Iterable[dict[str, Any]] | None = None, companies: Iterable[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Build hydrated profile artifacts from local person rows."""

    profiles: list[dict[str, Any]] = []
    for person in people:
        context = _hydrated_context(person)
        profiles.append({
            "id": context["person_id"],
            "base_id": context["person_id"],
            "person_id": context["person_id"],
            "name": context["name"],
            "location": context["location"],
            "headline": context["headline"],
            "summary": context["summary"],
            "positions": context["positions"],
            "education": context["education"],
            "tech_skills": context["tech_skills"],
            "linkedin_url": context["linkedin_url"],
            "profile_picture_url": context["profile_picture_url"],
            "linkedin_followers": context["linkedin_followers"],
            "linkedin_connections": context["linkedin_connections"],
            "x_twitter_handle": context["x_twitter_handle"],
            "x_twitter_followers": context["x_twitter_followers"],
            "instagram_handle": context["instagram_handle"],
            "instagram_followers": context["instagram_followers"],
            "inferred_age": None,
            "years_of_experience": context["years_of_experience"],
            "total_interactions": None,
            "base_score": 0.0,
            "matched_position_indexes": [],
            "trait_scores": {},
            "vertical_sources": context["vertical_sources"],
        })
    return profiles


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    _write_jsonl(path, rows)


def write_csv(path: str | Path, rows: Iterable[dict[str, Any]], columns: list[str] | None = None) -> None:
    rows = list(rows)
    if columns is None:
        columns = list(rows[0].keys()) if rows else []
    serialized = [
        {key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value for key, value in row.items()}
        for row in rows
    ]
    _write_csv(path, columns, serialized)
