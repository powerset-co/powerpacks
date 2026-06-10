#!/usr/bin/env python3
"""Execute a Powerpacks role search in TurboPuffer."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PRIMITIVES_DIR = Path(__file__).resolve().parents[1]
LIB_DIR = PRIMITIVES_DIR / "lib"
SHARED_DIR = PRIMITIVES_DIR / "shared"
LOCAL_DIR = PRIMITIVES_DIR / "local"
TURBOPUFFER_DIR = PRIMITIVES_DIR / "turbopuffer"
for _path in [LIB_DIR, SHARED_DIR, LOCAL_DIR, TURBOPUFFER_DIR]:
    sys.path.insert(0, str(_path))

import search_backend_mode  # noqa: E402
from search_result_merge import dedupe_people, merge_company_union_candidates  # noqa: E402
from search_common import (  # noqa: E402
    filters_from_role_payload,
    has_role_constraint,
    is_filter_only_payload,
    latest_step_output,
    load_env_file,
    role_payload_from_state,
    search_mode_for_payload,
    strip_is_current_filter,
    summarize_filter,
)


INCLUDE_ATTRIBUTES = [
    "position_title",
    "base_id",
    "city",
    "state",
    "country",
    "macro_region",
    "metro_areas",
    "role_track",
    "seniority_band",
    "company_id",
    "is_current",
]


def search_backend() -> Any:
    if search_backend_mode.is_local_backend_configured():
        import local_search_backend as local_backend  # type: ignore

        return local_backend
    import turbopuffer_search_backend as turbopuffer_client  # type: ignore

    return turbopuffer_client


async def local_summary_rows(payload: dict[str, Any], filters: tuple | None, *, top_k: int, include_attributes: list[str]) -> list[dict[str, Any]]:
    if not search_backend_mode.is_local_backend_configured():
        return []
    from local_search_verticals import summary_search_rows  # type: ignore

    return await summary_search_rows(payload, filters, top_k=top_k, include_attributes=include_attributes)


async def local_company_signal_rows(payload: dict[str, Any], filters: tuple | None, *, top_k: int, include_attributes: list[str]) -> list[dict[str, Any]]:
    if not search_backend_mode.is_local_backend_configured():
        return []
    from local_search_verticals import company_signal_rows  # type: ignore

    return await company_signal_rows(payload, filters, top_k=top_k, include_attributes=include_attributes)


def local_namespace_exists(logical_name: str) -> bool:
    if not search_backend_mode.is_local_backend_configured():
        return False
    import local_search_backend as local_backend  # type: ignore

    return local_backend.local_namespace_exists(logical_name)


def local_namespace_row_count(logical_name: str) -> int:
    if not search_backend_mode.is_local_backend_configured():
        return 0
    import local_search_backend as local_backend  # type: ignore

    return local_backend.local_namespace_row_count(logical_name)


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


def artifact_path(state_path: Path, state: dict[str, Any]) -> Path:
    task_id = state.get("task_id") or state_path.stem
    return state_path.parent / "artifacts" / str(task_id) / "retrieval.json"


def _source_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        for source in candidate.get("vertical_sources") or []:
            counts[str(source)] = counts.get(str(source), 0) + 1
    return counts


def record_step(state_path: Path, state: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> None:
    now = now_iso()
    state.setdefault("steps", []).append({
        "id": "execute_role_search",
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
        "step_id": "execute_role_search",
        "status": "completed",
        "timestamp": now,
        "elapsed_ms": elapsed_ms,
        "returned_people": output.get("returned_people"),
    })


async def run(args: argparse.Namespace) -> dict[str, Any]:
    load_env_file(Path(args.env_file) if args.env_file else None)
    state_path = Path(args.state) if args.state else None
    state = read_json(state_path) if state_path else {}
    payload = json.loads(args.payload_json) if args.payload_json else role_payload_from_state(state)
    backend = search_backend()
    filters = filters_from_role_payload(payload)
    prefilters = latest_step_output(state, "apply_prefilters") if state else {}
    search_mode = search_mode_for_payload(payload)
    verticals: dict[str, dict[str, Any]] = {
        "role": {"status": "pending", "row_count": 0},
        "summary": {"status": "pending", "row_count": 0},
        "company_signal": {"status": "pending", "row_count": 0},
    }
    prefilter_short_circuit = bool(prefilters.get("role_prefilter_ran") and not prefilters.get("base_candidate_ids"))
    if search_mode == "COMPANY_UNION" and not has_role_constraint(payload):
        rows = []
        verticals["role"].update(status="skipped_no_role_constraint")
        verticals["summary"].update(status="skipped_no_role_constraint")
        verticals["company_signal"].update(status="skipped_no_role_constraint")
    elif prefilter_short_circuit:
        rows = []
        verticals["role"].update(status="skipped_empty_prefilter")
        verticals["summary"].update(status="skipped_empty_prefilter")
        verticals["company_signal"].update(status="skipped_empty_prefilter")
    else:
        role_rows = await backend.hybrid_role_rows(payload, filters, top_k=args.top_k, include_attributes=INCLUDE_ATTRIBUTES)
        verticals["role"].update(status="completed", row_count=len(role_rows))
        # Prod's summary vertical is person-level: its eligibility prefilter is
        # built with is_current=None so past positions can qualify a person.
        summary_rows = await local_summary_rows(payload, strip_is_current_filter(filters), top_k=args.top_k, include_attributes=INCLUDE_ATTRIBUTES)
        verticals["summary"].update(status="completed" if summary_rows or local_namespace_exists("summaries") else "skipped_missing_namespace", row_count=len(summary_rows))
        signal_rows = await local_company_signal_rows(payload, filters, top_k=args.top_k, include_attributes=INCLUDE_ATTRIBUTES)
        signal_row_count = local_namespace_row_count("company_signals") if search_backend_mode.is_local_backend_configured() else 0
        if signal_rows:
            signal_status = "completed"
        elif search_backend_mode.is_local_backend_configured() and not local_namespace_exists("company_signals"):
            signal_status = "blocked_missing_local_company_signals"
        elif search_backend_mode.is_local_backend_configured() and signal_row_count == 0:
            signal_status = "unavailable_empty_local_company_signals"
        else:
            signal_status = "completed"
        verticals["company_signal"].update(status=signal_status, row_count=len(signal_rows), namespace_row_count=signal_row_count)
        rows = [*role_rows, *summary_rows, *signal_rows]
    candidates = dedupe_people(rows, limit=args.limit)
    company_union_candidates = prefilters.get("company_union_candidates") or prefilters.get("company_union_candidate_ids") or []
    candidates = merge_company_union_candidates(candidates, company_union_candidates, limit=args.limit)
    retrieval_mode = "filter_only" if is_filter_only_payload(payload) else "hybrid"
    batched_base_ids = any(row.get("retrieval_batched_base_ids") for row in rows)
    union_added = sum(1 for candidate in candidates if "company_filter" in (candidate.get("vertical_sources") or []))
    vertical_source_counts = _source_counts(candidates)

    retrieval_artifact = None
    if state_path and (args.write_state or getattr(args, "write_artifact", False)):
        path = artifact_path(state_path, state)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, {
            "query": state.get("query"),
            "namespace": backend.namespace_name("people"),
            "semantic_query": payload.get("semantic_query"),
            "bm25_queries": payload.get("bm25_queries") or [],
            "applied_filter": summarize_filter(filters),
            "candidate_count": len(candidates),
            "retrieval_mode": retrieval_mode,
            "search_mode": search_mode,
            "verticals": verticals,
            "vertical_source_counts": vertical_source_counts,
            "batched_base_ids": batched_base_ids,
            "base_id_batch_count": rows[0].get("base_id_batch_count") if rows else 0,
            "base_id_batch_size": rows[0].get("base_id_batch_size") if rows else None,
            "prefilter_short_circuit": prefilter_short_circuit,
            "company_union_candidate_count": prefilters.get("company_union_candidate_count") or len(company_union_candidates),
            "company_union_added": union_added,
            "candidates": candidates,
        })
        retrieval_artifact = str(path)

    return {
        "namespace": backend.namespace_name("people"),
        "limit": args.limit,
        "top_k": args.top_k,
        "applied_filter": summarize_filter(filters),
        "search_mode": search_mode,
        "retrieval_mode": retrieval_mode,
        "verticals": verticals,
        "vertical_source_counts": vertical_source_counts,
        "batched_base_ids": batched_base_ids,
        "base_id_batch_count": rows[0].get("base_id_batch_count") if rows else 0,
        "base_id_batch_size": rows[0].get("base_id_batch_size") if rows else None,
        "prefilter_short_circuit": prefilter_short_circuit,
        "company_union_candidate_count": prefilters.get("company_union_candidate_count") or len(company_union_candidates),
        "company_union_added": union_added,
        "returned_people": len(candidates),
        "candidate_ids": [candidate["person_id"] for candidate in candidates],
        "candidates": candidates,
        "retrieval_artifact": retrieval_artifact,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute role search in TurboPuffer")
    parser.add_argument("--state")
    parser.add_argument("--payload-json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--write-artifact", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Max unique people to keep after retrieval; 0 means keep full retrieved frontier")
    parser.add_argument("--top-k", type=int, default=1000)
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
