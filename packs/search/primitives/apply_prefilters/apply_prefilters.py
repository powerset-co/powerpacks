#!/usr/bin/env python3
"""Apply ID-producing prefilters and record base candidate IDs."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB_DIR))

from turbopuffer_client import (  # noqa: E402
    ADJACENCY_EXCLUDE_SENIORITY,
    ADJACENCY_LIMIT,
    adjacency_family_for_payload,
    allowed_operator_ids_from_payload,
    bm25_adjacency_rows,
    comparison,
    company_filter_applies_to_role_search,
    effective_adjacent_role_ids,
    extract_base_ids,
    filter_only_rows_for_namespace,
    filters_from_role_payload,
    get_adjacency_queries,
    has_role_constraint,
    is_non_operational_title,
    load_env_file,
    merge_adjacency_queries,
    namespace_name,
    role_payload_from_state,
    search_mode_for_payload,
    seniority_intent,
)
from postgres_client import fetch_interaction_filter_person_ids, fetch_social_filter_person_ids  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def append_event(state_path: Path, event: dict[str, Any]) -> None:
    log_path = state_path.with_suffix(state_path.suffix + ".events.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def intersect_ordered(left: list[str] | None, right: list[str]) -> list[str]:
    if left is None:
        return list(dict.fromkeys(right))
    allowed = set(right)
    return [pid for pid in left if pid in allowed]


def and_filter(clauses: list[tuple]) -> tuple | None:
    clauses = [clause for clause in clauses if clause is not None]
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else ("And", clauses)


def chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def education_filter(payload: dict[str, Any], education_ids: list[str] | None = None) -> tuple | None:
    ids = education_ids if education_ids is not None else payload.get("education_ids")
    clauses: list[tuple] = []
    if ids:
        clauses.append(comparison("canonical_education_id", "In", ids))
    if payload.get("degree_levels"):
        # Normalize to title-case to match TurboPuffer degree_normalized values
        degree_map = {"bachelors": "Bachelors", "masters": "Masters", "mba": "MBA", "phd": "PhD", "md": "MD", "jd": "JD"}
        degrees = [degree_map.get(d.lower(), d) for d in payload["degree_levels"]]
        clauses.append(comparison("degree_normalized", "In", degrees))
    if payload.get("fields_of_study"):
        fields = list(payload["fields_of_study"])
        if len(fields) == 1:
            clauses.append(comparison("field_of_study", "ContainsAllTokens", fields[0]))
        else:
            clauses.append(("Or", [comparison("field_of_study", "ContainsAllTokens", field) for field in fields]))
    if payload.get("graduation_year_min") is not None:
        clauses.append(comparison("graduation_year", "Gte", payload["graduation_year_min"]))
    if payload.get("graduation_year_max") is not None:
        clauses.append(comparison("graduation_year", "Lte", payload["graduation_year_max"]))
    operator_ids = allowed_operator_ids_from_payload(payload)
    if operator_ids:
        clauses.append(comparison("allowed_operator_ids", "ContainsAny", operator_ids))
    return and_filter(clauses)


async def education_base_ids(payload: dict[str, Any], *, page_size: int, max_ids: int) -> tuple[list[str], dict[str, Any]] | None:
    if not any(payload.get(key) for key in ["education_ids", "degree_levels", "fields_of_study", "graduation_year_min", "graduation_year_max"]):
        return None
    education_ids = [str(value) for value in payload.get("education_ids") or [] if value]
    education_op = payload.get("education_op") or "or"
    if education_op == "and" and len(education_ids) > 1:
        sets: list[list[str]] = []
        for school_id in education_ids:
            filters = education_filter(payload, [school_id])
            if filters is None:
                continue
            rows = await filter_only_rows_for_namespace("education", filters, ["person_id"], page_size=page_size, max_results=max_ids)
            sets.append(extract_base_ids(rows))
        if not sets:
            ids: list[str] = []
        else:
            allowed = set(sets[0])
            for ids_for_school in sets[1:]:
                allowed &= set(ids_for_school)
            ids = [pid for pid in sets[0] if pid in allowed]
        return ids[:max_ids], {"stage": "education", "mode": "and", "input_count": len(education_ids), "matched": len(ids)}

    filters = education_filter(payload)
    if filters is None:
        return None
    rows = await filter_only_rows_for_namespace("education", filters, ["person_id"], page_size=page_size, max_results=max_ids)
    ids = extract_base_ids(rows)
    return ids, {"stage": "education", "mode": education_op, "input_count": len(education_ids), "matched": len(ids)}


async def tech_skill_base_ids(payload: dict[str, Any], *, page_size: int, max_ids: int) -> tuple[list[str], dict[str, Any]] | None:
    skills = [str(value) for value in payload.get("tech_skills") or [] if value]
    if not skills:
        return None
    clauses = [comparison("tech_skills", "ContainsAny", skills)]
    operator_ids = allowed_operator_ids_from_payload(payload)
    if operator_ids:
        clauses.append(comparison("allowed_operator_ids", "ContainsAny", operator_ids))
    rows = await filter_only_rows_for_namespace("summaries", and_filter(clauses), [], page_size=page_size, max_results=max_ids)
    ids = extract_base_ids(rows)
    return ids, {"stage": "tech_skills", "input_count": len(skills), "matched": len(ids)}


def social_base_ids(payload: dict[str, Any], *, env_file: Path | None, max_ids: int) -> tuple[list[str], dict[str, Any]] | None:
    keys = [
        "x_followers_min", "x_followers_max", "li_followers_min", "li_followers_max",
        "li_connections_min", "li_connections_max", "ig_followers_min", "ig_followers_max",
    ]
    active = {key: payload.get(key) for key in keys if payload.get(key) is not None}
    if not active:
        return None
    ids = fetch_social_filter_person_ids(payload, env_file=env_file)
    return ids[:max_ids], {"stage": "social", "filters": active, "matched": len(ids)}


def interaction_base_ids(payload: dict[str, Any], *, env_file: Path | None, max_ids: int) -> tuple[list[str], dict[str, Any]] | None:
    keys = ["operator_interaction_min", "operator_interaction_max", "set_interaction_min", "set_interaction_max"]
    active = {key: payload.get(key) for key in keys if payload.get(key) is not None}
    if not active:
        return None
    ids = fetch_interaction_filter_person_ids(payload, env_file=env_file)
    return ids[:max_ids], {"stage": "interaction", "filters": active, "matched": len(ids)}


def company_people_payload(
    payload: dict[str, Any],
    company_ids: list[str],
    *,
    base_candidate_ids: list[str] | None,
    include_role_filters: bool,
    adjacent_role_filter: bool = False,
) -> dict[str, Any]:
    people_payload = dict(payload)
    people_payload["company_ids"] = company_ids
    people_payload["search_mode"] = "COMPANY_INTERSECTION"
    if base_candidate_ids:
        people_payload["base_candidate_ids"] = base_candidate_ids
    if payload.get("is_current_company") is not None:
        people_payload["is_current_role"] = bool(payload.get("is_current_company"))
    if not include_role_filters:
        for key in ["role_ids", "role_tracks", "semantic_query", "bm25_queries"]:
            people_payload.pop(key, None)
    if adjacent_role_filter:
        people_payload.pop("role_tracks", None)
        people_payload["role_ids"] = effective_adjacent_role_ids(payload)
        if payload.get("adjacent_departments"):
            people_payload["role_tracks"] = [str(value) for value in payload.get("adjacent_departments") or [] if value]
        if payload.get("adjacent_seniority"):
            people_payload["seniority_bands"] = [str(value) for value in payload.get("adjacent_seniority") or [] if value]
    return people_payload


def with_adjacency_exclusions(filters: tuple | None) -> tuple | None:
    exclusion = comparison("seniority_band", "NotIn", ADJACENCY_EXCLUDE_SENIORITY)
    if filters is None:
        return exclusion
    if isinstance(filters, tuple) and filters and filters[0] == "And":
        return ("And", [*list(filters[1]), exclusion])
    return ("And", [filters, exclusion])


def candidate_from_row(row: dict[str, Any], *, source: str, rank: int | None = None) -> dict[str, Any]:
    person_id = str(row.get("base_id") or row.get("person_id") or row.get("id") or "")
    out = {
        "person_id": person_id,
        "position_id": row.get("position_id") or row.get("id"),
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
        "score": row.get("score"),
        "vertical_sources": [source],
    }
    if rank is not None:
        out["company_union_rank"] = rank
    return out


async def company_base_ids(
    payload: dict[str, Any],
    *,
    page_size: int,
    max_ids: int,
    company_chunk_size: int,
    company_concurrency: int,
    base_candidate_ids: list[str] | None = None,
) -> tuple[list[str], dict[str, Any]] | None:
    company_ids = [str(value) for value in payload.get("company_ids") or [] if value]
    if not company_ids:
        return None

    mode = search_mode_for_payload(payload)
    role_prefilter = company_filter_applies_to_role_search(payload)
    role_constrained = has_role_constraint(payload)
    adjacency_queries: list[str] = []
    adjacency_source = None
    adjacency_method = "filter_only"
    adjacent_ids = effective_adjacent_role_ids(payload)
    if not role_prefilter and role_constrained and not adjacent_ids:
        static_queries = get_adjacency_queries(adjacency_family_for_payload(payload), seniority_intent(payload))
        adjacency_queries, adjacency_source = merge_adjacency_queries(
            [str(value) for value in payload.get("company_adjacency_queries") or [] if str(value).strip()],
            static_queries,
        )
        adjacency_method = "bm25"
    elif not role_prefilter and adjacent_ids:
        adjacency_method = "role_id"

    async def run_chunk(chunk: list[str], semaphore: asyncio.Semaphore) -> list[dict[str, Any]]:
        async with semaphore:
            if not role_prefilter and role_constrained and adjacent_ids:
                people_payload = company_people_payload(
                    payload,
                    chunk,
                    base_candidate_ids=base_candidate_ids,
                    include_role_filters=False,
                    adjacent_role_filter=True,
                )
                filters = with_adjacency_exclusions(filters_from_role_payload(people_payload))
                if filters is None:
                    return []
                return await filter_only_rows_for_namespace(
                    "people",
                    filters,
                    [
                        "base_id", "position_title", "city", "state", "country", "macro_region", "metro_areas",
                        "role_track", "seniority_band", "company_name", "company_id", "is_current",
                    ],
                    page_size=page_size,
                    max_results=max_ids,
                )
            if not role_prefilter and role_constrained:
                people_payload = company_people_payload(
                    payload,
                    chunk,
                    base_candidate_ids=base_candidate_ids,
                    include_role_filters=False,
                )
                filters = with_adjacency_exclusions(filters_from_role_payload(people_payload))
                if filters is None:
                    return []
                rows = await bm25_adjacency_rows(
                    adjacency_queries,
                    filters,
                    top_k=min(max_ids, ADJACENCY_LIMIT),
                    include_attributes=[
                        "base_id", "position_title", "city", "state", "country", "macro_region", "metro_areas",
                        "role_track", "seniority_band", "company_name", "company_id", "is_current",
                    ],
                )
                return [row for row in rows if not is_non_operational_title(str(row.get("position_title") or ""))]

            people_payload = company_people_payload(
                payload,
                chunk,
                base_candidate_ids=base_candidate_ids,
                include_role_filters=True,
            )
            filters = filters_from_role_payload(people_payload)
            if filters is None:
                return []
            return await filter_only_rows_for_namespace(
                "people",
                filters,
                [
                    "base_id", "position_title", "city", "state", "country", "macro_region", "metro_areas",
                    "role_track", "seniority_band", "company_name", "company_id", "is_current",
                ],
                page_size=page_size,
                max_results=max_ids,
            )

    chunked = chunks(company_ids, max(1, company_chunk_size))
    semaphore = asyncio.Semaphore(max(1, company_concurrency))
    chunk_rows = await asyncio.gather(*(run_chunk(chunk, semaphore) for chunk in chunked))
    rows = [row for batch in chunk_rows for row in batch]
    ids = extract_base_ids(rows)
    candidates = []
    seen_people: set[str] = set()
    for rank, row in enumerate(rows, start=1):
        person_id = str(row.get("base_id") or row.get("person_id") or row.get("id") or "")
        if not person_id or person_id in seen_people:
            continue
        seen_people.add(person_id)
        candidates.append(candidate_from_row(row, source="company_filter", rank=rank))
    stage_name = "company_current" if payload.get("is_current_company") is not None else "large_company_intersection"
    return ids[:max_ids], {
        "stage": stage_name,
        "role_prefilter": role_prefilter,
        "search_mode": mode,
        "adjacency_method": adjacency_method if not role_prefilter else None,
        "adjacency_query_source": adjacency_source,
        "adjacency_queries": adjacency_queries,
        "input_count": len(company_ids),
        "matched": len(ids),
        "candidates": candidates[:max_ids] if not role_prefilter else [],
        "company_id_batches": len(chunked),
        "company_id_batch_size": company_chunk_size,
        "company_id_batch_concurrency": company_concurrency,
        "base_candidate_filter_count": len(base_candidate_ids or []),
    }


def should_run_company_prefilter(payload: dict[str, Any], threshold: int) -> bool:
    company_ids = payload.get("company_ids") or []
    stages = ((payload.get("prefilters") or {}).get("stages") or [])
    explicit = any(stage.get("stage") in {"large_company_intersection", "company_current"} for stage in stages if isinstance(stage, dict))
    return (
        explicit
        or bool(company_ids and payload.get("is_current_company") is not None)
        or search_mode_for_payload(payload) == "COMPANY_UNION"
        or len(company_ids) >= threshold
    )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    load_env_file(Path(args.env_file) if args.env_file else None)
    state_path = Path(args.state) if args.state else None
    state = read_json(state_path) if state_path else {}
    payload = json.loads(args.payload_json) if args.payload_json else role_payload_from_state(state)

    base_ids: list[str] | None = None
    company_union_ids: list[str] = []
    stage_outputs: list[dict[str, Any]] = []
    env_file = Path(args.env_file) if args.env_file else None
    prefilter_results = [
        await education_base_ids(payload, page_size=args.page_size, max_ids=args.max_ids),
        await tech_skill_base_ids(payload, page_size=args.page_size, max_ids=args.max_ids),
        social_base_ids(payload, env_file=env_file, max_ids=args.max_ids),
        interaction_base_ids(payload, env_file=env_file, max_ids=args.max_ids),
    ]
    for maybe_result in prefilter_results:
        if maybe_result is None:
            continue
        ids, stage = maybe_result
        base_ids = intersect_ordered(base_ids, ids)
        stage["frontier_after_stage"] = len(base_ids or [])
        stage_outputs.append(stage)

    maybe_company = await company_base_ids(
        payload,
        page_size=args.page_size,
        max_ids=args.max_ids,
        company_chunk_size=args.company_id_batch_size,
        company_concurrency=args.company_id_batch_concurrency,
        base_candidate_ids=base_ids,
    ) if should_run_company_prefilter(payload, args.company_prefilter_threshold) else None
    if maybe_company is not None:
        ids, stage = maybe_company
        if stage.get("role_prefilter") is False:
            company_union_ids = list(dict.fromkeys([*company_union_ids, *ids]))
            stage["frontier_after_stage"] = len(base_ids or [])
            stage["union_candidate_count"] = len(company_union_ids)
        else:
            base_ids = intersect_ordered(base_ids, ids)
            stage["frontier_after_stage"] = len(base_ids or [])
        stage_outputs.append(stage)

    final_ids = base_ids if base_ids is not None else []
    return {
        "namespaces": {
            "people": namespace_name("people"),
            "education": namespace_name("education"),
            "summaries": namespace_name("summaries"),
        },
        "stages": stage_outputs,
        "search_mode": search_mode_for_payload(payload),
        "ran_prefilters": bool(stage_outputs),
        "role_prefilter_ran": base_ids is not None,
        "base_candidate_ids": final_ids[: args.max_ids],
        "base_candidate_count": len(final_ids),
        "company_union_candidate_ids": company_union_ids[: args.max_ids],
        "company_union_candidates": [
            candidate
            for stage in stage_outputs
            if stage.get("role_prefilter") is False
            for candidate in (stage.get("candidates") or [])
        ][: args.max_ids],
        "company_union_candidate_count": len(company_union_ids),
        "truncated": len(final_ids) > args.max_ids,
    }


def record_step(state_path: Path, state: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> None:
    now = now_iso()
    state.setdefault("steps", []).append({
        "id": "apply_prefilters",
        "status": "completed",
        "recorded_at": now,
        "elapsed_ms": elapsed_ms,
        "output": output,
    })
    state["updated_at"] = now
    write_json(state_path, state)
    append_event(state_path, {
        "event": "record_step",
        "task_id": state.get("task_id"),
        "state": str(state_path),
        "step_id": "apply_prefilters",
        "status": "completed",
        "timestamp": now,
        "elapsed_ms": elapsed_ms,
        "base_candidate_count": output.get("base_candidate_count"),
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply Powerpacks prefilters")
    parser.add_argument("--state")
    parser.add_argument("--payload-json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--page-size", type=int, default=10000)
    parser.add_argument("--max-ids", type=int, default=50000)
    parser.add_argument("--company-prefilter-threshold", type=int, default=3000)
    parser.add_argument("--company-id-batch-size", type=int, default=500)
    parser.add_argument("--company-id-batch-concurrency", type=int, default=8)
    args = parser.parse_args()

    started = time.time()
    output = asyncio.run(run(args))
    elapsed_ms = int((time.time() - started) * 1000)
    if args.write_state:
        if not args.state:
            raise SystemExit("--write-state requires --state")
        state_path = Path(args.state)
        record_step(state_path, read_json(state_path), output, elapsed_ms)
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
