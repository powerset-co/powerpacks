#!/usr/bin/env python3
"""Small JSON task-state helper for Powerpacks search runs."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json_arg(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)


def read_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def cmd_init(args: argparse.Namespace) -> None:
    task_id = args.task_id or f"search-network-{uuid4()}"
    state = {
        "task_id": task_id,
        "task": "search_network",
        "status": "running",
        "query": args.query,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "constraints": {
            "no_llm_enrichment": True,
            "no_summary_search": True,
            "no_company_signal_search": True,
            "no_expensive_scoring": True,
        },
        "plan": load_json_arg(args.plan_json) or {},
        "steps": [],
    }
    write_state(Path(args.out), state)
    print(json.dumps({"task_id": task_id, "state": args.out}, indent=2))


def cmd_record_step(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = read_state(path)
    step = {
        "id": args.step_id,
        "status": args.status,
        "recorded_at": now_iso(),
    }
    output = load_json_arg(args.output_json)
    if output is not None:
        step["output"] = output
    if args.note:
        step["note"] = args.note
    if args.elapsed_ms is not None:
        step["elapsed_ms"] = args.elapsed_ms

    state.setdefault("steps", []).append(step)
    state["updated_at"] = now_iso()
    write_state(path, state)
    print(json.dumps({"state": str(path), "recorded_step": args.step_id}, indent=2))


def cmd_set_summary(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = read_state(path)
    state["summary"] = load_json_arg(args.summary_json) or {}
    state["status"] = args.status
    state["updated_at"] = now_iso()
    write_state(path, state)
    print(json.dumps({"state": str(path), "status": args.status}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage a Powerpacks JSON task run")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--query", required=True)
    init.add_argument("--out", required=True)
    init.add_argument("--task-id")
    init.add_argument("--plan-json")
    init.set_defaults(func=cmd_init)

    record = sub.add_parser("record-step")
    record.add_argument("--state", required=True)
    record.add_argument("--step-id", required=True)
    record.add_argument("--status", default="completed")
    record.add_argument("--output-json")
    record.add_argument("--elapsed-ms", type=int)
    record.add_argument("--note")
    record.set_defaults(func=cmd_record_step)

    summary = sub.add_parser("set-summary")
    summary.add_argument("--state", required=True)
    summary.add_argument("--summary-json", required=True)
    summary.add_argument("--status", default="completed")
    summary.set_defaults(func=cmd_set_summary)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
