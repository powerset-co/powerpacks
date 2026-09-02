"""Normalize reviewed eligibility filters and compile supported retrieval fields.

The English ``filters`` list is the recruiter-owned contract. ``retrieval_filters`` is a
deterministic execution projection for the small subset the role-search backend can represent
without guessing. Unsupported English filters remain in the plan for review and evaluation.
"""
from __future__ import annotations

import copy
import re
from typing import Any


FILTER_SOURCES = {"jd", "user", "default"}
RETRIEVAL_FILTER_FIELDS = ("years_experience_min", "years_experience_max")

_NUMBER = r"(?P<{name}>\d+(?:\.\d+)?)"
_YOE_UNIT = r"(?:years?|yrs?)\s+(?:of\s+)?(?:(?:professional|industry|work)\s+)?(?:(?:software\s+)?engineering\s+)?experience|yoe"
_DOMAIN_TAIL = r"(?!\s+(?:in|building|developing|designing|operating|working|on|with)\b)"
_YOE_RANGE = re.compile(
    rf"\b{_NUMBER.format(name='minimum')}\s*(?:-|–|—|to)\s*"
    rf"{_NUMBER.format(name='maximum')}\s*(?:{_YOE_UNIT})\b{_DOMAIN_TAIL}",
    re.IGNORECASE,
)
_YOE_MIN = re.compile(
    rf"(?:\b(?:at\s+least|minimum(?:\s+of)?|min\.?)\s*)?"
    rf"\b{_NUMBER.format(name='minimum')}\s*(?:\+|or\s+more)?\s*(?:{_YOE_UNIT})\b{_DOMAIN_TAIL}",
    re.IGNORECASE,
)
_YOE_MAX = re.compile(
    rf"\b(?:up\s+to|at\s+most|no\s+more\s+than|maximum(?:\s+of)?|max\.?)\s*"
    rf"{_NUMBER.format(name='maximum')}\s*(?:{_YOE_UNIT})\b{_DOMAIN_TAIL}",
    re.IGNORECASE,
)


def _clean_number(value: Any, *, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    number = float(value)
    if number < 0:
        raise ValueError(f"{field} must be non-negative")
    return int(number) if number.is_integer() else number


def normalize_plan_filters(raw: Any, *, default_source: str = "jd") -> list[dict[str, str]]:
    """Return canonical ``[{filter, source}]`` entries, accepting strings at generation time."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("plan filters must be a list")
    if default_source not in FILTER_SOURCES:
        raise ValueError(f"unsupported default filter source: {default_source!r}")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            text = str(item.get("filter") or "").strip()
            source = str(item.get("source") or default_source).strip().lower()
        else:
            text = str(item).strip()
            source = default_source
        if not text:
            continue
        if source not in FILTER_SOURCES:
            raise ValueError(f"unsupported filter source: {source!r}")
        key = " ".join(text.lower().split())
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"filter": text, "source": source})
    return normalized


def _number_from_match(match: re.Match[str], name: str) -> int | float:
    return _clean_number(float(match.group(name)), field=f"years_experience_{name}")


def compile_plan_filters(filters: Any) -> dict[str, int | float]:
    """Compile clear overall-YOE expressions; preserve every other English filter as English."""
    minimums: list[int | float] = []
    maximums: list[int | float] = []
    for item in normalize_plan_filters(filters):
        text = item["filter"]
        match = _YOE_RANGE.search(text)
        if match:
            minimums.append(_number_from_match(match, "minimum"))
            maximums.append(_number_from_match(match, "maximum"))
            continue
        match = _YOE_MAX.search(text)
        if match:
            maximums.append(_number_from_match(match, "maximum"))
            continue
        match = _YOE_MIN.search(text)
        if match and (
            "+" in match.group(0)
            or re.search(r"\b(?:at\s+least|minimum|min\.)\b", match.group(0), re.I)
            or re.search(r"\byoe\b", match.group(0), re.I)
        ):
            minimums.append(_number_from_match(match, "minimum"))

    compiled: dict[str, int | float] = {}
    if minimums:
        compiled["years_experience_min"] = max(minimums)
    if maximums:
        compiled["years_experience_max"] = min(maximums)
    if (
        "years_experience_min" in compiled
        and "years_experience_max" in compiled
        and compiled["years_experience_min"] > compiled["years_experience_max"]
    ):
        raise ValueError("compiled years_experience_min must not exceed years_experience_max")
    return compiled


def normalize_retrieval_filters(raw: Any) -> dict[str, int | float]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("retrieval_filters must be an object")
    unknown = sorted(set(raw) - set(RETRIEVAL_FILTER_FIELDS))
    if unknown:
        raise ValueError(f"unsupported retrieval filters: {unknown}")
    normalized = {
        field: _clean_number(raw[field], field=field)
        for field in RETRIEVAL_FILTER_FIELDS
        if raw.get(field) is not None
    }
    if (
        "years_experience_min" in normalized
        and "years_experience_max" in normalized
        and normalized["years_experience_min"] > normalized["years_experience_max"]
    ):
        raise ValueError("years_experience_min must not exceed years_experience_max")
    return normalized


def bind_plan_filters(plan: dict[str, Any]) -> dict[str, Any]:
    """Return a copy whose derived retrieval projection exactly matches its English filters."""
    bound = copy.deepcopy(plan)
    filters = normalize_plan_filters(bound.get("filters"))
    if "filters" in bound or filters:
        bound["filters"] = filters
    compiled = compile_plan_filters(filters)
    if compiled:
        bound["retrieval_filters"] = compiled
    else:
        bound.pop("retrieval_filters", None)
    return bound


def validate_plan_filter_contract(plan: dict[str, Any]) -> dict[str, int | float]:
    """Reject hidden or stale structured values in an approved plan."""
    filters = normalize_plan_filters(plan.get("filters"))
    compiled = compile_plan_filters(filters)
    structured = normalize_retrieval_filters(plan.get("retrieval_filters"))
    if structured != compiled:
        raise ValueError(
            "retrieval_filters must equal the deterministic compilation of plan filters; "
            f"expected {compiled}, got {structured}"
        )
    return compiled


def enforce_payload_retrieval_filters(
    payload: dict[str, Any],
    retrieval_filters: dict[str, Any],
) -> dict[str, Any]:
    """Make reviewed compiled values authoritative over per-probe expansion output."""
    normalized = normalize_retrieval_filters(retrieval_filters)
    target = payload.get("role_search_filters")
    if not isinstance(target, dict):
        target = payload
    for field in RETRIEVAL_FILTER_FIELDS:
        target.pop(field, None)
    target.update(normalized)
    return payload
