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


LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB_DIR))

from turbopuffer_client import (  # noqa: E402
    dedupe_people,
    filters_from_role_payload,
    hybrid_role_rows,
    load_env_file,
    namespace_name,
    role_payload_from_state,
    summarize_filter,
)


INCLUDE_ATTRIBUTES = [
    "position_title",
    "base_id",
    "city",
    "state",
    "role_track",
    "seniority_band",
    "company_id",
    "is_current",
]


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
    filters = filters_from_role_payload(payload)
    rows = await hybrid_role_rows(payload, filters, top_k=args.top_k, include_attributes=INCLUDE_ATTRIBUTES)
    candidates = dedupe_people(rows, limit=args.limit)

    retrieval_artifact = None
    if state_path and (args.write_state or getattr(args, "write_artifact", False)):
        path = artifact_path(state_path, state)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, {
            "query": state.get("query"),
            "namespace": namespace_name("people"),
            "semantic_query": payload.get("semantic_query"),
            "bm25_queries": payload.get("bm25_queries") or [],
            "applied_filter": summarize_filter(filters),
            "candidate_count": len(candidates),
            "candidates": candidates,
        })
        retrieval_artifact = str(path)

    return {
        "namespace": namespace_name("people"),
        "limit": args.limit,
        "top_k": args.top_k,
        "applied_filter": summarize_filter(filters),
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
    parser.add_argument("--limit", type=int, default=200)
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
