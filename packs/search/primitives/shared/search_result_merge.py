"""Shared candidate merge/provenance helpers for search execution.

These helpers are backend-neutral: TurboPuffer and local DuckDB retrieval rows
both flow through the same result-processing step.  Keeping them outside the
TurboPuffer client makes that shared pipeline boundary explicit.
"""

from __future__ import annotations

from typing import Any


def base_person_id(value: str) -> str:
    parts = str(value).split("-")
    if len(parts) == 6 and parts[5].isdigit():
        return "-".join(parts[:5])
    return str(value)


def dedupe_people(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Collapse position rows to unique people while preserving provenance.

    `limit <= 0` keeps the full retrieved frontier. Summary/filter-only rows are
    person-level evidence and intentionally do not create matched-position
    evidence unless a later role/company-position row contributes it.
    """
    candidates: list[dict[str, Any]] = []
    by_person: dict[str, dict[str, Any]] = {}
    representative_fields = [
        "position_id", "position_title", "city", "state", "country", "macro_region", "metro_areas",
        "role_track", "seniority_band", "company_name", "company_id", "is_current",
    ]
    for row in rows:
        person_id = str(row.get("person_id") or row.get("base_id") or base_person_id(str(row.get("id"))))
        vertical_sources = list(row.get("vertical_sources") or [])
        retrieval_mode = row.get("retrieval_mode")
        if retrieval_mode and retrieval_mode not in vertical_sources:
            vertical_sources.append(str(retrieval_mode))
        position_id = row.get("position_id") or row.get("id")
        contributes_position_evidence = retrieval_mode not in {"summary", "filter_only"}
        existing = by_person.get(person_id)
        if existing is not None:
            sources = list(existing.get("vertical_sources") or [])
            for source in vertical_sources:
                if source not in sources:
                    sources.append(source)
            existing["vertical_sources"] = sources
            matched = list(existing.get("matched_position_ids") or [])
            if position_id and contributes_position_evidence and position_id not in matched:
                matched.append(position_id)
            existing["matched_position_ids"] = matched
            if contributes_position_evidence and not existing.get("position_id"):
                existing["position_id"] = position_id
            if contributes_position_evidence:
                for field in representative_fields:
                    value = row.get(field)
                    if value is not None and existing.get(field) in (None, "", [], {}):
                        existing[field] = value
            for flag in ["has_core_regex", "has_adjacent_regex"]:
                if row.get(flag):
                    existing[flag] = True
            if row.get("bucket") == "good" or (row.get("bucket") and not existing.get("bucket")):
                existing["bucket"] = row.get("bucket")
            if row.get("score") is not None and (
                existing.get("score") is None or float(row.get("score") or 0.0) > float(existing.get("score") or 0.0)
            ):
                existing["score"] = row.get("score")
            continue
        candidate = {
            "person_id": person_id,
            "position_id": None if retrieval_mode == "summary" else position_id,
            "score": row.get("score"),
            "position_title": row.get("position_title"),
            "city": row.get("city"),
            "state": row.get("state"),
            "country": row.get("country"),
            "macro_region": row.get("macro_region"),
            "metro_areas": row.get("metro_areas"),
            "role_track": row.get("role_track"),
            "seniority_band": row.get("seniority_band"),
            "company_name": row.get("company_name"),
            "company_id": row.get("company_id"),
            "is_current": row.get("is_current"),
            "vertical_sources": vertical_sources,
            "matched_position_ids": [position_id] if position_id and contributes_position_evidence else [],
        }
        for key in ["has_core_regex", "has_adjacent_regex", "bucket"]:
            if key in row:
                candidate[key] = row[key]
        if row.get("retrieval_batched_base_ids"):
            candidate["retrieval_batched_base_ids"] = True
            candidate["base_id_batch_count"] = row.get("base_id_batch_count")
            candidate["base_id_batch_size"] = row.get("base_id_batch_size")
        candidates.append(candidate)
        by_person[person_id] = candidate
    return candidates[:limit] if limit and limit > 0 else candidates


def merge_agentic_sql_candidates(
    candidates: list[dict[str, Any]],
    sql_candidates: list[Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Union agent-authored SQL vertical people into the candidate pool.

    Mirrors merge_company_union_candidates: people already retrieved gain the
    `agentic_sql` provenance tag (a cross-vertical confidence signal); people
    found only by SQL are appended so they flow through the same hydration and
    LLM filter/rerank steps as every other candidate.
    """
    if not sql_candidates:
        return candidates
    merged = [dict(candidate) for candidate in candidates]
    by_person = {str(candidate.get("person_id")): candidate for candidate in merged if candidate.get("person_id")}
    for rank, raw in enumerate(sql_candidates, start=1):
        item = raw if isinstance(raw, dict) else {"person_id": raw}
        person_id = base_person_id(str(item.get("person_id") or item.get("base_id") or item.get("id") or ""))
        if not person_id:
            continue
        evidence = item.get("evidence")
        existing = by_person.get(person_id)
        if existing is not None:
            sources = list(existing.get("vertical_sources") or [])
            if "agentic_sql" not in sources:
                sources.append("agentic_sql")
            existing["vertical_sources"] = sources
            existing.setdefault("agentic_sql_rank", rank)
            if evidence and not existing.get("agentic_sql_evidence"):
                existing["agentic_sql_evidence"] = evidence
            continue
        candidate = {
            "person_id": person_id,
            "position_id": item.get("position_id"),
            "score": item.get("score"),
            "position_title": item.get("position_title"),
            "city": item.get("city"),
            "state": item.get("state"),
            "country": item.get("country"),
            "macro_region": item.get("macro_region"),
            "metro_areas": item.get("metro_areas"),
            "role_track": item.get("role_track"),
            "seniority_band": item.get("seniority_band"),
            "company_name": item.get("company_name"),
            "company_id": item.get("company_id"),
            "is_current": item.get("is_current"),
            "vertical_sources": ["agentic_sql"],
            "agentic_sql_rank": rank,
        }
        if evidence:
            candidate["agentic_sql_evidence"] = evidence
        if candidate.get("position_id"):
            candidate["matched_position_ids"] = [candidate["position_id"]]
        merged.append(candidate)
        by_person[person_id] = candidate
        if limit and limit > 0 and len(merged) >= limit:
            break
    return merged[:limit] if limit and limit > 0 else merged


def merge_company_union_candidates(
    candidates: list[dict[str, Any]],
    union_candidates: list[Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not union_candidates:
        return candidates
    merged = [dict(candidate) for candidate in candidates]
    by_person = {str(candidate.get("person_id")): candidate for candidate in merged if candidate.get("person_id")}
    for rank, raw in enumerate(union_candidates, start=1):
        item = raw if isinstance(raw, dict) else {"person_id": raw}
        person_id = base_person_id(str(item.get("person_id") or item.get("base_id") or item.get("id") or ""))
        if not person_id:
            continue
        existing = by_person.get(person_id)
        if existing is not None:
            sources = list(existing.get("vertical_sources") or [])
            if "company_filter" not in sources:
                sources.append("company_filter")
            existing["vertical_sources"] = sources
            existing.setdefault("company_union_rank", rank)
            if item.get("position_id") or item.get("id"):
                matched = list(existing.get("matched_position_ids") or [])
                position_id = item.get("position_id") or item.get("id")
                if position_id and position_id not in matched:
                    matched.append(position_id)
                existing["matched_position_ids"] = matched
            continue
        candidate = {
            "person_id": person_id,
            "position_id": item.get("position_id") or item.get("id"),
            "score": item.get("score"),
            "position_title": item.get("position_title"),
            "city": item.get("city"),
            "state": item.get("state"),
            "country": item.get("country"),
            "macro_region": item.get("macro_region"),
            "metro_areas": item.get("metro_areas"),
            "role_track": item.get("role_track"),
            "seniority_band": item.get("seniority_band"),
            "company_name": item.get("company_name"),
            "company_id": item.get("company_id"),
            "is_current": item.get("is_current"),
            "vertical_sources": ["company_filter"],
            "company_union_rank": rank,
        }
        if candidate.get("position_id"):
            candidate["matched_position_ids"] = [candidate["position_id"]]
        merged.append(candidate)
        by_person[person_id] = candidate
        if limit and limit > 0 and len(merged) >= limit:
            break
    return merged[:limit] if limit and limit > 0 else merged
