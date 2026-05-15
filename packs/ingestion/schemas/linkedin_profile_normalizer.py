"""Stdlib-only LinkedIn/RapidAPI profile normalizer.

The public normalizer always returns a dictionary with a stable shape. It is
purposefully permissive about provider field names while conservative about
error/unrecognized payloads.
"""

from __future__ import annotations

from typing import Any


_PROFILE_KEYS = [
    "success",
    "error",
    "public_identifier",
    "member_id",
    "first_name",
    "last_name",
    "full_name",
    "headline",
    "summary",
    "location_str",
    "city",
    "state",
    "country",
    "profile_pic_url",
    "linkedin_url",
    "connections",
    "skills",
    "languages",
    "certifications",
    "education",
    "experiences",
]


def _empty(error: str = "") -> dict[str, Any]:
    return {
        "success": False if error else True,
        "error": error,
        "public_identifier": "",
        "member_id": "",
        "first_name": "",
        "last_name": "",
        "full_name": "",
        "headline": "",
        "summary": "",
        "location_str": "",
        "city": "",
        "state": "",
        "country": "",
        "profile_pic_url": "",
        "linkedin_url": "",
        "connections": "",
        "skills": [],
        "languages": [],
        "certifications": [],
        "education": [],
        "experiences": [],
    }


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return ""


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _date_dict(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    year = value.get("year")
    if year in (None, ""):
        return None
    result = {"year": year, "month": value.get("month"), "day": value.get("day")}
    return result


def _normalize_experience(exp: Any) -> dict[str, Any] | None:
    if not isinstance(exp, dict):
        return None
    company = exp.get("company") if isinstance(exp.get("company"), dict) else {}
    normalized = dict(exp)
    normalized.setdefault("title", _first(exp, "title", "position", "role"))
    if "company_name" not in normalized:
        normalized["company_name"] = _first(exp, "company_name", "companyName", "organization") or _string(company.get("name"))
    if "company" not in normalized or isinstance(normalized.get("company"), dict):
        normalized["company"] = normalized.get("company_name", "")
    for output_key, input_keys in {
        "starts_at": ("starts_at", "start_date", "startDate", "start"),
        "ends_at": ("ends_at", "end_date", "endDate", "end"),
    }.items():
        date = None
        for input_key in input_keys:
            date = _date_dict(exp.get(input_key))
            if date is not None:
                break
        normalized[output_key] = date
    return normalized


def _normalize_education(edu: Any) -> dict[str, Any] | None:
    if not isinstance(edu, dict):
        return None
    normalized = dict(edu)
    normalized.setdefault("school", _first(edu, "school", "school_name", "schoolName", "name"))
    for output_key, input_keys in {
        "starts_at": ("starts_at", "start_date", "startDate", "start"),
        "ends_at": ("ends_at", "end_date", "endDate", "end"),
    }.items():
        for input_key in input_keys:
            date = _date_dict(edu.get(input_key))
            if date is not None:
                normalized[output_key] = date
                break
    return normalized


def _unwrap_data(data: dict[str, Any]) -> dict[str, Any]:
    inner = data.get("data")
    if isinstance(inner, dict) and not ("success" in inner and "data" not in inner):
        return inner
    return data


def _compact_error(data: dict[str, Any]) -> str:
    for key in ("error", "message", "detail"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
        if isinstance(value, dict):
            nested = _compact_error(value)
            if nested:
                return nested
    return "profile normalization failed"


def detect_linkedin_schema(data: dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return "unknown"
    if data.get("success") is False or data.get("error") or (data.get("message") and not any(k in data for k in ("data", "full_name", "fullName"))):
        return "error"
    if all(key in data for key in _PROFILE_KEYS):
        return "normalized"
    if isinstance(data.get("data"), dict):
        inner = data["data"]
        if inner.get("success") is False or inner.get("error"):
            return "error"
        inner_schema = detect_linkedin_schema(inner)
        if inner_schema != "unknown":
            return inner_schema
        return "rapidapi_parsed"
    if any(key in data for key in ("experiences", "work_experience", "workExperience")) and any(
        key in data for key in ("full_name", "fullName", "first_name", "firstName", "public_identifier", "publicIdentifier")
    ):
        return "rapidapi_parsed"
    if any(key in data for key in ("profile", "profile_url", "profileURL")) and any(key in data for key in ("firstName", "lastName", "fullName")):
        return "rapidapi_converted"
    if any(key in data for key in ("fullPositions", "position")):
        return "linkedin_native"
    return "unknown"


def normalize_linkedin_profile(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize supported LinkedIn profile payloads into a stable dict."""

    if not isinstance(data, dict):
        return _empty("expected dict payload")
    schema = detect_linkedin_schema(data)
    if schema == "error":
        return _empty(_compact_error(data))
    profile = _unwrap_data(data)
    if schema == "normalized":
        result = _empty("")
        for key in _PROFILE_KEYS:
            if key in profile:
                result[key] = profile[key]
        result["success"] = bool(profile.get("success", True))
        result["error"] = _string(profile.get("error"))
        if not result["success"]:
            result["error"] = result["error"] or "profile normalization failed"
        return result
    if schema == "unknown":
        return _empty("unrecognized linkedin profile payload")

    result = _empty("")
    first = _string(_first(profile, "first_name", "firstName", "first"))
    last = _string(_first(profile, "last_name", "lastName", "last"))
    full = _string(_first(profile, "full_name", "fullName", "name"))
    if full and (not first or not last):
        parts = full.split()
        if not first and parts:
            first = parts[0]
        if not last and len(parts) > 1:
            last = " ".join(parts[1:])
    if not full:
        full = f"{first} {last}".strip()

    location = profile.get("location") if isinstance(profile.get("location"), dict) else {}
    location_text = profile.get("location") if isinstance(profile.get("location"), str) else ""

    experiences = _first(profile, "experiences", "work_experience", "workExperience", "experience")
    if not experiences:
        experiences = _first(profile, "fullPositions", "position", "positions")
    if isinstance(experiences, dict):
        experiences = experiences.get("values") or experiences.get("items") or [experiences]

    education = _first(profile, "education", "educations")
    if isinstance(education, dict):
        education = education.get("values") or education.get("items") or [education]

    normalized_experiences = [item for item in (_normalize_experience(exp) for exp in _as_list(experiences)) if item is not None]
    normalized_education = [item for item in (_normalize_education(edu) for edu in _as_list(education)) if item is not None]

    has_identity = any(
        _string(_first(profile, key))
        for key in ("public_identifier", "publicIdentifier", "username", "member_id", "memberId", "linkedin_url", "profile_url", "profileURL")
    ) or bool(full or first or last or normalized_experiences or normalized_education)
    if not has_identity:
        return _empty("unrecognized linkedin profile payload")

    result.update(
        {
            "success": True,
            "error": "",
            "public_identifier": _string(_first(profile, "public_identifier", "publicIdentifier", "username")),
            "member_id": _string(_first(profile, "member_id", "memberId", "id")),
            "first_name": first,
            "last_name": last,
            "full_name": full,
            "headline": _string(_first(profile, "headline", "occupation")),
            "summary": _string(_first(profile, "summary", "about")),
            "location_str": _string(_first(profile, "location_str", "locationName")) or _string(location_text) or _string(location.get("location")),
            "city": _string(_first(profile, "city")) or _string(location.get("city")),
            "state": _string(_first(profile, "state")) or _string(location.get("state")),
            "country": _string(_first(profile, "country")) or _string(location.get("country")),
            "profile_pic_url": _string(_first(profile, "profile_pic_url", "profilePicUrl", "profilePicture", "profile_picture_url")),
            "linkedin_url": _string(_first(profile, "linkedin_url", "profile_url", "profileURL", "url")),
            "connections": _string(_first(profile, "connections", "connection_count", "connectionCount")),
            "skills": _as_list(_first(profile, "skills")),
            "languages": _as_list(_first(profile, "languages")),
            "certifications": _as_list(_first(profile, "certifications")),
            "education": normalized_education,
            "experiences": normalized_experiences,
        }
    )
    return result
