#!/usr/bin/env python3
"""Run the LinkedIn CSV vertical end-to-end without the legacy setup.py orchestrator.

This primitive is intentionally source-specific. It imports the LinkedIn
building blocks directly, writes progress to stdout, persists status under
.powerpacks/runs/setup-linkedin-csv, and pumps the source output into the shared
network lake for indexing/processing to pick up.
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

from packs.ingestion.primitives.discover_contacts_pipeline import linkedin as linkedin_discovery  # noqa: E402
from packs.ingestion.primitives.discover_contacts_pipeline.common import read_json  # noqa: E402
from packs.ingestion.primitives.discover_contacts_pipeline.directory import commit_people_csv_to_directory  # noqa: E402
from packs.ingestion.primitives.import_contacts_pipeline.common import (  # noqa: E402
    DEFAULT_ACCOUNTS,
    DEFAULT_BASE_DIR,
    DEFAULT_DIRECTORY_CSV,
    DEFAULT_IMPORT_DIR,
    copy_people_csv,
    csv_count,
    import_manifest_current,
    linkedin_csv_path,
    linkedin_source_user,
    sha256_file,
    write_manifest,
)
from packs.ingestion.primitives.linkedin_network_import import linkedin_network_import  # noqa: E402
from packs.indexing.primitives.index_contacts_pipeline import index_contacts_pipeline  # noqa: E402
from packs.ingestion.accounts import load_registry, save_registry  # noqa: E402

VERTICAL = "linkedin_csv"
RUN_ROOT = Path(".powerpacks/runs/setup-linkedin-csv")
IMPORT_SOURCE = "linkedin"
IMPORT_DIR = DEFAULT_IMPORT_DIR / IMPORT_SOURCE
IMPORT_LEDGER = IMPORT_DIR / "ledger.json"
IMPORT_ARTIFACT_DIR = IMPORT_DIR
DISCOVER_DIR = DEFAULT_BASE_DIR / "discover" / "linkedin"
DISCOVER_CONNECTIONS_CSV = DISCOVER_DIR / "Connections.csv"
STAGES = [
    {"id": "inspect", "label": "Check LinkedIn CSV"},
    {"id": "discover", "label": "Import LinkedIn contacts"},
    {"id": "enrich", "label": "Enrich LinkedIn profiles"},
    {"id": "source_people", "label": "Save LinkedIn people file"},
    {"id": "merge_network", "label": "Merge contact sources"},
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


def read_csv_stats(path: Path, source_user: str) -> dict[str, Any]:
    try:
        rows, stats = linkedin_discovery.parse_connections_csv(path, source_user)
    except Exception as exc:
        return {"status": "failed", "error": str(exc), "path": str(path)}
    return {
        "status": "ok",
        "path": str(path),
        "bytes": path.stat().st_size if path.exists() else 0,
        "valid_contacts": len(rows),
        "duplicates": stats.get("duplicates", 0),
        "skipped_invalid": stats.get("skipped_invalid", 0),
    }


def same_file_content(left: Path, right: Path) -> bool:
    if not left.exists() or not right.exists() or not left.is_file() or not right.is_file():
        return False
    try:
        if left.resolve() == right.resolve():
            return True
    except OSError:
        pass
    return left.stat().st_size == right.stat().st_size and sha256_file(left) == sha256_file(right)


def stable_manifest_csv_path(csv_path: Path) -> Path:
    """Return the CSV path used in the import manifest currentness check.

    Discovery copies uploads into the repo-local stable LinkedIn export path.
    Import manifests are written against that stable path, so currentness must
    check the same path once the stable copy matches the selected/uploaded CSV.
    """
    return DISCOVER_CONNECTIONS_CSV if same_file_content(csv_path, DISCOVER_CONNECTIONS_CSV) else csv_path


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


def run_linkedin_import(csv_path: Path, source_user: str, operator_id: str, *, force: bool) -> tuple[int, dict[str, Any]]:
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    ns = argparse.Namespace(
        csv=str(csv_path),
        source_user=source_user,
        operator_id=operator_id,
        limit=None,
        output_dir=str(IMPORT_DIR),
        ledger=str(IMPORT_LEDGER),
        force=force,
        no_harmonic=False,
        refresh_cache=False,
        profile_cache_dir=str(DEFAULT_BASE_DIR / "profile_cache_v2"),
        company_corpus_jsonl=[],
        sleep_seconds=0.0,
        force_enrich=False,
        convert_only=False,
        max_workers=linkedin_network_import.people_enrichment.DEFAULT_RAPIDAPI_MAX_WORKERS,
        max_rpm=linkedin_network_import.people_enrichment.DEFAULT_RAPIDAPI_MAX_RPM,
        failure_retry_hours=linkedin_network_import.people_enrichment.DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS,
    )
    code = linkedin_network_import.command_run(ns)
    ledger = linkedin_network_import.load_ledger(IMPORT_LEDGER) if IMPORT_LEDGER.exists() else {}
    return code, ledger


def finalize_import_manifest(ledger: dict[str, Any], csv_path: Path, source_user: str) -> dict[str, Any]:
    status = "completed" if ledger.get("status") == "completed" else ledger.get("status") or "failed"
    artifacts = ledger.get("artifacts") if isinstance(ledger.get("artifacts"), dict) else {}
    people_csv = copy_people_csv(IMPORT_SOURCE, str(artifacts.get("people_csv") or ""), import_dir=DEFAULT_IMPORT_DIR)
    directory_checkpoint: dict[str, Any] = {}
    if status == "completed" and people_csv:
        directory_checkpoint = commit_people_csv_to_directory(
            {"linkedin_directory_csv": str(DEFAULT_DIRECTORY_CSV)},
            artifacts,
            people_csv,
            source="linkedin_csv",
            source_account=source_user,
        )
    return write_manifest(IMPORT_SOURCE, {
        "status": status,
        "ledger": str(IMPORT_LEDGER),
        "artifact_dir": str(IMPORT_DIR),
        "command_status": 0 if status == "completed" else 1,
        "child": {"status": status, "ledger": str(IMPORT_LEDGER), "artifacts": artifacts},
        "error": "" if status == "completed" else ledger.get("error", ""),
        "input": {"connections_csv": str(csv_path), "source_user": source_user},
        "outputs": {"people_csv": people_csv, "directory_csv": str(DEFAULT_DIRECTORY_CSV)},
        "stats": {"people": csv_count(people_csv), "candidates": csv_count(str(DEFAULT_BASE_DIR / "discover" / "linkedin" / "contacts.csv"))},
        "directory_checkpoint": directory_checkpoint,
        "artifacts": artifacts,
    }, import_dir=DEFAULT_IMPORT_DIR)


def resolve_inputs(args: argparse.Namespace) -> tuple[Path, str]:
    accounts = read_json(Path(args.accounts), {}) or {}
    csv_value = args.csv or linkedin_csv_path(accounts)
    source_user = args.source_user or linkedin_source_user(accounts)
    return Path(str(csv_value)).expanduser(), str(source_user or "local")


def link(args: argparse.Namespace) -> dict[str, Any]:
    """Register an uploaded Connections.csv without processing it.

    Writes csv_path/source_label and flips linked=true in accounts.json so the
    console source page shows LinkedIn connected. Discovery, enrichment, and
    indexing stay behind their own explicit buttons — link does none of them.
    """
    csv_path, source_user = resolve_inputs(args)
    if not csv_path.exists():
        return {"status": "failed", "vertical": VERTICAL, "error": f"LinkedIn Connections CSV not found: {csv_path}"}
    csv_stats = read_csv_stats(csv_path, source_user)
    if csv_stats.get("status") != "ok":
        return {"status": "failed", "vertical": VERTICAL, "error": csv_stats.get("error") or "Could not parse LinkedIn CSV", "csv_stats": csv_stats}
    accounts_path = Path(args.accounts)
    registry = load_registry(accounts_path)
    li = registry["accounts"]["linkedin_csv"]
    li["config"]["csv_path"] = str(csv_path)
    if args.source_user:
        li["config"]["source_label"] = source_user
    li["linked"] = True
    li["skipped"] = False
    li["last_success_at"] = now_iso()
    if str(csv_path) not in li["artifacts"]:
        li["artifacts"].append(str(csv_path))
    save_registry(registry, accounts_path)
    return {"status": "completed", "vertical": VERTICAL, "linked": True, "csv": str(csv_path), "source_user": source_user, "csv_stats": csv_stats}


def dry_run(args: argparse.Namespace) -> dict[str, Any]:
    csv_path, source_user = resolve_inputs(args)
    stats = read_csv_stats(csv_path, source_user) if csv_path.exists() else {"status": "missing", "path": str(csv_path)}
    manifest_csv_path = stable_manifest_csv_path(csv_path)
    current = import_manifest_current(IMPORT_SOURCE, {"connections_csv": str(manifest_csv_path), "source_user": source_user}, import_dir=DEFAULT_IMPORT_DIR)
    return {
        "status": "dry_run",
        "vertical": VERTICAL,
        "csv": str(csv_path),
        "manifest_csv": str(manifest_csv_path),
        "source_user": source_user,
        "stages": STAGES,
        "csv_stats": stats,
        "current_import": bool(current),
        "outputs": {
            "discover_contacts_csv": str(DEFAULT_BASE_DIR / "discover" / "linkedin" / "contacts.csv"),
            "source_people_csv": str(DEFAULT_IMPORT_DIR / "linkedin" / "people.csv"),
            "merged_people_csv": str(index_contacts_pipeline.DEFAULT_PEOPLE_CSV),
            "duckdb": str(index_contacts_pipeline.DEFAULT_OUTPUT_DIR / "local-search.duckdb"),
        },
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    ctx = make_context(args.run_id)
    csv_path, source_user = resolve_inputs(args)
    try:
        ctx.event("inspect", "Reading LinkedIn CSV", payload={"csv": str(csv_path), "source_user": source_user})
        if not csv_path.exists():
            raise FileNotFoundError(f"LinkedIn Connections CSV not found: {csv_path}")
        csv_stats = read_csv_stats(csv_path, source_user)
        if csv_stats.get("status") != "ok":
            raise RuntimeError(csv_stats.get("error") or "Could not parse LinkedIn CSV")
        ctx.event("inspect", "LinkedIn CSV is readable", status="completed", payload=csv_stats)

        # Write csv path and source label back to accounts.json
        accounts_data = read_json(Path(args.accounts), {}) or {}
        li = accounts_data.setdefault("accounts", {}).setdefault("linkedin_csv", {})
        li_config = li.setdefault("config", {})
        li_config["csv_path"] = str(csv_path)
        Path(args.accounts).write_text(json.dumps(accounts_data, indent=2) + "\n")

        ctx.event("discover", "Copying and parsing LinkedIn contacts", payload=csv_stats)
        discovery_payload = linkedin_discovery.discover(accounts_path=Path(args.accounts), connections_csv=csv_path, source_user_label=source_user)
        if discovery_payload.get("status") != "completed":
            raise RuntimeError(discovery_payload.get("reason") or discovery_payload.get("error") or "LinkedIn discovery did not complete")
        stable_csv_path = Path(str(discovery_payload.get("source_csv") or DISCOVER_CONNECTIONS_CSV))
        ctx.event("discover", "LinkedIn contacts CSV is parsed", status="completed", payload=discovery_payload)

        ctx.event("enrich", "Running LinkedIn import and RapidAPI/cache enrichment", payload={"contacts": discovery_payload.get("contacts")})
        current = None if args.force else import_manifest_current(IMPORT_SOURCE, {"connections_csv": str(stable_csv_path), "source_user": source_user}, import_dir=DEFAULT_IMPORT_DIR)
        if current:
            import_payload = current
            import_payload = {**import_payload, "noop": True}
        else:
            code, ledger = run_linkedin_import(stable_csv_path, source_user, args.operator_id, force=True)
            if code == 20 or ledger.get("status") == "blocked_approval":
                payload = {"status": "blocked_approval", "vertical": VERTICAL, "ledger": str(IMPORT_LEDGER), "import_ledger": ledger}
                ctx.event("enrich", "LinkedIn enrichment needs approval", status="blocked_approval", payload=payload)
                return payload
            if code != 0 or ledger.get("status") != "completed":
                raise RuntimeError(f"LinkedIn import failed: {ledger.get('status') or code}")
            import_payload = finalize_import_manifest(ledger, stable_csv_path, source_user)
        ctx.event("enrich", "LinkedIn people are enriched", status="completed", payload={"people": (import_payload.get("stats") or {}).get("people"), "noop": import_payload.get("noop", False)})

        ctx.event("source_people", "Writing LinkedIn people file", payload={"people_csv": (import_payload.get("outputs") or {}).get("people_csv"), "people": (import_payload.get("stats") or {}).get("people")})
        ctx.event("source_people", "LinkedIn people file is ready", status="completed", payload={"people_csv": (import_payload.get("outputs") or {}).get("people_csv"), "people": (import_payload.get("stats") or {}).get("people")})

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
            "csv_stats": csv_stats,
            "discovery": discovery_payload,
            "import": import_payload,
            "index": index_payload,
            "outputs": {
                "source_people_csv": (import_payload.get("outputs") or {}).get("people_csv"),
                "merged_people_csv": str(index_contacts_pipeline.DEFAULT_PEOPLE_CSV),
                "duckdb": index_payload.get("duckdb") or str(duckdb_path),
                "status_path": str(ctx.state_path),
                "events_path": str(ctx.events_path),
            },
        }
        ctx.event("search_duckdb", "LinkedIn CSV is searchable", status="completed", progress=1.0, payload=result["outputs"])
        ctx.update(status="completed", progress=1.0, result=result, completed_at=now_iso())
        return result
    except Exception as exc:
        payload = {"status": "failed", "vertical": VERTICAL, "run_id": ctx.run_id, "error": str(exc), "status_path": str(ctx.state_path)}
        ctx.event(str(ctx.status.get("current_stage") or "inspect"), str(exc), status="failed", payload=payload)
        ctx.update(status="failed", error=str(exc), completed_at=now_iso())
        return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run LinkedIn CSV onboarding v2 vertical")
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd in ["dry-run", "run", "link"]:
        s = sub.add_parser(cmd)
        s.add_argument("--csv", default="")
        s.add_argument("--source-user", default="")
        s.add_argument("--operator-id", default="local")
        s.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS))
        s.add_argument("--run-id", default="")
        s.add_argument("--force", action="store_true")
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
        return 0 if payload.get("csv_stats", {}).get("status") != "failed" else 1
    if args.command == "status":
        emit_json(status_payload(args.run_id))
        return 0
    if args.command == "link":
        payload = link(args)
        emit_json(payload)
        return 0 if payload.get("status") == "completed" else 1
    payload = run(args)
    emit_json(payload)
    return 0 if payload.get("status") == "completed" else 20 if payload.get("status") == "blocked_approval" else 1


if __name__ == "__main__":
    raise SystemExit(main())
