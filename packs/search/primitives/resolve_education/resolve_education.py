#!/usr/bin/env python3
"""Resolve education names to canonical school IDs in TurboPuffer."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB_DIR))

from turbopuffer_client import (  # noqa: E402
    STRONG_CONSISTENCY,
    load_env_file,
    namespace,
    namespace_name,
    role_payload_from_state,
    row_attrs,
)

TOKEN_RE = re.compile(r"[a-z0-9]+")
ROOT_SCHOOL_SUFFIXES = {"university", "college", "institute"}


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


def education_names(payload: dict[str, Any]) -> list[str]:
    names = payload.get("education_names") or payload.get("school_names") or []
    return list(dict.fromkeys(str(name).strip() for name in names if str(name).strip()))


def school_tokens(name: str) -> list[str]:
    return TOKEN_RE.findall(str(name or "").lower())


def affiliated_school_queries(name: str) -> list[str]:
    """Return broader school-family queries for root names like Stanford University."""
    tokens = school_tokens(name)
    if len(tokens) == 2 and tokens[1] in ROOT_SCHOOL_SUFFIXES and len(tokens[0]) >= 4:
        return [tokens[0]]
    return []


def is_affiliated_candidate(root_queries: list[str], school_name: str) -> bool:
    if not root_queries:
        return False
    tokens = school_tokens(school_name)
    return bool(tokens and tokens[0] in set(root_queries))


async def query_school_rows(query: str, *, limit: int) -> list[dict[str, Any]]:
    filters = ("school_name", "ContainsAllTokens", query, {"last_as_prefix": True})
    ns = namespace("schools")

    def run_query() -> Any:
        return ns.query(
            filters=filters,
            top_k=limit,
            include_attributes=["school_name", "person_count"],
            consistency=STRONG_CONSISTENCY,
        )

    response = await asyncio.to_thread(run_query)
    return [row_attrs(row, ["school_name", "person_count"]) for row in (response.rows or [])]


async def resolve_name(name: str, *, limit: int) -> dict[str, Any]:
    root_queries = affiliated_school_queries(name)
    query_rows: list[tuple[str, dict[str, Any]]] = []
    for query in [name, *root_queries]:
        query_rows.extend((query, row) for row in await query_school_rows(query, limit=limit))

    counts: Counter[str] = Counter()
    names_by_id: dict[str, Counter[str]] = {}
    family_ids: set[str] = set()
    for query, row in query_rows:
        school_id = row.get("id")
        if not school_id:
            continue
        school_id = str(school_id)
        counts[school_id] = max(counts[school_id], int(row.get("person_count") or 1))
        school_name = str(row.get("school_name") or school_id)
        names_by_id.setdefault(school_id, Counter())[school_name] += 1
        if query in root_queries or is_affiliated_candidate(root_queries, school_name):
            family_ids.add(school_id)

    candidates = []
    for school_id, count in counts.most_common(10):
        display_name = names_by_id[school_id].most_common(1)[0][0]
        include = school_id in family_ids or (not candidates and not root_queries)
        candidates.append({
            "id": school_id,
            "name": display_name,
            "matched_rows": count,
            "included": include,
            "match_family": bool(root_queries and school_id in family_ids),
        })
    resolved_ids = [candidate["id"] for candidate in candidates if candidate.get("included")]
    if not resolved_ids and candidates:
        resolved_ids = [candidates[0]["id"]]
    return {
        "query": name,
        "candidates": candidates,
        "resolved_ids": list(dict.fromkeys(resolved_ids)),
        "affiliated_queries": root_queries,
    }


def record_step(state_path: Path, state: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> None:
    now = now_iso()
    state.setdefault("steps", []).append({
        "id": "resolve_education",
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
        "step_id": "resolve_education",
        "status": "completed",
        "timestamp": now,
        "elapsed_ms": elapsed_ms,
        "resolved_count": len(output.get("education_ids") or []),
    })


async def run(args: argparse.Namespace) -> dict[str, Any]:
    load_env_file(Path(args.env_file) if args.env_file else None)
    state_path = Path(args.state) if args.state else None
    state = read_json(state_path) if state_path else {}
    payload = json.loads(args.payload_json) if args.payload_json else role_payload_from_state(state)

    existing_ids = [str(value) for value in payload.get("education_ids") or [] if value]
    names = education_names(payload)
    resolutions = [await resolve_name(name, limit=args.max_rows_per_name) for name in names]

    resolved_ids = []
    for resolution in resolutions:
        resolved_ids.extend(str(value) for value in resolution.get("resolved_ids") or [] if value)

    education_ids = list(dict.fromkeys([*existing_ids, *resolved_ids]))
    return {
        "namespace": namespace_name("schools"),
        "education_names": names,
        "education_ids": education_ids,
        "resolutions": resolutions,
        "unresolved_names": [
            resolution["query"]
            for resolution in resolutions
            if not resolution["candidates"]
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve education names to canonical IDs")
    parser.add_argument("--state")
    parser.add_argument("--payload-json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--max-rows-per-name", type=int, default=5000)
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
