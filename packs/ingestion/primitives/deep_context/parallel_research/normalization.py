"""Normalize Parallel results into the standing research artifact contract."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from packs.ingestion.primitives.deep_context.parallel_research.queue import input_fingerprint


def _json_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _positions(result: dict[str, Any]) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    for value in _json_array(result.get("work_experience")):
        if isinstance(value, str):
            positions.append({
                "title": "", "company_name": value, "company_domain": None,
                "company_linkedin_url": None, "description": None, "start_date": None,
                "end_date": None, "is_current": False, "confidence": 0.5, "sources": [],
            })
            continue
        if not isinstance(value, dict):
            continue
        positions.append({
            "title": value.get("title") or value.get("position") or "",
            "company_name": next((str(value.get(key)) for key in (
                "company", "organization", "employer", "company_name", "name"
            ) if value.get(key)), ""),
            "company_domain": value.get("domain") or value.get("company_domain"),
            "company_linkedin_url": None,
            "description": value.get("description"),
            "start_date": value.get("start_date"),
            "end_date": value.get("end_date"),
            "is_current": value.get("current") or value.get("is_current", False),
            "confidence": value.get("confidence", 0.7),
            "sources": value.get("evidence") if isinstance(value.get("evidence"), list)
            else ([value["source"]] if value.get("source") else []),
        })
    return positions


def _education(result: dict[str, Any]) -> list[dict[str, Any]]:
    education: list[dict[str, Any]] = []
    for value in _json_array(result.get("education")):
        if isinstance(value, str):
            education.append({
                "school_name": value, "degree": None, "field_of_study": None,
                "start_year": None, "end_year": None, "confidence": 0.5, "source": "",
            })
            continue
        if not isinstance(value, dict):
            continue
        education.append({
            "school_name": next((str(value.get(key)) for key in (
                "school", "school_name", "institution", "university", "name"
            ) if value.get(key)), ""),
            "degree": value.get("degree"),
            "field_of_study": value.get("field") or value.get("field_of_study"),
            "start_year": value.get("start_year"),
            "end_year": value.get("end_year"),
            "confidence": value.get("confidence", 0.7),
            "source": str(value.get("evidence") or ""),
        })
    return education


def _quality(result: dict[str, Any]) -> tuple[float, list[str]]:
    positions = _json_array(result.get("work_experience"))
    education = _json_array(result.get("education"))
    score = 0.3 if result.get("real_name") else 0.0
    score += min(0.3, len(positions) * 0.1)
    score += min(0.2, len(education) * 0.1)
    score += 0.1 if result.get("location_city") else 0.0
    score += 0.1 if result.get("linkedin_url") else 0.0
    gaps = []
    for missing, label in (
        (not result.get("real_name"), "Real name not identified"),
        (not positions, "No work experience found"),
        (not education, "No education found"),
        (not result.get("location_city") and not result.get("location_country"), "Location unknown"),
        (not result.get("linkedin_url"), "No LinkedIn profile found"),
    ):
        if missing:
            gaps.append(label)
    return round(min(1.0, score), 2), gaps


def parallel_to_research_json(
    result: dict[str, Any], row: dict[str, str], handle: str, name: str, bio: str,
    *, research_method: str = "parallel-core2x",
) -> dict[str, Any]:
    """Normalize one provider result into the standing research artifact shape."""
    real_name = str(result.get("real_name") or name or handle)
    first, _, last = real_name.partition(" ")
    source_channel = (row.get("source_channel") or "phone").strip().lower()
    completeness, gaps = _quality(result)
    return {
        "research_id": f"{handle}-{date.today().isoformat()}",
        "query": f"@{handle} ({name}): {bio[:100]}",
        "status": "draft", "research_method": research_method,
        "person": {
            "full_name": real_name, "first_name": first, "last_name": last,
            "also_known_as": [handle, name] if real_name != name else [handle],
            "confidence": result.get("name_confidence", 0.3), "sources": [],
            "notes": result.get("name_evidence", ""),
        },
        "location": {
            "city": result.get("location_city") or "", "state": "",
            "country": result.get("location_country") or "", "raw": "",
            "confidence": 0.5 if result.get("location_city") or result.get("location_country") else 0.0,
            "source": "",
        },
        "headline": {
            "text": bio[:200] if bio else "", "confidence": 0.95 if bio else 0.0,
            "source": f"https://x.com/{handle}",
        },
        "summary": {
            "text": result.get("summary") or "", "confidence": 0.7,
            "source": "Parallel Deep Research",
        },
        "positions": _positions(result), "education": _education(result),
        "social": {
            "twitter_handle": handle if source_channel == "twitter" else None,
            "linkedin_url": result.get("linkedin_url"),
            "linkedin_status": "found" if result.get("linkedin_url") else "not_found",
            "github_url": result.get("github_url"),
            "personal_website": result.get("personal_website"),
            "primary_email": row.get("primary_email") if source_channel == "email" else None,
            "primary_phone": row.get("phone_e164") if source_channel == "phone" else None,
        },
        "metadata": {
            "total_sources_consulted": 0, "estimated_completeness": completeness,
            "gaps": gaps, "research_date": date.today().isoformat(),
            "research_method": research_method,
            "research_notes": result.get("research_notes") or "",
            "source_channel": source_channel or "unknown",
            "source_identifier": row.get("primary_email") or row.get("phone_e164") or handle,
            "input_fingerprint": input_fingerprint(row, handle),
        },
    }
