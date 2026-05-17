#!/usr/bin/env python3
"""Orchestrate local network ingestion sources into merged CSVs + DuckDB.

Sources handled here:
- LinkedIn Connections.csv -> linkedin_network_import
- Gmail msgvault SQLite -> gmail_network_import msgvault
- Existing message contacts and Twitter artifacts are picked up by merge discovery

This orchestrator is local-first. It does not upload or mutate production. Child
primitives remain responsible for their own approval gates.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LEDGER = Path(".powerpacks/network-import/import-network-run.json")
DEFAULT_BASE_DIR = Path(".powerpacks/network-import")
DEFAULT_MSGVAULT_DB = Path.home() / ".msgvault" / "msgvault.db"


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


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_last_json(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    idx = 0
    last: dict[str, Any] = {}
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        try:
            value, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        if isinstance(value, dict):
            last = value
        idx = end
    return last


def run_cmd(cmd: list[str], *, timeout: int = 300) -> tuple[int, dict[str, Any], str]:
    proc = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[4], capture_output=True, text=True, timeout=timeout, check=False)
    return proc.returncode, parse_last_json(proc.stdout), proc.stderr


def py_cmd(script: str, *args: str) -> list[str]:
    return [sys.executable, script, *args]


def load_ledger(path: Path) -> dict[str, Any]:
    ledger = read_json(path, {}) or {}
    ledger.setdefault("primitive", "import_network_pipeline")
    ledger.setdefault("version", 1)
    ledger.setdefault("created_at", now_iso())
    ledger.setdefault("updated_at", now_iso())
    ledger.setdefault("steps", {})
    ledger.setdefault("artifacts", {})
    return ledger


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = now_iso()
    write_json(path, ledger)


def mark_step(ledger: dict[str, Any], step: str, status: str, **extra: Any) -> None:
    rec = ledger.setdefault("steps", {}).setdefault(step, {"id": step})
    if status == "running" and "started_at" not in rec:
        rec["started_at"] = now_iso()
    if status in {"completed", "failed", "blocked", "skipped"}:
        rec["finished_at"] = now_iso()
    rec["status"] = status
    rec.update({k: v for k, v in extra.items() if v is not None})


def run_linkedin(ledger_path: Path, ledger: dict[str, Any], mode: str) -> bool:
    input_cfg = ledger.get("input", {})
    if not input_cfg.get("linkedin_csv"):
        mark_step(ledger, "linkedin", "skipped", reason="no --linkedin-csv")
        return True
    child_ledger = Path(ledger["run_dir"]) / "linkedin.ledger.json"
    if mode == "run":
        cmd = py_cmd(
            "packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py",
            "run",
            "--csv", input_cfg["linkedin_csv"],
            "--source-user", input_cfg.get("linkedin_source_user") or "local",
            "--operator-id", input_cfg.get("operator_id") or "local",
            "--output-dir", str(DEFAULT_BASE_DIR),
            "--ledger", str(child_ledger),
            "--run-id", f"{ledger['run_id']}-linkedin",
            "--force",
        )
        if input_cfg.get("linkedin_limit") is not None:
            cmd.extend(["--limit", str(input_cfg["linkedin_limit"])])
    else:
        cmd = py_cmd("packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py", "continue", "--ledger", str(child_ledger))
    code, payload, stderr = run_cmd(cmd)
    ledger.setdefault("artifacts", {})["linkedin_ledger"] = str(child_ledger)
    if code == 20 or payload.get("status") == "blocked_approval":
        ledger["blocked"] = {"step_id": "linkedin", "child_ledger": str(child_ledger), "child": payload}
        mark_step(ledger, "linkedin", "blocked", payload=payload)
        save_ledger(ledger_path, ledger)
        emit({"status": "blocked_approval", "step_id": "linkedin", "ledger": str(ledger_path), "child": payload})
        return False
    if code != 0:
        mark_step(ledger, "linkedin", "failed", error=stderr or payload.get("error") or payload)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "linkedin", "error": stderr or payload})
        return False
    mark_step(ledger, "linkedin", "completed", payload=payload)
    for key, value in (payload.get("artifacts") or {}).items():
        ledger.setdefault("artifacts", {})[f"linkedin_{key}"] = value
    return True


def run_gmail_msgvault(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    input_cfg = ledger.get("input", {})
    if not input_cfg.get("gmail_account_email") and not input_cfg.get("msgvault_db"):
        mark_step(ledger, "gmail_msgvault", "skipped", reason="no --gmail-account-email/--msgvault-db")
        return True
    db = input_cfg.get("msgvault_db") or str(DEFAULT_MSGVAULT_DB)
    cmd = py_cmd(
        "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py",
        "msgvault",
        "--db", db,
        "--operator-id", input_cfg.get("operator_id") or "local",
        "--run-id", f"{ledger['run_id']}-gmail",
    )
    if input_cfg.get("gmail_account_email"):
        cmd.extend(["--account-email", input_cfg["gmail_account_email"]])
    if input_cfg.get("gmail_limit") is not None:
        cmd.extend(["--limit", str(input_cfg["gmail_limit"])])
    if input_cfg.get("include_automated_gmail"):
        cmd.append("--include-automated")
    code, payload, stderr = run_cmd(cmd)
    if code != 0:
        mark_step(ledger, "gmail_msgvault", "failed", error=stderr or payload.get("error") or payload)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "gmail_msgvault", "error": stderr or payload})
        return False
    mark_step(ledger, "gmail_msgvault", "completed", payload=payload)
    for key, value in (payload.get("artifacts") or {}).items():
        ledger.setdefault("artifacts", {})[f"gmail_{key}"] = value
    return True


def run_merge(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    merge_dir = Path(ledger["run_dir"]) / "merged"
    cmd = py_cmd(
        "packs/ingestion/primitives/merge_network_sources/merge_network_sources.py",
        "run",
        "--base-dir", ".powerpacks",
        "--output-dir", str(merge_dir),
    )
    if not ledger.get("input", {}).get("include_existing_artifacts"):
        explicit_inputs = [
            value for key, value in sorted(ledger.get("artifacts", {}).items())
            if key in {"linkedin_people_csv", "gmail_people_csv"} and value
        ]
        for input_path in explicit_inputs:
            cmd.extend(["--input", str(input_path)])
    code, payload, stderr = run_cmd(cmd)
    if code != 0:
        mark_step(ledger, "merge", "failed", error=stderr or payload)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "merge", "error": stderr or payload})
        return False
    mark_step(ledger, "merge", "completed", payload=payload)
    ledger.setdefault("artifacts", {}).update({
        "merged_people_csv": payload.get("people_csv"),
        "network_contacts_csv": payload.get("network_contacts_csv"),
        "network_contact_sources_csv": payload.get("network_contact_sources_csv"),
        "network_companies_csv": payload.get("network_companies_csv"),
        "merge_manifest": payload.get("manifest"),
    })
    return True


def run_duckdb(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    merge_dir = Path(ledger["run_dir"]) / "merged"
    duckdb_dir = Path(ledger["run_dir"]) / "duckdb"
    cmd = py_cmd(
        "packs/ingestion/primitives/build_network_duckdb/build_network_duckdb.py",
        "--network-dir", str(merge_dir),
        "--output-dir", str(duckdb_dir),
        "--flavor", ledger["run_id"],
        "--force",
    )
    code, payload, stderr = run_cmd(cmd)
    if code != 0:
        mark_step(ledger, "duckdb", "failed", error=stderr or payload)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "duckdb", "error": stderr or payload})
        return False
    mark_step(ledger, "duckdb", "completed", payload=payload)
    ledger.setdefault("artifacts", {}).update({"duckdb": payload.get("duckdb"), "duckdb_manifest": payload.get("manifest")})
    return True


def run_pipeline(ledger_path: Path, *, resume: bool = False) -> int:
    ledger = load_ledger(ledger_path)
    if not resume or ledger.get("steps", {}).get("linkedin", {}).get("status") not in {"completed", "skipped"}:
        if not run_linkedin(ledger_path, ledger, "continue" if resume else "run"):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if ledger.get("steps", {}).get("gmail_msgvault", {}).get("status") not in {"completed", "skipped"}:
        if not run_gmail_msgvault(ledger_path, ledger):
            return 1
        save_ledger(ledger_path, ledger)
    if not run_merge(ledger_path, ledger):
        return 1
    save_ledger(ledger_path, ledger)
    if not run_duckdb(ledger_path, ledger):
        return 1
    ledger["status"] = "completed"
    ledger.pop("blocked", None)
    save_ledger(ledger_path, ledger)
    emit({"status": "completed", "ledger": str(ledger_path), "run_dir": ledger["run_dir"], "artifacts": ledger.get("artifacts", {})})
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    run_id = args.run_id or f"network-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    run_dir = DEFAULT_BASE_DIR / "network-runs" / run_id
    ledger_path = Path(args.ledger)
    if ledger_path.exists() and not args.force:
        existing = load_ledger(ledger_path)
        if existing.get("status") not in {"completed", "failed"}:
            emit({"status": "active_run_exists", "ledger": str(ledger_path), "message": "Use continue/approve or --force."})
            return 0
    ledger = {
        "primitive": "import_network_pipeline",
        "version": 1,
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "ledger": str(ledger_path),
        "input": {
            "operator_id": args.operator_id,
            "linkedin_csv": args.linkedin_csv,
            "linkedin_source_user": args.linkedin_source_user,
            "linkedin_limit": args.linkedin_limit,
            "msgvault_db": args.msgvault_db,
            "gmail_account_email": args.gmail_account_email,
            "gmail_limit": args.gmail_limit,
            "include_automated_gmail": args.include_automated_gmail,
            "include_existing_artifacts": args.include_existing_artifacts,
        },
        "steps": {},
        "artifacts": {},
    }
    save_ledger(ledger_path, ledger)
    return run_pipeline(ledger_path, resume=False)


def cmd_continue(args: argparse.Namespace) -> int:
    if not Path(args.ledger).exists():
        emit({"status": "missing_ledger", "ledger": args.ledger})
        return 2
    return run_pipeline(Path(args.ledger), resume=True)


def cmd_approve(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    ledger = load_ledger(ledger_path)
    blocked = ledger.get("blocked") or {}
    if blocked.get("step_id") == "linkedin" and blocked.get("child_ledger"):
        code, payload, stderr = run_cmd(py_cmd("packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py", "approve", "--ledger", blocked["child_ledger"]))
        if code != 0:
            emit({"status": "failed", "step_id": "approve", "error": stderr or payload})
            return 1
        ledger.pop("blocked", None)
        save_ledger(ledger_path, ledger)
        emit({"status": "approved", "ledger": str(ledger_path), "child": payload})
        return 0
    emit({"status": "no_pending_approval", "ledger": str(ledger_path)})
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    ledger = load_ledger(Path(args.ledger))
    emit({
        "status": ledger.get("status", "unknown"),
        "ledger": args.ledger,
        "run_dir": ledger.get("run_dir"),
        "blocked": ledger.get("blocked"),
        "steps": ledger.get("steps", {}),
        "artifacts": ledger.get("artifacts", {}),
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local network ingestion orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    run.add_argument("--run-id")
    run.add_argument("--operator-id", default="local")
    run.add_argument("--linkedin-csv", default="")
    run.add_argument("--linkedin-source-user", default="")
    run.add_argument("--linkedin-limit", type=int)
    run.add_argument("--msgvault-db", default=str(DEFAULT_MSGVAULT_DB))
    run.add_argument("--gmail-account-email", default="")
    run.add_argument("--gmail-limit", type=int)
    run.add_argument("--include-automated-gmail", action="store_true")
    run.add_argument("--include-existing-artifacts", action="store_true", help="Merge all discovered existing LinkedIn/Gmail/Twitter/message artifacts instead of only artifacts produced by this run")
    run.add_argument("--force", action="store_true")
    run.set_defaults(func=cmd_run)

    cont = sub.add_parser("continue")
    cont.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    cont.set_defaults(func=cmd_continue)

    approve = sub.add_parser("approve")
    approve.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    approve.set_defaults(func=cmd_approve)

    status = sub.add_parser("status")
    status.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.func(args)
    except KeyboardInterrupt:
        emit({"status": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
