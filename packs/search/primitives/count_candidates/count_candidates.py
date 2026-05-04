#!/usr/bin/env python3
"""Count Powerpacks role-search candidates in TurboPuffer."""

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
    base_person_id,
    filter_only_rows,
    filters_from_role_payload,
    load_env_file,
    namespace_name,
    role_payload_from_state,
    summarize_filter,
)


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


def record_step(state_path: Path, state: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> None:
    now = now_iso()
    state.setdefault("steps", []).append({
        "id": "count_candidates",
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
        "step_id": "count_candidates",
        "status": "completed",
        "timestamp": now,
        "elapsed_ms": elapsed_ms,
        "unique_people": output.get("unique_people"),
        "position_rows": output.get("position_rows"),
    })


async def run(args: argparse.Namespace) -> dict[str, Any]:
    load_env_file(Path(args.env_file) if args.env_file else None)
    state_path = Path(args.state) if args.state else None
    state = read_json(state_path) if state_path else {}
    payload = json.loads(args.payload_json) if args.payload_json else role_payload_from_state(state)
    filters = filters_from_role_payload(payload)
    if filters is None:
        raise RuntimeError("count_candidates requires at least one filter")
    rows = await filter_only_rows(filters, ["base_id", "position_title"], page_size=args.page_size, max_results=args.max_position_rows)
    unique_people = {str(row.get("base_id") or base_person_id(str(row["id"]))) for row in rows}
    return {
        "namespace": namespace_name("people"),
        "filters": payload.get("hard_filters") or {},
        "applied_filter": summarize_filter(filters),
        "position_rows": len(rows),
        "unique_people": len(unique_people),
        "truncated": bool(args.max_position_rows and len(rows) >= args.max_position_rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Count role-search candidates in TurboPuffer")
    parser.add_argument("--state")
    parser.add_argument("--payload-json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--page-size", type=int, default=10000)
    parser.add_argument("--max-position-rows", type=int, default=0)
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
