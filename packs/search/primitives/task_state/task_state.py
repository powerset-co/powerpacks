#!/usr/bin/env python3
"""Small JSON task-state helper for Powerpacks search runs."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


LINEAGE_CONFIG = {
    "search_plan_revision": ("search_plan_revisions", "revision_id"),
    "candidate_feedback": ("candidate_feedback", "feedback_id"),
    "criteria_mutation": ("criteria_mutations", "mutation_id"),
    "run_lineage": ("run_lineage", "lineage_id"),
    "exemplar_set": ("exemplar_sets", "exemplar_set_id"),
    "fanout_thread": ("fanout_threads", "thread_id"),
}


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


def event_log_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".events.jsonl")


def append_event(path: Path, event: dict[str, Any]) -> None:
    log_path = event_log_path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.open("a").write(json.dumps(event, sort_keys=True) + "\n")


def slugify(value: str, max_length: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug[:max_length].strip("-") or "query")


def init_output_path(args: argparse.Namespace, task_id: str) -> Path:
    if args.out:
        return Path(args.out)
    out_dir = Path(args.out_dir)
    return out_dir / f"{task_id}-{slugify(args.query)}.json"


def normalize_planned_step(item: Any, now: str) -> dict[str, Any]:
    if isinstance(item, str):
        return {
            "id": item,
            "status": "pending",
            "planned_at": now,
        }
    if not isinstance(item, dict):
        raise TypeError(f"planned step must be a string or object, got {type(item).__name__}")
    step_id = item.get("id") or item.get("step_id")
    if not step_id:
        raise ValueError("planned step object requires id")
    step = dict(item)
    step["id"] = str(step_id)
    step.pop("step_id", None)
    step.setdefault("status", "pending")
    step.setdefault("planned_at", now)
    return step


def planned_steps_from_plan(plan: Any, now: str) -> list[dict[str, Any]]:
    if isinstance(plan, list):
        raw_steps = plan
    elif isinstance(plan, dict):
        raw_steps = plan.get("planned_steps")
        if raw_steps is None:
            raw_steps = plan.get("steps")
    else:
        raise TypeError("approval plan must be an object or a planned steps array")
    if not raw_steps:
        return []
    if not isinstance(raw_steps, list):
        raise TypeError("approval plan planned_steps must be an array")
    return [normalize_planned_step(item, now) for item in raw_steps]


def update_planned_step(state: dict[str, Any], step_id: str, status: str, now: str, note: str | None = None) -> bool:
    planned_steps = state.get("planned_steps")
    if not isinstance(planned_steps, list):
        return False
    updated = False
    for planned in planned_steps:
        if not isinstance(planned, dict) or planned.get("id") != step_id:
            continue
        planned["status"] = status
        if status == "running":
            planned.setdefault("started_at", now)
        elif status in {"completed", "failed", "skipped"}:
            planned.setdefault("started_at", now)
            planned["completed_at"] = now
        else:
            planned["updated_at"] = now
        if note:
            planned["note"] = note
        updated = True
    return updated


def cmd_init(args: argparse.Namespace) -> None:
    task_id = args.task_id or f"search-network-{uuid4()}"
    out_path = init_output_path(args, task_id)
    now = now_iso()
    state = {
        "task_id": task_id,
        "task": "search_network",
        "status": "running",
        "query": args.query,
        "created_at": now,
        "updated_at": now,
        "constraints": {
            "no_llm_enrichment": True,
            "no_summary_search": True,
            "no_company_signal_search": True,
            "no_expensive_scoring": True,
        },
        "plan": load_json_arg(args.plan_json) or {},
        "steps": [],
        **{field: [] for field, _id_field in LINEAGE_CONFIG.values()},
    }
    write_state(out_path, state)
    append_event(out_path, {
        "event": "init",
        "task_id": task_id,
        "state": str(out_path),
        "status": "running",
        "timestamp": now,
    })
    print(json.dumps({"task_id": task_id, "state": str(out_path), "event_log": str(event_log_path(out_path))}, indent=2))


def cmd_record_step(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = read_state(path)
    now = now_iso()
    step = {
        "id": args.step_id,
        "status": args.status,
        "recorded_at": now,
    }
    output = load_json_arg(args.output_json)
    if output is not None:
        step["output"] = output
    if args.note:
        step["note"] = args.note
    if args.elapsed_ms is not None:
        step["elapsed_ms"] = args.elapsed_ms

    state.setdefault("steps", []).append(step)
    planned_step_updated = update_planned_step(state, args.step_id, args.status, now, args.note)
    state["updated_at"] = now
    write_state(path, state)
    append_event(path, {
        "event": "record_step",
        "task_id": state.get("task_id"),
        "state": str(path),
        "step_id": args.step_id,
        "status": args.status,
        "timestamp": now,
        "elapsed_ms": args.elapsed_ms,
        "planned_step_updated": planned_step_updated,
    })
    print(json.dumps({
        "state": str(path),
        "event_log": str(event_log_path(path)),
        "recorded_step": args.step_id,
        "planned_step_updated": planned_step_updated,
    }, indent=2))


def cmd_set_summary(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = read_state(path)
    now = now_iso()
    state["summary"] = load_json_arg(args.summary_json) or {}
    state["status"] = args.status
    state["updated_at"] = now
    write_state(path, state)
    append_event(path, {
        "event": "set_summary",
        "task_id": state.get("task_id"),
        "state": str(path),
        "status": args.status,
        "timestamp": now,
        "step_count": len(state.get("steps", [])),
    })
    print(json.dumps({"state": str(path), "event_log": str(event_log_path(path)), "status": args.status}, indent=2))


def cmd_request_approval(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = read_state(path)
    now = now_iso()
    plan = load_json_arg(args.plan_json) or {}
    approval = {
        "status": "pending",
        "requested_at": now,
        "reason": args.reason,
        "proposed_next_step": args.proposed_next_step,
        "plan": plan,
    }
    state["approval"] = approval
    planned_steps = planned_steps_from_plan(plan, now)
    if planned_steps:
        state["planned_steps"] = planned_steps
    state["status"] = "awaiting_approval"
    state["updated_at"] = now
    write_state(path, state)
    append_event(path, {
        "event": "request_approval",
        "task_id": state.get("task_id"),
        "state": str(path),
        "timestamp": now,
        "reason": args.reason,
        "proposed_next_step": args.proposed_next_step,
        "planned_step_count": len(planned_steps),
    })
    print(json.dumps({
        "state": str(path),
        "event_log": str(event_log_path(path)),
        "status": "awaiting_approval",
        "planned_step_count": len(planned_steps),
    }, indent=2))


def cmd_approve(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = read_state(path)
    now = now_iso()
    approval = state.get("approval") if isinstance(state.get("approval"), dict) else {}
    approval.update({
        "status": "approved",
        "approved_at": now,
        "approved_by": args.approved_by,
        "execution_mode": args.execution_mode,
        "note": args.note,
    })
    state["approval"] = approval
    state["status"] = "running"
    state["updated_at"] = now
    write_state(path, state)
    append_event(path, {
        "event": "approve",
        "task_id": state.get("task_id"),
        "state": str(path),
        "timestamp": now,
        "approved_by": args.approved_by,
        "execution_mode": args.execution_mode,
        "note": args.note,
    })
    print(json.dumps({"state": str(path), "event_log": str(event_log_path(path)), "status": "running"}, indent=2))


def cmd_request_changes(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = read_state(path)
    now = now_iso()
    approval = state.get("approval") if isinstance(state.get("approval"), dict) else {}
    approval.update({
        "status": "changes_requested",
        "changed_at": now,
        "requested_by": args.requested_by,
        "note": args.note,
    })
    state["approval"] = approval
    state["status"] = "paused"
    state["updated_at"] = now
    write_state(path, state)
    append_event(path, {
        "event": "request_changes",
        "task_id": state.get("task_id"),
        "state": str(path),
        "timestamp": now,
        "requested_by": args.requested_by,
        "note": args.note,
    })
    print(json.dumps({"state": str(path), "event_log": str(event_log_path(path)), "status": "paused"}, indent=2))


def cmd_append_lineage(args: argparse.Namespace) -> None:
    path = Path(args.state)
    state = read_state(path)
    now = now_iso()
    payload = load_json_arg(args.payload_json) or {}
    if not isinstance(payload, dict):
        raise TypeError("lineage payload must be a JSON object")
    field, id_field = LINEAGE_CONFIG[args.kind]
    item = dict(payload)
    item.setdefault(id_field, f"{args.kind}-{uuid4()}")
    if args.kind == "search_plan_revision":
        item.setdefault("revision", len(state.get(field, [])) + 1)
    item.setdefault("recorded_at", now)
    item.setdefault("source", args.source)
    state.setdefault(field, []).append(item)
    state["updated_at"] = now
    write_state(path, state)
    append_event(path, {
        "event": "append_lineage",
        "task_id": state.get("task_id"),
        "state": str(path),
        "timestamp": now,
        "kind": args.kind,
        "field": field,
        "count": len(state.get(field, [])),
        "source": args.source,
        id_field: item.get(id_field),
    })
    print(json.dumps({
        "state": str(path),
        "event_log": str(event_log_path(path)),
        "kind": args.kind,
        "field": field,
        "count": len(state.get(field, [])),
        id_field: item.get(id_field),
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage a Powerpacks JSON task run")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--query", required=True)
    init.add_argument("--out")
    init.add_argument("--out-dir", default=".powerpacks/runs")
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

    approval = sub.add_parser("request-approval")
    approval.add_argument("--state", required=True)
    approval.add_argument("--reason", required=True)
    approval.add_argument("--proposed-next-step", required=True)
    approval.add_argument("--plan-json")
    approval.set_defaults(func=cmd_request_approval)

    approve = sub.add_parser("approve")
    approve.add_argument("--state", required=True)
    approve.add_argument("--approved-by", default="user")
    approve.add_argument("--note", default="")
    approve.add_argument("--execution-mode", choices=["search_only", "rerank"], default="search_only")
    approve.set_defaults(func=cmd_approve)

    changes = sub.add_parser("request-changes")
    changes.add_argument("--state", required=True)
    changes.add_argument("--requested-by", default="user")
    changes.add_argument("--note", required=True)
    changes.set_defaults(func=cmd_request_changes)

    lineage = sub.add_parser("append-lineage")
    lineage.add_argument("--state", required=True)
    lineage.add_argument("--kind", required=True, choices=sorted(LINEAGE_CONFIG))
    lineage.add_argument("--payload-json", required=True)
    lineage.add_argument("--source", default="user")
    lineage.set_defaults(func=cmd_append_lineage)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
