#!/usr/bin/env python3
"""Resumable local LinkedIn Connections.csv import.

This primitive is source-specific only: it parses LinkedIn's Connections.csv into
Powerpacks' shared people schema, then delegates LinkedIn profile enrichment to
`packs/ingestion/primitives/enrich_people`. Provider calls, cache handling,
normalization, and final `people.csv` writing live in that shared primitive.

Stdlib-only. Local artifacts only. External RapidAPI calls are approval-gated by
`enrich_people`; seeded profile-cache hits complete without keys or approval.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.primitives.enrich_people import enrich_people as people_enrichment
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS as PEOPLE_COLUMNS,
        extract_public_identifier,
        normalize_linkedin_url,
        normalize_people_row,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.primitives.enrich_people import enrich_people as people_enrichment
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS as PEOPLE_COLUMNS,
        extract_public_identifier,
        normalize_linkedin_url,
        normalize_people_row,
    )

DEFAULT_LEDGER = Path(".powerpacks/network-import/linkedin/import-run.json")
DEFAULT_BASE_DIR = Path(".powerpacks/network-import")
DEFAULT_PROFILE_CACHE_DIR = DEFAULT_BASE_DIR / "profile_cache_v2"

CONNECTION_COLUMNS = [
    "person_id",
    "public_identifier",
    "linkedin_url",
    "first_name",
    "last_name",
    "source_user",
    "linkedin_company",
    "linkedin_position",
    "linkedin_email",
    "connected_on",
]
PIPELINE_STEPS = ["convert", "enrich_people"]


class PipelineBlocked(Exception):
    def __init__(self, payload: dict[str, Any], code: int = 20) -> None:
        super().__init__(payload.get("message") or "blocked")
        self.payload = payload
        self.code = code


class PipelineFailed(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def generate_person_id(public_identifier: str) -> str:
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"linkedin:{public_identifier.lower().strip()}"))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


@dataclass
class LinkedInConnection:
    first_name: str
    last_name: str
    linkedin_url: str
    email_address: str
    company: str
    position: str
    connected_on: str
    public_identifier: str
    person_id: str
    source_user: str

    def row(self) -> dict[str, str]:
        return {
            "person_id": self.person_id,
            "public_identifier": self.public_identifier,
            "linkedin_url": self.linkedin_url,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "source_user": self.source_user,
            "linkedin_company": self.company,
            "linkedin_position": self.position,
            "linkedin_email": self.email_address,
            "connected_on": self.connected_on,
        }

    def people_row(self, source_csv: Path) -> dict[str, str]:
        full_name = f"{self.first_name} {self.last_name}".strip()
        provenance = {
            "source": "linkedin_csv",
            "source_user": self.source_user,
            "source_csv": str(source_csv),
            "connected_on": self.connected_on,
        }
        return normalize_people_row({
            "id": self.person_id,
            "public_identifier": self.public_identifier,
            "linkedin_url": self.linkedin_url,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": full_name,
            "current_title": self.position,
            "current_company": self.company,
            "primary_email": self.email_address,
            "all_emails": self.email_address,
            "source_channels": "linkedin_csv",
            "source_artifacts": json.dumps(provenance, sort_keys=True),
            "enrichment_provider": "linkedin_csv_source",
        })


def linkedin_export_header(line: str) -> bool:
    lowered = line.strip().lower()
    return lowered.startswith("first name,") or ("first name" in lowered and "url" in lowered and "," in lowered)


def parse_connections_csv(path: Path, source_user: str, limit: int | None = None) -> tuple[list[LinkedInConnection], dict[str, Any]]:
    if not path.exists():
        raise PipelineFailed(f"LinkedIn Connections CSV not found: {path}")
    connections: list[LinkedInConnection] = []
    seen: set[str] = set()
    duplicates = 0
    skipped = 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        header_line = ""
        for line in handle:
            if linkedin_export_header(line):
                header_line = line.strip()
                break
        if not header_line:
            raise PipelineFailed("Could not find LinkedIn export header row containing 'First Name' and 'URL'")
        reader = csv.DictReader(handle, fieldnames=next(csv.reader([header_line])))
        for row in reader:
            url = normalize_linkedin_url(row.get("URL", ""))
            pub_id = extract_public_identifier(url)
            if not pub_id:
                skipped += 1
                continue
            if pub_id in seen:
                duplicates += 1
                continue
            seen.add(pub_id)
            connections.append(
                LinkedInConnection(
                    first_name=(row.get("First Name") or "").strip(),
                    last_name=(row.get("Last Name") or "").strip(),
                    linkedin_url=url,
                    email_address=(row.get("Email Address") or "").strip(),
                    company=(row.get("Company") or "").strip(),
                    position=(row.get("Position") or "").strip(),
                    connected_on=(row.get("Connected On") or "").strip(),
                    public_identifier=pub_id,
                    person_id=generate_person_id(pub_id),
                    source_user=source_user,
                )
            )
            if limit and len(connections) >= limit:
                break
    return connections, {"parsed": len(connections), "duplicates": duplicates, "skipped_invalid": skipped}


def load_ledger(path: Path) -> dict[str, Any]:
    ledger = read_json(path, {}) or {}
    ledger.setdefault("primitive", "linkedin_network_import")
    ledger.setdefault("version", 2)
    ledger.setdefault("created_at", now_iso())
    ledger.setdefault("updated_at", now_iso())
    ledger.setdefault("steps", {})
    ledger.setdefault("approvals", {})
    ledger.setdefault("artifacts", {})
    return ledger


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = now_iso()
    write_json(path, ledger)


def mark_step(ledger: dict[str, Any], step_id: str, status: str, **extra: Any) -> None:
    rec = ledger.setdefault("steps", {}).setdefault(step_id, {"id": step_id})
    if status == "running" and "started_at" not in rec:
        rec["started_at"] = now_iso()
    if status in {"completed", "failed", "blocked_approval", "skipped"}:
        rec["finished_at"] = now_iso()
    rec["status"] = status
    rec.update({k: v for k, v in extra.items() if v is not None})


def next_pending_step(ledger: dict[str, Any]) -> str | None:
    for step_id in PIPELINE_STEPS:
        if ledger.setdefault("steps", {}).get(step_id, {}).get("status") != "completed":
            return step_id
    return None


def approval_id(ledger: dict[str, Any], step_id: str) -> str:
    return f"{ledger.get('run_id', 'run')}:{step_id}"


def block_for_delegate_approval(ledger_path: Path, ledger: dict[str, Any], blocked: dict[str, Any], delegate_ledger: Path) -> None:
    app_id = approval_id(ledger, "enrich_people")
    ledger["blocked"] = {
        "step_id": "enrich_people",
        "approval_id": app_id,
        "approval_type": blocked.get("approval_type", "external_api_spend"),
        "delegate_ledger": str(delegate_ledger),
        "delegate_approval_id": blocked.get("approval_id"),
    }
    mark_step(ledger, "enrich_people", "blocked_approval", approval_id=app_id, approval_type=ledger["blocked"]["approval_type"])
    save_ledger(ledger_path, ledger)
    raise PipelineBlocked({
        "status": "blocked_approval",
        "step_id": "enrich_people",
        "approval_id": app_id,
        "approval_type": ledger["blocked"]["approval_type"],
        "message": blocked.get("message") or "Approve paid LinkedIn enrichment?",
        "ledger": str(ledger_path),
        "delegate_ledger": str(delegate_ledger),
        "delegate_approval_id": blocked.get("approval_id"),
        "continue_command": f"uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py approve --ledger {ledger_path} && uv run --project . python packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py continue --ledger {ledger_path}",
    })


def step_convert(ledger: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(ledger["run_dir"])
    inp = Path(ledger["input"]["csv"])
    connections, stats = parse_connections_csv(inp, ledger["input"]["source_user"], ledger["input"].get("limit"))
    connection_rows = [conn.row() for conn in connections]
    people_rows = [conn.people_row(inp) for conn in connections]
    connections_out = run_dir / "connections_for_enrichment.csv"
    people_out = run_dir / "source_people.csv"
    write_csv(connections_out, CONNECTION_COLUMNS, connection_rows)
    write_csv(people_out, PEOPLE_COLUMNS, people_rows)
    ledger["artifacts"].update({
        "connections_csv": str(connections_out),
        "source_people_csv": str(people_out),
    })
    return {**stats, "connections_file": str(connections_out), "source_people_file": str(people_out)}


def ensure_delegate_ledger(ledger: dict[str, Any]) -> Path:
    run_dir = Path(ledger["run_dir"])
    delegate_path = run_dir / "enrich_people.ledger.json"
    if delegate_path.exists():
        return delegate_path
    source_people = ledger.get("artifacts", {}).get("source_people_csv")
    if not source_people:
        raise PipelineFailed("convert step did not produce source_people_csv")
    delegate = {
        "primitive": "enrich_people",
        "version": 1,
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "run_id": f"{ledger.get('run_id', 'linkedin')}-enrichment",
        "run_dir": str(run_dir),
        "ledger": str(delegate_path),
        "input": {
            "input_csv": source_people,
            "limit": None,
            "force": bool(ledger.get("input", {}).get("force_enrich")),
            "profile_cache_dir": ledger.get("input", {}).get("profile_cache_dir") or str(DEFAULT_PROFILE_CACHE_DIR),
            "refresh_cache": bool(ledger.get("input", {}).get("refresh_cache")),
            "company_corpus_jsonl": ledger.get("input", {}).get("company_corpus_jsonl") or [],
            "sleep_seconds": ledger.get("input", {}).get("sleep_seconds") or 0.0,
            "max_workers": ledger.get("input", {}).get("max_workers"),
            "max_rpm": ledger.get("input", {}).get("max_rpm"),
            "failure_retry_hours": ledger.get("input", {}).get("failure_retry_hours"),
        },
        "steps": {},
        "approvals": {},
        "artifacts": {},
    }
    people_enrichment.save_ledger(delegate_path, delegate)
    return delegate_path


def step_enrich_people(ledger: dict[str, Any], ledger_path: Path) -> dict[str, Any]:
    delegate_path = ensure_delegate_ledger(ledger)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            code = people_enrichment.run_until_blocked_or_done(delegate_path)
    except people_enrichment.PipelineBlocked as blocked:
        block_for_delegate_approval(ledger_path, ledger, blocked.payload, delegate_path)
    if code != 0:
        delegate = people_enrichment.load_ledger(delegate_path)
        failed_step = people_enrichment.next_pending_step(delegate)
        error = "enrich_people failed"
        if failed_step:
            error = delegate.get("steps", {}).get(failed_step, {}).get("error") or error
        raise PipelineFailed(error)

    delegate = people_enrichment.load_ledger(delegate_path)
    artifacts = delegate.get("artifacts", {}) or {}
    ledger["artifacts"].update({f"enrich_people_{key}": value for key, value in artifacts.items()})
    for key in [
        "people_csv",
        "provider_enriched_csv",
        "linkedin_enrichment_queue_csv",
        "rapidapi_cache_hits_csv",
        "rapidapi_cache_misses_csv",
        "needs_resolution_queue_csv",
        "skipped_enrichment_csv",
        "raw_provider_responses_dir",
    ]:
        if artifacts.get(key):
            ledger["artifacts"][key] = artifacts[key]
    ledger["artifacts"]["enrich_people_ledger"] = str(delegate_path)
    return {
        "delegate_ledger": str(delegate_path),
        "people_csv": artifacts.get("people_csv"),
        "cache_hit_count": delegate.get("cache_hit_count", 0),
        "paid_call_count": delegate.get("paid_call_count", 0),
        "queue_count": delegate.get("queue_count", 0),
    }


def execute_step(ledger_path: Path, ledger: dict[str, Any], step_id: str) -> dict[str, Any]:
    if step_id == "convert":
        return step_convert(ledger)
    if step_id == "enrich_people":
        return step_enrich_people(ledger, ledger_path)
    raise PipelineFailed(f"unknown step: {step_id}")


def run_until_blocked_or_done(ledger_path: Path) -> int:
    ledger = load_ledger(ledger_path)
    while True:
        step_id = next_pending_step(ledger)
        if step_id is None:
            ledger["status"] = "completed"
            ledger.pop("blocked", None)
            save_ledger(ledger_path, ledger)
            emit({"status": "completed", "ledger": str(ledger_path), "run_dir": ledger.get("run_dir"), "artifacts": ledger.get("artifacts", {})})
            return 0
        try:
            mark_step(ledger, step_id, "running")
            save_ledger(ledger_path, ledger)
            summary = execute_step(ledger_path, ledger, step_id)
            if step_id == "convert":
                ledger["connection_count"] = summary.get("parsed", 0)
            mark_step(ledger, step_id, "completed", summary=summary)
            ledger.pop("blocked", None)
            save_ledger(ledger_path, ledger)
        except PipelineFailed as exc:
            mark_step(ledger, step_id, "failed", error=str(exc))
            ledger["status"] = "failed"
            save_ledger(ledger_path, ledger)
            emit({"status": "failed", "step_id": step_id, "error": str(exc), "ledger": str(ledger_path)})
            return 1


def command_run(args: argparse.Namespace) -> int:
    run_id = args.run_id or f"linkedin-{sha(str(args.csv) + ':' + now_iso())}"
    run_dir = Path(args.output_dir) / "linkedin" / run_id
    ledger_path = Path(args.ledger)
    if ledger_path.exists() and not args.force:
        existing = load_ledger(ledger_path)
        if existing.get("status") not in {"completed", "failed"}:
            emit({"status": "active_run_exists", "ledger": str(ledger_path), "message": "Use continue/approve or --force."})
            return 0
    if args.no_harmonic:
        # Accepted for old scripts; shared enrichment is RapidAPI-only now.
        pass
    ledger = {
        "primitive": "linkedin_network_import",
        "version": 2,
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "ledger": str(ledger_path),
        "input": {
            "csv": str(Path(args.csv)),
            "source_user": args.source_user,
            "operator_id": args.operator_id,
            "limit": args.limit,
            "profile_cache_dir": str(Path(args.profile_cache_dir)),
            "refresh_cache": args.refresh_cache,
            "company_corpus_jsonl": [str(Path(p)) for p in (args.company_corpus_jsonl or [])],
            "sleep_seconds": args.sleep_seconds,
            "force_enrich": args.force_enrich,
            "max_workers": args.max_workers,
            "max_rpm": args.max_rpm,
            "failure_retry_hours": args.failure_retry_hours,
        },
        "steps": {},
        "approvals": {},
        "artifacts": {},
    }
    save_ledger(ledger_path, ledger)
    try:
        return run_until_blocked_or_done(ledger_path)
    except PipelineBlocked as blocked:
        emit(blocked.payload)
        return blocked.code


def command_continue(args: argparse.Namespace) -> int:
    if not Path(args.ledger).exists():
        emit({"status": "missing_ledger", "ledger": args.ledger})
        return 2
    try:
        return run_until_blocked_or_done(Path(args.ledger))
    except PipelineBlocked as blocked:
        emit(blocked.payload)
        return blocked.code


def command_approve(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    ledger = load_ledger(ledger_path)
    blocked = ledger.get("blocked") or {}
    app_id = args.approval_id or blocked.get("approval_id")
    if not app_id:
        emit({"status": "no_pending_approval", "ledger": str(ledger_path)})
        return 1
    ledger.setdefault("approvals", {})[app_id] = {"approved_at": now_iso(), "source": "operator"}

    delegate_ledger = blocked.get("delegate_ledger")
    delegate_approval_id = blocked.get("delegate_approval_id")
    if delegate_ledger and delegate_approval_id:
        delegate_path = Path(delegate_ledger)
        delegate = people_enrichment.load_ledger(delegate_path)
        delegate.setdefault("approvals", {})[delegate_approval_id] = {"approved_at": now_iso(), "source": "operator", "step_id": "enrich_linkedin"}
        if delegate.get("blocked", {}).get("approval_id") == delegate_approval_id:
            delegate.pop("blocked", None)
        people_enrichment.save_ledger(delegate_path, delegate)

    ledger.pop("blocked", None)
    save_ledger(ledger_path, ledger)
    emit({"status": "approved", "approval_id": app_id, "ledger": str(ledger_path), "delegate_ledger": delegate_ledger})
    return 0


def command_status(args: argparse.Namespace) -> int:
    ledger = load_ledger(Path(args.ledger))
    emit({"status": ledger.get("status", "unknown"), "blocked": ledger.get("blocked"), "steps": ledger.get("steps", {}), "artifacts": ledger.get("artifacts", {})})
    return 0


def command_check_keys(_: argparse.Namespace) -> int:
    return people_enrichment.command_check_keys(argparse.Namespace())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LinkedIn Connections.csv network import")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--csv", required=True, help="LinkedIn Connections.csv export")
    run.add_argument("--source-user", required=True, help="Non-secret source label for this export")
    run.add_argument("--operator-id", default="local")
    run.add_argument("--limit", type=int, help=argparse.SUPPRESS)
    run.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    run.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    run.add_argument("--run-id")
    run.add_argument("--force", action="store_true", help="Overwrite an active linkedin_network_import ledger")
    run.add_argument("--force-enrich", action="store_true", help="Re-enrich rows even when source rows look complete")
    run.add_argument("--profile-cache-dir", default=str(DEFAULT_PROFILE_CACHE_DIR))
    run.add_argument("--refresh-cache", action="store_true", help="Force RapidAPI calls even when cache entries exist")
    run.add_argument("--company-corpus-jsonl", action="append", default=[])
    run.add_argument("--sleep-seconds", type=float, default=0.0)
    run.add_argument("--max-workers", type=int, default=people_enrichment.DEFAULT_RAPIDAPI_MAX_WORKERS)
    run.add_argument("--max-rpm", type=float, default=people_enrichment.DEFAULT_RAPIDAPI_MAX_RPM)
    run.add_argument("--failure-retry-hours", type=float, default=people_enrichment.DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS)
    run.add_argument("--no-harmonic", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--no-rapidapi", action="store_true", help=argparse.SUPPRESS)
    run.set_defaults(func=command_run)

    cont = sub.add_parser("continue")
    cont.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    cont.set_defaults(func=command_continue)

    approve = sub.add_parser("approve")
    approve.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    approve.add_argument("--approval-id")
    approve.set_defaults(func=command_approve)

    status = sub.add_parser("status")
    status.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    status.set_defaults(func=command_status)

    keys = sub.add_parser("check-keys")
    keys.set_defaults(func=command_check_keys)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "no_rapidapi", False):
        emit({"status": "error", "error": "linkedin_network_import delegates to RapidAPI-only enrich_people; --no-rapidapi is no longer supported"})
        return 2
    try:
        return args.func(args)
    except ValueError as exc:
        emit({"status": "error", "error": str(exc)})
        return 2
    except KeyboardInterrupt:
        emit({"status": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
