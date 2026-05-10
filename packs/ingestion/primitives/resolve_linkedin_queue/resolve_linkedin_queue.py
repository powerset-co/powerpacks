#!/usr/bin/env python3
"""Resolve queued LinkedIn URLs via shared harness prompt or Parallel.ai.

Input is typically Twitter's linkedin_resolution_queue.csv. Harness mode writes
prompts for Codex/Claude/manual execution. Parallel mode is spend-bearing and
uses approve/continue before submission.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LEDGER = Path(".powerpacks/network-import/linkedin-resolution/import-run.json")
DEFAULT_OUTPUT_DIR = Path(".powerpacks/network-import/linkedin-resolution")
DEFAULT_BASE_URL = os.environ.get("POWERPACKS_PARALLEL_BASE_URL", "https://api.parallel.ai")
DEFAULT_BETA = os.environ.get("POWERPACKS_PARALLEL_BETA", "search-extract-2025-10-10")
DEFAULT_PROCESSOR = os.environ.get("POWERPACKS_PARALLEL_PROCESSOR", "core2x")
ALLOWED_PROCESSORS = {"core", "core2x", "pro"}
PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "linkedin_resolution.md"
OUTPUT_COLUMNS = ["handle", "status", "linkedin_url", "confidence", "matched_name", "matched_headline", "evidence", "reasoning"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in fieldnames})


def prompt_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def candidate_id(row: dict[str, str], idx: int) -> str:
    return (row.get("handle") or row.get("twitter_handle") or row.get("id") or f"row-{idx}").strip()


def candidate_payload(row: dict[str, str], idx: int) -> dict[str, Any]:
    return {
        "handle": candidate_id(row, idx),
        "display_name": row.get("display_name") or row.get("full_name") or "",
        "bio": row.get("bio") or row.get("headline") or "",
        "location": row.get("location") or row.get("location_raw") or "",
        "website_url": row.get("website_url") or "",
        "twitter_handle": row.get("handle") or row.get("twitter_handle") or "",
        "source": row.get("source") or row.get("source_channels") or "",
        "known_info": {
            "follower_count": row.get("follower_count") or "",
            "moe_verdict": row.get("moe_verdict") or "",
            "moe_reasoning": row.get("moe_top_reasoning") or "",
            "primary_email": row.get("primary_email") or "",
            "primary_phone": row.get("primary_phone") or "",
        },
    }


def task_spec() -> dict[str, Any]:
    return {
        "instructions": prompt_text(),
        "input_schema": {"json_schema": {"type": "object", "additionalProperties": True}},
        "output_schema": {"json_schema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "status": {"type": "string", "enum": ["found", "not_found", "ambiguous"]},
                "linkedin_url": {"type": ["string", "null"]},
                "confidence": {"type": "number"},
                "matched_name": {"type": ["string", "null"]},
                "matched_headline": {"type": ["string", "null"]},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "reasoning": {"type": "string"},
            },
            "required": ["handle", "status", "confidence", "reasoning"],
        }},
    }


class ParallelClient:
    def __init__(self, api_key: str, base_url: str, beta: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.beta = beta

    def request(self, method: str, path: str, body: Any = None, timeout: int = 60) -> tuple[int, Any, str]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"x-api-key": self.api_key, "parallel-beta": self.beta, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, json.loads(raw) if raw else None, ""
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw) if raw else None, raw
            except json.JSONDecodeError:
                return exc.code, None, raw

    def create_group(self) -> dict[str, Any]:
        status, body, raw = self.request("POST", "/v1beta/tasks/groups", {"metadata": {"source": "powerpacks-linkedin-resolution", "submitted_at": now_iso()}})
        if status not in (200, 201) or not isinstance(body, dict):
            raise RuntimeError(f"create_group failed HTTP {status}: {raw[:200]}")
        return body

    def add_runs(self, group_id: str, inputs: list[dict[str, Any]]) -> dict[str, Any]:
        status, body, raw = self.request("POST", f"/v1beta/tasks/groups/{group_id}/runs", {"inputs": inputs})
        if status not in (200, 201) or not isinstance(body, dict):
            raise RuntimeError(f"add_runs failed HTTP {status}: {raw[:200]}")
        return body

    def get_group(self, group_id: str) -> dict[str, Any]:
        status, body, raw = self.request("GET", f"/v1beta/tasks/groups/{group_id}")
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"get_group failed HTTP {status}: {raw[:200]}")
        return body

    def get_result(self, run_id: str) -> dict[str, Any] | None:
        path = f"/v1/tasks/runs/{run_id}/result?" + urllib.parse.urlencode({"beta": "true", "api_timeout": 60})
        status, body, raw = self.request("GET", path, timeout=75)
        if status == 404:
            return None
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"get_result failed HTTP {status}: {raw[:200]}")
        return body


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = now_iso()
    write_json(path, ledger)


def block(path: Path, ledger: dict[str, Any]) -> None:
    ledger["blocked"] = {"step": "parallel_submit", "approval_type": "external_api_spend"}
    save_ledger(path, ledger)
    emit({"status": "blocked_approval", "approval_type": "external_api_spend", "message": "Approve Parallel.ai LinkedIn resolution spend?", "ledger": str(path), "continue_command": f"uv run --project . python packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py approve --ledger {path} && uv run --project . python packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py continue --ledger {path}"})


def write_harness(output_dir: Path, rows: list[dict[str, str]]) -> dict[str, Any]:
    payloads = [candidate_payload(row, i) for i, row in enumerate(rows)]
    prompts = output_dir / "harness_prompts.jsonl"
    instructions = output_dir / "instructions.md"
    output_dir.mkdir(parents=True, exist_ok=True)
    instructions.write_text(prompt_text() + "\n\nWrite results as JSON Lines to `linkedin_resolutions.csv` using the documented schema.\n", encoding="utf-8")
    with prompts.open("w", encoding="utf-8") as handle:
        for payload in payloads:
            handle.write(json.dumps({"instructions": prompt_text(), "input": payload}, ensure_ascii=False) + "\n")
    return {"mode": "harness", "rows": len(rows), "instructions": str(instructions), "prompts_jsonl": str(prompts)}


def submit_parallel(ledger_path: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("PARALLEL_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("PARALLEL_API_KEY is not set")
    processor = ledger["input"].get("processor") or DEFAULT_PROCESSOR
    if processor not in ALLOWED_PROCESSORS:
        raise SystemExit(f"processor must be one of {sorted(ALLOWED_PROCESSORS)}")
    rows = read_csv(Path(ledger["input"]["input_csv"]))
    if ledger["input"].get("limit"):
        rows = rows[: int(ledger["input"]["limit"])]
    client = ParallelClient(api_key, ledger["input"].get("base_url") or DEFAULT_BASE_URL, ledger["input"].get("beta") or DEFAULT_BETA)
    group = client.create_group()
    group_id = group.get("taskgroup_id") or group.get("id")
    inputs = [{"task_spec": task_spec(), "input": candidate_payload(row, i), "metadata": {"handle": candidate_id(row, i)}, "processor": processor} for i, row in enumerate(rows)]
    run_ids: list[str] = []
    for i in range(0, len(inputs), 50):
        resp = client.add_runs(group_id, inputs[i:i + 50])
        run_ids.extend(resp.get("run_ids") or [])
    ledger["parallel"] = {"taskgroup_id": group_id, "run_ids": run_ids, "submitted_at": now_iso(), "rows": rows}
    ledger.pop("blocked", None)
    save_ledger(ledger_path, ledger)
    return {"status": "submitted", "taskgroup_id": group_id, "submitted": len(run_ids)}


def poll_parallel(ledger_path: Path, ledger: dict[str, Any], wait: bool) -> dict[str, Any]:
    api_key = os.getenv("PARALLEL_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("PARALLEL_API_KEY is not set")
    client = ParallelClient(api_key, ledger["input"].get("base_url") or DEFAULT_BASE_URL, ledger["input"].get("beta") or DEFAULT_BETA)
    group_id = ledger.get("parallel", {}).get("taskgroup_id")
    if not group_id:
        return {"status": "missing_taskgroup"}
    while True:
        group = client.get_group(group_id)
        if not wait or group.get("status", {}).get("is_active") is False:
            break
        time.sleep(10)
    results: list[dict[str, Any]] = []
    if group.get("status", {}).get("is_active") is False:
        for run_id in ledger.get("parallel", {}).get("run_ids", []):
            result = client.get_result(run_id) or {}
            content = result.get("output", {}).get("content") if isinstance(result.get("output"), dict) else result.get("content")
            if isinstance(content, dict):
                results.append(content)
    output = Path(ledger["output_dir"]) / "linkedin_resolutions.csv"
    if results:
        for row in results:
            if isinstance(row.get("evidence"), list):
                row["evidence"] = json.dumps(row["evidence"], ensure_ascii=False)
        write_csv(output, OUTPUT_COLUMNS, results)
    return {"status": "completed" if results else "pending", "taskgroup_id": group_id, "results": len(results), "output": str(output), "group": group}


def cmd_run(args: argparse.Namespace) -> int:
    rows = read_csv(Path(args.input))
    if args.limit is not None:
        rows = rows[: args.limit]
    output_dir = Path(args.output_dir)
    ledger = {"primitive": "resolve_linkedin_queue", "created_at": now_iso(), "updated_at": now_iso(), "output_dir": str(output_dir), "input": {"input_csv": str(args.input), "provider": args.provider, "processor": args.processor, "limit": args.limit, "base_url": args.base_url, "beta": args.beta}}
    if args.provider == "harness":
        result = write_harness(output_dir, rows)
        ledger["status"] = "prepared_harness"
        ledger["artifacts"] = result
        save_ledger(Path(args.ledger), ledger)
        emit({"status": "prepared_harness", "ledger": args.ledger, **result})
        return 0
    ledger["status"] = "blocked_approval"
    save_ledger(Path(args.ledger), ledger)
    block(Path(args.ledger), ledger)
    return 20


def cmd_approve(args: argparse.Namespace) -> int:
    ledger = read_json(Path(args.ledger), {})
    ledger["approved_at"] = now_iso()
    ledger.pop("blocked", None)
    save_ledger(Path(args.ledger), ledger)
    emit({"status": "approved", "ledger": args.ledger})
    return 0


def cmd_continue(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    ledger = read_json(ledger_path, {})
    if ledger.get("blocked"):
        block(ledger_path, ledger)
        return 20
    if not ledger.get("parallel"):
        emit(submit_parallel(ledger_path, ledger))
        return 0
    emit(poll_parallel(ledger_path, ledger, args.wait))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    emit(read_json(Path(args.ledger), {"status": "missing", "ledger": args.ledger}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve LinkedIn URLs for queued candidates via harness prompt or Parallel.ai")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--input", required=True)
    run.add_argument("--provider", choices=["harness", "parallel"], default="harness")
    run.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    run.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    run.add_argument("--processor", default=DEFAULT_PROCESSOR, choices=sorted(ALLOWED_PROCESSORS))
    run.add_argument("--base-url", default=DEFAULT_BASE_URL)
    run.add_argument("--beta", default=DEFAULT_BETA)
    run.add_argument("--limit", type=int, help=argparse.SUPPRESS)
    run.set_defaults(func=cmd_run)
    approve = sub.add_parser("approve")
    approve.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    approve.set_defaults(func=cmd_approve)
    cont = sub.add_parser("continue")
    cont.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    cont.add_argument("--wait", action="store_true")
    cont.set_defaults(func=cmd_continue)
    status = sub.add_parser("status")
    status.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
