#!/usr/bin/env python3
"""Run the Gmail vertical end-to-end without the legacy setup.py orchestrator.

This primitive mirrors setup_linkedin_csv but for Gmail. It links Gmail
accounts from accounts.json, syncs message metadata, discovers contacts,
runs import/enrichment (auto-approving Parallel.ai spend so the single
onboarding-v2 button completes in one shot), then reuses the shared indexing
wrapper. Progress is written to stdout and to a single overwritten
status.json/events.jsonl under .powerpacks/runs/setup-gmail.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.ingestion.primitives.discover_contacts_pipeline import gmail as gmail_discovery  # noqa: E402
from packs.ingestion.primitives.discover_contacts_pipeline.common import read_json  # noqa: E402
from packs.ingestion.primitives.import_contacts_pipeline import gmail as gmail_import  # noqa: E402
from packs.ingestion.primitives.import_contacts_pipeline.common import (  # noqa: E402
    DEFAULT_ACCOUNTS,
    DEFAULT_BASE_DIR,
    DEFAULT_IMPORT_DIR,
    import_manifest_current,
    linked_gmail_accounts,
)
from packs.indexing.primitives.index_contacts_pipeline import index_contacts_pipeline  # noqa: E402

VERTICAL = "gmail"
RUN_ROOT = Path(".powerpacks/runs/setup-gmail")
IMPORT_SOURCE = "gmail"
IMPORT_DIR = DEFAULT_IMPORT_DIR / IMPORT_SOURCE
DISCOVER_DIR = DEFAULT_BASE_DIR / "discover" / "gmail"
DISCOVER_CONTACTS_CSV = DISCOVER_DIR / "contacts.csv"

# Gmail enrichment resolves unmatched contacts to LinkedIn via Parallel.ai. The
# resolution queue uses the core2x processor by default (see resolve_linkedin_queue
# DEFAULT_PROCESSOR), priced at $0.05 per lookup (see PROCESSOR_PRICING_USD in
# deep_research_contacts). The estimate is simply pending queue rows * per-lookup.
GMAIL_PARALLEL_PROCESSOR = "core2x"
GMAIL_PARALLEL_COST_PER_CONTACT_USD = 0.05
STAGES = [
    {"id": "inspect", "label": "Check linked Gmail accounts"},
    {"id": "discover", "label": "Sync and discover Gmail contacts"},
    {"id": "enrich", "label": "Enrich Gmail contacts"},
    {"id": "source_people", "label": "Save Gmail people file"},
    {"id": "merge_network", "label": "Merge contact sources"},
    {"id": "network_duckdb", "label": "Prepare contact lookup database"},
    {"id": "index_estimate", "label": "Estimate search updates"},
    {"id": "index_records", "label": "Build searchable people records"},
    {"id": "search_duckdb", "label": "Update local search database"},
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def valid_run_id(run_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_:-]{0,127}", run_id))


@dataclass
class RunContext:
    run_id: str
    state_path: Path
    events_path: Path
    status: dict[str, Any]

    def update(self, **fields: Any) -> None:
        self.status.update(fields)
        self.status["updated_at"] = now_iso()
        atomic_write_json(self.state_path, self.status)

    def event(self, stage_id: str, message: str, *, status: str = "running", progress: float | None = None, payload: dict[str, Any] | None = None) -> None:
        stage_index = next((idx + 1 for idx, stage in enumerate(STAGES) if stage["id"] == stage_id), 0)
        stage_label = next((stage["label"] for stage in STAGES if stage["id"] == stage_id), stage_id)
        if progress is None:
            if status == "completed":
                progress = stage_index / len(STAGES) if stage_index else 0
            else:
                progress = max(stage_index - 1, 0) / len(STAGES) if stage_index else 0
        event = {
            "run_id": self.run_id,
            "vertical": VERTICAL,
            "status": status,
            "stage": stage_id,
            "stage_label": stage_label,
            "stage_index": stage_index,
            "stage_total": len(STAGES),
            "message": message,
            "progress": progress,
            "updated_at": now_iso(),
            "payload": payload or {},
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        print(f"setup: {stage_label} ({stage_index}/{len(STAGES)})", flush=True)
        emit_json({"event": "progress", **event})
        stages = dict(self.status.get("stages") or {})
        stages[stage_id] = {"status": status, "label": stage_label, "message": message, "updated_at": event["updated_at"], "payload": payload or {}}
        if status in {"failed", "blocked_approval"}:
            overall_status = status
        elif status == "completed" and stage_index == len(STAGES):
            overall_status = "completed"
        else:
            overall_status = "running"
        self.update(status=overall_status, current_stage=stage_id, progress=event["progress"], stages=stages)


def make_context(run_id: str | None = None) -> RunContext:
    rid = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    if not valid_run_id(rid):
        raise ValueError("run_id may only contain letters, numbers, underscore, dash, and colon")
    status = {
        "schema_version": 1,
        "vertical": VERTICAL,
        "run_id": rid,
        "status": "running",
        "started_at": now_iso(),
        "updated_at": now_iso(),
        "progress": 0,
        "stages": {},
        "stage_order": STAGES,
    }
    # Single status/events file per vertical, overwritten when a new run starts.
    state_path = RUN_ROOT / "status.json"
    events_path = RUN_ROOT / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text("", encoding="utf-8")
    ctx = RunContext(rid, state_path, events_path, status)
    ctx.update()
    return ctx


def args_for_index(operator_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        cmd="run",
        operator_id=operator_id,
        accounts=str(DEFAULT_ACCOUNTS),
        people_csv=str(index_contacts_pipeline.DEFAULT_PEOPLE_CSV),
        output_dir=str(index_contacts_pipeline.DEFAULT_OUTPUT_DIR),
        artifact_dir=str(index_contacts_pipeline.DEFAULT_ARTIFACT_DIR),
        manifest=str(index_contacts_pipeline.DEFAULT_MANIFEST),
        openai_usage_tier=os.getenv("POWERPACKS_OPENAI_USAGE_TIER") or None,
        input=[],
        include_existing_artifacts=True,
    )


def ready_duckdb_path(index_payload: dict[str, Any]) -> Path:
    if index_payload.get("status") != "ready":
        raise RuntimeError(index_payload.get("error") or index_payload.get("reason") or "Indexing did not finish ready")
    duckdb_value = index_payload.get("duckdb") or str(index_contacts_pipeline.DEFAULT_OUTPUT_DIR / "local-search.duckdb")
    duckdb_path = Path(str(duckdb_value))
    if not duckdb_path.is_absolute():
        duckdb_path = Path.cwd() / duckdb_path
    if not duckdb_path.exists() or duckdb_path.stat().st_size <= 1024:
        raise RuntimeError(f"Local search DuckDB was not materialized: {duckdb_value}")
    return duckdb_path


def run_gmail_import(operator_id: str, accounts_path: Path) -> dict[str, Any]:
    """Run Gmail import/enrichment with Parallel spend auto-approved.

    The one-button onboarding-v2 flow completes discovery -> enrichment ->
    indexing without an approval gate, so we always approve Parallel spend.
    gmail_import.run() no-ops when its manifest is already current, which keeps
    repeat button clicks from re-spending on enrichment.
    """
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    ns = argparse.Namespace(
        command="run",
        accounts=accounts_path,
        operator_id=operator_id,
        approve_parallel_spend=True,
    )
    return gmail_import.run(ns)


def resolve_inputs(args: argparse.Namespace) -> tuple[Path, list[str]]:
    accounts_path = Path(str(args.accounts)).expanduser()
    accounts = read_json(accounts_path, {}) or {}
    emails = linked_gmail_accounts(accounts)
    return accounts_path, emails


def estimate_parallel_spend() -> dict[str, Any]:
    """Estimate Parallel.ai enrichment spend for the next Gmail run.

    Gmail enrichment resolves contacts that are not already matched in the
    LinkedIn directory through Parallel.ai. The number of pending lookups is the
    size of the discovered resolution queue, so the cost is simply
    pending_contacts * per-lookup price. Returns zero contacts/cost when nothing
    has been discovered yet (the queue is built during the discover stage).
    """
    artifacts = gmail_import.gmail_artifacts_from_discovery()
    pending = gmail_import.pending_gmail_parallel_contacts({"artifacts": artifacts})
    estimated_usd = round(pending * GMAIL_PARALLEL_COST_PER_CONTACT_USD, 2)
    return {
        "pending_contacts": pending,
        "processor": GMAIL_PARALLEL_PROCESSOR,
        "cost_per_contact_usd": GMAIL_PARALLEL_COST_PER_CONTACT_USD,
        "estimated_usd": estimated_usd,
        "auto_approved": True,
    }


def dry_run(args: argparse.Namespace) -> dict[str, Any]:
    accounts_path, emails = resolve_inputs(args)
    current = import_manifest_current(IMPORT_SOURCE, import_dir=DEFAULT_IMPORT_DIR)
    return {
        "status": "dry_run",
        "vertical": VERTICAL,
        "accounts": str(accounts_path),
        "linked_accounts": emails,
        "stages": STAGES,
        "account_stats": {"linked_accounts": len(emails)},
        "current_import": bool(current),
        "parallel_spend_estimate": estimate_parallel_spend(),
        "outputs": {
            "discover_contacts_csv": str(DISCOVER_CONTACTS_CSV),
            "source_people_csv": str(IMPORT_DIR / "people.csv"),
            "merged_people_csv": str(index_contacts_pipeline.DEFAULT_PEOPLE_CSV),
            "duckdb": str(index_contacts_pipeline.DEFAULT_OUTPUT_DIR / "local-search.duckdb"),
        },
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    ctx = make_context(args.run_id)
    accounts_path, emails = resolve_inputs(args)
    try:
        ctx.event("inspect", "Checking linked Gmail accounts", payload={"accounts": str(accounts_path), "linked_accounts": emails})
        if not emails:
            raise RuntimeError("No Gmail accounts are linked. Connect a Gmail account before running.")
        ctx.event("inspect", "Linked Gmail accounts are ready", status="completed", payload={"linked_accounts": emails})

        ctx.event("discover", "Syncing Gmail metadata and discovering contacts", payload={"linked_accounts": emails})
        discovery_payload = gmail_discovery.discover(accounts_path=accounts_path, selected_accounts=emails)
        if discovery_payload.get("status") == "skipped":
            raise RuntimeError(discovery_payload.get("reason") or "Gmail discovery was skipped")
        if discovery_payload.get("status") != "completed":
            raise RuntimeError(discovery_payload.get("reason") or discovery_payload.get("error") or "Gmail discovery did not complete")
        ctx.event("discover", "Gmail contacts are discovered", status="completed", payload={"contacts": discovery_payload.get("contacts"), "selected_accounts": discovery_payload.get("selected_accounts")})

        ctx.event("enrich", "Importing and enriching Gmail contacts (Parallel spend auto-approved)", payload={"contacts": discovery_payload.get("contacts")})
        import_payload = run_gmail_import(args.operator_id, accounts_path)
        import_status = import_payload.get("status")
        if import_status == "blocked_approval":
            payload = {"status": "blocked_approval", "vertical": VERTICAL, "run_id": ctx.run_id, "import": import_payload}
            ctx.event("enrich", "Gmail enrichment needs approval", status="blocked_approval", payload=payload)
            return payload
        if import_status == "skipped":
            raise RuntimeError(import_payload.get("reason") or "Gmail import was skipped")
        if import_status != "completed":
            raise RuntimeError(import_payload.get("reason") or import_payload.get("error") or f"Gmail import failed: {import_status}")
        ctx.event("enrich", "Gmail contacts are enriched", status="completed", payload={"people": (import_payload.get("stats") or {}).get("people"), "noop": import_payload.get("noop", False)})

        people_csv = (import_payload.get("outputs") or {}).get("people_csv")
        ctx.event("source_people", "Writing Gmail people file", payload={"people_csv": people_csv, "people": (import_payload.get("stats") or {}).get("people")})
        ctx.event("source_people", "Gmail people file is ready", status="completed", payload={"people_csv": people_csv, "people": (import_payload.get("stats") or {}).get("people")})

        def index_progress(stage_id: str, message: str, status: str, payload: dict[str, Any] | None) -> None:
            ctx.event(stage_id, message, status=status, payload=payload or {})

        index_payload, index_code = index_contacts_pipeline.run_pipeline(args_for_index(args.operator_id), progress_callback=index_progress)
        if index_code != 0:
            raise RuntimeError(index_payload.get("error") or "Indexing failed")
        duckdb_path = ready_duckdb_path(index_payload)

        result = {
            "status": "completed",
            "vertical": VERTICAL,
            "run_id": ctx.run_id,
            "linked_accounts": emails,
            "discovery": discovery_payload,
            "import": import_payload,
            "index": index_payload,
            "outputs": {
                "source_people_csv": people_csv,
                "merged_people_csv": str(index_contacts_pipeline.DEFAULT_PEOPLE_CSV),
                "duckdb": index_payload.get("duckdb") or str(duckdb_path),
                "status_path": str(ctx.state_path),
                "events_path": str(ctx.events_path),
            },
        }
        ctx.event("search_duckdb", "Gmail contacts are searchable", status="completed", progress=1.0, payload=result["outputs"])
        ctx.update(status="completed", progress=1.0, result=result, completed_at=now_iso())
        return result
    except Exception as exc:
        payload = {"status": "failed", "vertical": VERTICAL, "run_id": ctx.run_id, "error": str(exc), "status_path": str(ctx.state_path)}
        ctx.event(str(ctx.status.get("current_stage") or "inspect"), str(exc), status="failed", payload=payload)
        ctx.update(status="failed", error=str(exc), completed_at=now_iso())
        return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Gmail onboarding v2 vertical")
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd in ["dry-run", "run"]:
        s = sub.add_parser(cmd)
        s.add_argument("--operator-id", default="local")
        s.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS))
        s.add_argument("--run-id", default="")
    status = sub.add_parser("status")
    status.add_argument("--run-id", default="")
    return parser


def status_payload(run_id: str = "") -> dict[str, Any]:
    path = RUN_ROOT / "status.json"
    if not path.exists():
        return {"status": "missing", "vertical": VERTICAL, "path": str(path)}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"status": "failed", "vertical": VERTICAL, "path": str(path), "error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "dry-run":
        payload = dry_run(args)
        emit_json(payload)
        return 0
    if args.command == "status":
        emit_json(status_payload(args.run_id))
        return 0
    payload = run(args)
    emit_json(payload)
    return 0 if payload.get("status") == "completed" else 20 if payload.get("status") == "blocked_approval" else 1


if __name__ == "__main__":
    raise SystemExit(main())
