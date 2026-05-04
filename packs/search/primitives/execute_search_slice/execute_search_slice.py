#!/usr/bin/env python3
"""Execute one generated Powerpacks search slice."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXECUTE_DIR = Path(__file__).resolve().parents[1] / "execute_role_search"
sys.path.insert(0, str(EXECUTE_DIR))

from execute_role_search import run as run_role_search  # noqa: E402


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


def latest_step_output(state: dict[str, Any], step_id: str) -> dict[str, Any]:
    for step in reversed(state.get("steps", [])):
        if step.get("id") == step_id:
            return step.get("output", {}) or {}
    return {}


def slice_from_state(state: dict[str, Any], slice_id: str | None) -> dict[str, Any]:
    generated = latest_step_output(state, "generate_search_slices")
    slices = generated.get("slices") or generated.get("search_slices") or []
    if not slices:
        raise RuntimeError("state does not contain generate_search_slices output")
    if slice_id:
        for item in slices:
            if str(item.get("slice_id")) == slice_id:
                return item
        raise RuntimeError(f"slice not found: {slice_id}")
    return slices[0]


def record_step(state_path: Path, state: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> None:
    now = now_iso()
    state.setdefault("steps", []).append({
        "id": "execute_search_slice",
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
        "step_id": "execute_search_slice",
        "status": "completed",
        "timestamp": now,
        "elapsed_ms": elapsed_ms,
        "slice_id": output.get("slice_id"),
        "returned_people": output.get("returned_people"),
    })


async def run(args: argparse.Namespace) -> dict[str, Any]:
    state_path = Path(args.state) if args.state else None
    state = read_json(state_path) if state_path else {}
    search_slice = json.loads(args.slice_json) if args.slice_json else slice_from_state(state, args.slice_id)
    payload = search_slice.get("role_search_filters")
    if not isinstance(payload, dict):
        raise RuntimeError("slice must contain role_search_filters")
    knobs = search_slice.get("knobs") or {}
    role_args = argparse.Namespace(
        state=args.state,
        payload_json=json.dumps(payload),
        env_file=args.env_file,
        write_state=False,
        write_artifact=args.write_state,
        limit=args.limit or knobs.get("candidate_limit") or 200,
        top_k=args.top_k,
    )
    result = await run_role_search(role_args)
    return {
        "slice_id": search_slice.get("slice_id"),
        "label": search_slice.get("label"),
        "reason": search_slice.get("reason"),
        "knobs": knobs,
        **result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute one generated search slice")
    parser.add_argument("--state")
    parser.add_argument("--slice-json")
    parser.add_argument("--slice-id")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--limit", type=int)
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
