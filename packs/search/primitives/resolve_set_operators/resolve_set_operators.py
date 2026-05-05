#!/usr/bin/env python3
"""Resolve a Powerset set_id to TurboPuffer allowed operator IDs."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB_DIR))

from postgres_client import fetch_set_operator_ids, load_env_file  # noqa: E402
from turbopuffer_client import latest_step_output  # noqa: E402


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


def role_payload_from_state(state: dict[str, Any]) -> dict[str, Any]:
    expansion = latest_step_output(state, "expand_search_request")
    payload = expansion.get("role_search_filters") if isinstance(expansion, dict) else None
    return dict(payload) if isinstance(payload, dict) else {}


def requested_set_id(args: argparse.Namespace, state: dict[str, Any]) -> str | None:
    if args.set_id:
        return args.set_id
    if args.payload_json:
        payload = json.loads(args.payload_json)
        value = payload.get("set_id")
        return str(value) if value else None
    payload = role_payload_from_state(state)
    value = payload.get("set_id")
    return str(value) if value else None


def record_step(state_path: Path, state: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> None:
    now = now_iso()
    state.setdefault("steps", []).append({
        "id": "resolve_set_operators",
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
        "step_id": "resolve_set_operators",
        "status": "completed",
        "timestamp": now,
        "elapsed_ms": elapsed_ms,
        "set_id": output.get("set_id"),
        "operator_count": output.get("operator_count"),
    })


def run(args: argparse.Namespace) -> dict[str, Any]:
    env_file = Path(args.env_file) if args.env_file else None
    load_env_file(env_file)
    state_path = Path(args.state) if args.state else None
    state = read_json(state_path) if state_path and state_path.exists() else {}
    set_id = requested_set_id(args, state)
    output = fetch_set_operator_ids(
        set_id=set_id,
        env_file=env_file,
        credentials_path=Path(args.credentials) if args.credentials else None,
    )
    return {
        "primitive": "resolve_set_operators",
        **output,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve a Powerset set_id to operator IDs")
    parser.add_argument("--set-id", help="Powerset set UUID. Defaults to payload/state set_id, then POWERPACKS_DEFAULT_SET_ID/POWERSET_DEFAULT_SET_ID, then the logged-in user's personal set.")
    parser.add_argument("--state", help="Search task state containing expand_search_request.role_search_filters.set_id")
    parser.add_argument("--payload-json", help="Role-search payload containing set_id")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--credentials", help="Path to ~/.powerpacks/credentials.json for personal default-set fallback")
    parser.add_argument("--write-state", action="store_true")
    args = parser.parse_args()

    started = time.time()
    output = run(args)
    elapsed_ms = int((time.time() - started) * 1000)
    if args.write_state:
        if not args.state:
            raise SystemExit("--write-state requires --state")
        state_path = Path(args.state)
        record_step(state_path, read_json(state_path), output, elapsed_ms)
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
