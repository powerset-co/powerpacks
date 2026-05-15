"""Company identity helpers for RapidAPI LinkedIn profile enrichment.

These helpers intentionally keep RapidAPI/company-provider IDs separate from
LinkedIn company slugs and URLs. Harmonic URNs are treated as foreign legacy
identifiers and are never promoted into RapidAPI company IDs or company keys.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

_COMPANY_KEYS = ("company", "organization", "employer")
_RAPIDAPI_ID_FIELDS = ("company_id", "companyId", "company_urn", "companyUrn")
_LOOKUP_RAPIDAPI_ID_FIELDS = ("rapidapi_company_id", "company_id", "companyId")
_LINKEDIN_URL_FIELDS = ("linkedin_url", "company_linkedin_url", "company_linkedin_profile_url")
_SLUG_FIELDS = ("company_public_identifier", "public_identifier", "linkedin_slug")
_NAME_FIELDS = ("company_name", "name")


def _clean_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_harmonic_urn(value: Any) -> bool:
    return _clean_string(value).lower().startswith("urn:harmonic:")


def _first_string(mapping: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = _clean_string(mapping.get(field))
        if value:
            return value
    return ""


def _nested_company_objects(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    companies: list[dict[str, Any]] = []
    for key in _COMPANY_KEYS:
        value = mapping.get(key)
        if isinstance(value, dict):
            companies.append(value)
    return companies


def normalize_company_linkedin_url(value: str) -> str:
    """Return canonical LinkedIn company URL or an empty string.

    Only LinkedIn ``/company/{slug}`` URLs are accepted. The returned slug is
    URL-decoded and lower-cased so it is safe for deterministic keys.
    """

    slug = extract_company_public_identifier(value)
    return f"https://www.linkedin.com/company/{slug}" if slug else ""


def extract_company_public_identifier(value: str) -> str:
    """Extract the lower-case LinkedIn company slug from a LinkedIn company URL."""

    raw = _clean_string(value)
    if not raw:
        return ""
    if raw.startswith("linkedin.com/"):
        raw = "https://www." + raw
    elif raw.startswith("www.linkedin.com/"):
        raw = "https://" + raw
    match = re.search(r"(?:^|://)(?:[^/]*\.)?linkedin\.com/company/([^/?#]+)", raw, re.IGNORECASE)
    if not match:
        return ""
    slug = urllib.parse.unquote(match.group(1)).strip().strip("/").lower()
    return slug


def extract_rapidapi_company_id(exp_or_company: dict[str, Any]) -> str:
    """Extract a non-Harmonic opaque RapidAPI/company-provider company ID.

    Top-level RapidAPI fields and IDs inside nested company objects are
    supported. Values beginning with ``urn:harmonic:`` are ignored.
    """

    if not isinstance(exp_or_company, dict):
        return ""
    candidates: list[Any] = [exp_or_company.get(field) for field in _RAPIDAPI_ID_FIELDS]
    for company in _nested_company_objects(exp_or_company):
        candidates.extend(company.get(field) for field in (*_RAPIDAPI_ID_FIELDS, "id"))
    for value in candidates:
        text = _clean_string(value)
        if text and not _is_harmonic_urn(text):
            return text
    return ""


def _metadata_from_row(row: dict[str, Any]) -> dict[str, str]:
    rapidapi_id = ""
    for field in _LOOKUP_RAPIDAPI_ID_FIELDS:
        value = _clean_string(row.get(field))
        if value and not _is_harmonic_urn(value):
            rapidapi_id = value
            break

    url = _first_string(row, _LINKEDIN_URL_FIELDS)
    slug = _first_string(row, _SLUG_FIELDS).lower()
    if url:
        slug = slug or extract_company_public_identifier(url)
    company_url = normalize_company_linkedin_url(url) if url else (f"https://www.linkedin.com/company/{slug}" if slug else "")
    company_name = _first_string(row, _NAME_FIELDS)

    metadata: dict[str, str] = {}
    if rapidapi_id:
        metadata["rapidapi_company_id"] = rapidapi_id
    if slug:
        metadata["company_public_identifier"] = slug
    if company_url:
        metadata["company_linkedin_url"] = company_url
    if company_name:
        metadata["company_name"] = company_name
    if rapidapi_id:
        metadata["company_key"] = f"rapidapi:{rapidapi_id}"
    elif slug:
        metadata["company_key"] = f"linkedin_company:{slug}"
    return metadata


def build_company_identity_lookup(corpus_paths: list[Path]) -> dict[str, dict[str, str]]:
    """Read JSONL company metadata and index it by RapidAPI ID and LinkedIn slug."""

    lookup: dict[str, dict[str, str]] = {}
    for path in corpus_paths or []:
        corpus_path = Path(path)
        if not corpus_path.exists():
            continue
        with corpus_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                metadata = _metadata_from_row(row)
                rapidapi_id = metadata.get("rapidapi_company_id", "")
                slug = metadata.get("company_public_identifier", "")
                if rapidapi_id:
                    lookup[f"rapidapi:{rapidapi_id}"] = dict(metadata)
                if slug:
                    lookup[f"linkedin_company:{slug}"] = dict(metadata)
    return lookup


def _company_name(exp_or_company: dict[str, Any]) -> str:
    for field in ("company_name", "companyName", "organization", "company"):
        value = exp_or_company.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for company in _nested_company_objects(exp_or_company):
        value = _first_string(company, ("name", "company_name", "companyName"))
        if value:
            return value
    return ""


def _company_linkedin_url(exp_or_company: dict[str, Any]) -> str:
    url = _first_string(exp_or_company, _LINKEDIN_URL_FIELDS + ("company_url", "url"))
    if url:
        normalized = normalize_company_linkedin_url(url)
        if normalized:
            return normalized
    for company in _nested_company_objects(exp_or_company):
        url = _first_string(company, _LINKEDIN_URL_FIELDS + ("company_url", "url"))
        normalized = normalize_company_linkedin_url(url)
        if normalized:
            return normalized
    return ""


def _company_slug(exp_or_company: dict[str, Any], company_url: str = "") -> str:
    slug = _first_string(exp_or_company, _SLUG_FIELDS).lower()
    if slug:
        return slug
    for company in _nested_company_objects(exp_or_company):
        slug = _first_string(company, _SLUG_FIELDS).lower()
        if slug:
            return slug
    return extract_company_public_identifier(company_url)


def resolve_company_identity(
    exp_or_url: dict[str, Any] | str,
    lookup: dict[str, dict[str, str]] | None = None,
) -> dict[str, str]:
    """Resolve RapidAPI and LinkedIn company identity fields for an experience."""

    lookup = lookup or {}
    if isinstance(exp_or_url, str):
        company_url = normalize_company_linkedin_url(exp_or_url)
        slug = extract_company_public_identifier(company_url)
        result = {
            "rapidapi_company_id": "",
            "company_public_identifier": slug,
            "company_linkedin_url": company_url,
            "company_key": f"linkedin_company:{slug}" if slug else "",
            "company_name": "",
        }
    elif isinstance(exp_or_url, dict):
        rapidapi_id = extract_rapidapi_company_id(exp_or_url)
        company_url = _company_linkedin_url(exp_or_url)
        slug = _company_slug(exp_or_url, company_url)
        result = {
            "rapidapi_company_id": rapidapi_id,
            "company_public_identifier": slug,
            "company_linkedin_url": company_url or (f"https://www.linkedin.com/company/{slug}" if slug else ""),
            "company_key": "",
            "company_name": _company_name(exp_or_url),
        }
    else:
        result = {
            "rapidapi_company_id": "",
            "company_public_identifier": "",
            "company_linkedin_url": "",
            "company_key": "",
            "company_name": "",
        }

    candidate_keys = []
    if result.get("rapidapi_company_id"):
        candidate_keys.append(f"rapidapi:{result['rapidapi_company_id']}")
    if result.get("company_public_identifier"):
        candidate_keys.append(f"linkedin_company:{result['company_public_identifier']}")

    metadata: dict[str, str] = {}
    for key in candidate_keys:
        if key in lookup:
            metadata = lookup[key]
            break
    for key, value in metadata.items():
        if value and (not result.get(key) or key in {"company_name", "company_linkedin_url", "company_public_identifier"}):
            result[key] = value

    rapidapi_id = result.get("rapidapi_company_id", "")
    slug = result.get("company_public_identifier", "")
    if rapidapi_id and not _is_harmonic_urn(rapidapi_id):
        result["company_key"] = f"rapidapi:{rapidapi_id}"
    elif slug:
        result["company_key"] = f"linkedin_company:{slug}"
    else:
        result["company_key"] = ""
    if result.get("company_linkedin_url"):
        result["company_linkedin_url"] = normalize_company_linkedin_url(result["company_linkedin_url"]) or result["company_linkedin_url"]
    return result


def _normalize_date(value: Any) -> dict[str, int | None] | None:
    if isinstance(value, dict):
        year = value.get("year") or value.get("start_year") or value.get("end_year")
        try:
            year_int = int(year)
        except (TypeError, ValueError):
            return None
        date: dict[str, int | None] = {"year": year_int, "month": None, "day": None}
        for field in ("month", "day"):
            raw = value.get(field)
            if raw in (None, ""):
                continue
            try:
                date[field] = int(raw)
            except (TypeError, ValueError):
                date[field] = None
        return date
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"present", "current", "now"}:
            return None
        match = re.match(r"^(\d{4})(?:[-/](\d{1,2}))?(?:[-/](\d{1,2}))?", text)
        if match:
            return {
                "year": int(match.group(1)),
                "month": int(match.group(2)) if match.group(2) else None,
                "day": int(match.group(3)) if match.group(3) else None,
            }
    return None


def _date_from_fields(exp: dict[str, Any], primary_fields: tuple[str, ...], year_fields: tuple[str, ...], month_fields: tuple[str, ...]) -> dict[str, int | None] | None:
    for field in primary_fields:
        date = _normalize_date(exp.get(field))
        if date:
            return date
    year = _first_string(exp, year_fields)
    if year:
        date_input: dict[str, Any] = {"year": year}
        month = _first_string(exp, month_fields)
        if month:
            date_input["month"] = month
        return _normalize_date(date_input)
    return None


def _bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def _location(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _first_string(value, ("location", "name", "text", "city"))
    return ""


def rapidapi_experience_to_powerpacks(
    exp: dict[str, Any],
    lookup: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Convert a RapidAPI work experience to Powerpacks' stable shape."""

    exp = exp if isinstance(exp, dict) else {}
    identity = resolve_company_identity(exp, lookup)
    title = _first_string(exp, ("title", "position", "role"))
    company_name = identity.get("company_name") or _company_name(exp)
    starts_at = _date_from_fields(
        exp,
        ("starts_at", "start_date", "startDate", "start", "from"),
        ("start_year", "startYear"),
        ("start_month", "startMonth"),
    )
    ends_at = _date_from_fields(
        exp,
        ("ends_at", "end_date", "endDate", "end", "to"),
        ("end_year", "endYear"),
        ("end_month", "endMonth"),
    )
    explicit_current = None
    for field in ("is_current_position", "is_current", "current"):
        explicit_current = _bool_value(exp.get(field))
        if explicit_current is not None:
            break
    is_current = explicit_current if explicit_current is not None else ends_at is None

    return {
        "title": title,
        "company": company_name,
        "company_name": company_name,
        "rapidapi_company_id": identity.get("rapidapi_company_id", ""),
        "company_public_identifier": identity.get("company_public_identifier", ""),
        "company_linkedin_url": identity.get("company_linkedin_url", ""),
        "company_key": identity.get("company_key", ""),
        "description": _first_string(exp, ("description", "summary")),
        "starts_at": starts_at,
        "ends_at": ends_at,
        "is_current_position": bool(is_current),
        "location": _location(exp.get("location")),
        "source": "rapidapi",
    }
