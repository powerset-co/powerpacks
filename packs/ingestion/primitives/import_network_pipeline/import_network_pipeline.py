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
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LEDGER = Path(".powerpacks/network-import/import-network-run.json")
DEFAULT_BASE_DIR = Path(".powerpacks/network-import")
DEFAULT_MSGVAULT_DB = Path.home() / ".msgvault" / "msgvault.db"
DEFAULT_CHILD_TIMEOUT_SECONDS = int(os.environ.get("POWERPACKS_IMPORT_NETWORK_CHILD_TIMEOUT_SECONDS", str(6 * 60 * 60)))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def emit_progress(message: str) -> None:
    print(f"[import-network] {message}", file=sys.stderr, flush=True)


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


def collect_artifact_paths(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            paths.extend(collect_artifact_paths(item))
    elif isinstance(value, list):
        for item in value:
            paths.extend(collect_artifact_paths(item))
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith(".powerpacks/") or text.startswith("/"):
            paths.append(text)
    return paths


def check_artifact_paths(ledger: dict[str, Any]) -> dict[str, Any]:
    seen: set[str] = set()
    existing = 0
    missing: list[str] = []
    for path_text in collect_artifact_paths({"artifacts": ledger.get("artifacts", {}), "steps": ledger.get("steps", {})}):
        if path_text in seen:
            continue
        seen.add(path_text)
        path = Path(path_text)
        if path.exists():
            existing += 1
        else:
            missing.append(path_text)
    return {"checked": len(seen), "existing": existing, "missing": missing[:50], "missing_count": len(missing)}


def resolve_msgvault_db(args: argparse.Namespace) -> str:
    explicit = str(getattr(args, "msgvault_db", "") or "").strip()
    if explicit:
        return explicit
    if str(getattr(args, "gmail_account_email", "") or "").strip():
        return str(DEFAULT_MSGVAULT_DB)
    return ""


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


def run_cmd(cmd: list[str], *, timeout: int | None = None) -> tuple[int, dict[str, Any], str]:
    effective_timeout = DEFAULT_CHILD_TIMEOUT_SECONDS if timeout is None else timeout
    proc = subprocess.Popen(
        cmd,
        cwd=Path(__file__).resolve().parents[4],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def read_stdout() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            stdout_chunks.append(line)

    def read_stderr() -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            stderr_chunks.append(line)
            sys.stderr.write(line)
            sys.stderr.flush()

    threads = [
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=read_stderr, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        code = proc.wait(timeout=effective_timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
        timeout_message = f"child command timed out after {effective_timeout} seconds: {' '.join(cmd)}"
        stderr_chunks.append(timeout_message + "\n")
        emit_progress(timeout_message)
    for thread in threads:
        thread.join(timeout=1)
    return code, parse_last_json("".join(stdout_chunks)), "".join(stderr_chunks)


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


def begin_step(ledger_path: Path, ledger: dict[str, Any], step: str, message: str) -> None:
    mark_step(ledger, step, "running")
    save_ledger(ledger_path, ledger)
    emit_progress(message)


def run_linkedin(ledger_path: Path, ledger: dict[str, Any], mode: str) -> bool:
    input_cfg = ledger.get("input", {})
    if not input_cfg.get("linkedin_csv"):
        mark_step(ledger, "linkedin", "skipped", reason="no --linkedin-csv")
        return True
    begin_step(ledger_path, ledger, "linkedin", "Importing LinkedIn CSV and enriching profiles.")
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
    emit_progress("LinkedIn import completed.")
    return True


def run_gmail_msgvault(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    input_cfg = ledger.get("input", {})
    if not input_cfg.get("gmail_account_email") and not input_cfg.get("msgvault_db"):
        mark_step(ledger, "gmail_msgvault", "skipped", reason="no --gmail-account-email/--msgvault-db")
        return True
    begin_step(ledger_path, ledger, "gmail_msgvault", "Importing Gmail metadata from msgvault.")
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
    counts = payload.get("counts") or {}
    if counts:
        emit_progress(f"Gmail metadata import completed: {counts.get('contacts_written', 0)} contacts from {counts.get('contacts_seen', 0)} seen.")
    else:
        emit_progress("Gmail metadata import completed.")
    return True


def run_gmail_linkedin_resolution(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    input_cfg = ledger.get("input", {})
    provider = input_cfg.get("gmail_linkedin_provider") or "off"
    queue = ledger.get("artifacts", {}).get("gmail_linkedin_resolution_queue_csv")
    if provider == "off" or not queue:
        mark_step(ledger, "gmail_linkedin_resolution", "skipped", reason="provider off or no queue")
        return True
    begin_step(ledger_path, ledger, "gmail_linkedin_resolution", "Resolving Gmail contacts to LinkedIn.")
    child_ledger = Path(ledger["run_dir"]) / "gmail-linkedin-resolution.ledger.json"
    out_dir = Path(ledger["run_dir"]) / "gmail-linkedin-resolution"
    cmd = py_cmd(
        "packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py",
        "run",
        "--provider", provider,
        "--input", str(queue),
        "--output-dir", str(out_dir),
        "--ledger", str(child_ledger),
    )
    if input_cfg.get("gmail_linkedin_limit") is not None:
        cmd.extend(["--limit", str(input_cfg["gmail_linkedin_limit"])])
    code, payload, stderr = run_cmd(cmd)
    ledger.setdefault("artifacts", {})["gmail_linkedin_resolution_ledger"] = str(child_ledger)
    if code == 20 or payload.get("status") == "blocked_approval":
        ledger["blocked"] = {"step_id": "gmail_linkedin_resolution", "child_ledger": str(child_ledger), "child": payload}
        mark_step(ledger, "gmail_linkedin_resolution", "blocked", payload=payload)
        save_ledger(ledger_path, ledger)
        emit({"status": "blocked_approval", "step_id": "gmail_linkedin_resolution", "ledger": str(ledger_path), "child": payload})
        return False
    if code != 0:
        mark_step(ledger, "gmail_linkedin_resolution", "failed", error=stderr or payload)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "gmail_linkedin_resolution", "error": stderr or payload})
        return False
    mark_step(ledger, "gmail_linkedin_resolution", "completed", payload=payload)
    if payload.get("output"):
        ledger.setdefault("artifacts", {})["gmail_linkedin_resolutions_csv"] = payload.get("output")
    if payload.get("prompts_jsonl"):
        ledger.setdefault("artifacts", {})["gmail_linkedin_harness_prompts_jsonl"] = payload.get("prompts_jsonl")
    if payload.get("instructions"):
        ledger.setdefault("artifacts", {})["gmail_linkedin_harness_instructions"] = payload.get("instructions")
    emit_progress("Gmail LinkedIn resolution completed.")
    return True


def run_gmail_apply_and_enrich(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    input_cfg = ledger.get("input", {})
    resolutions = input_cfg.get("gmail_resolutions_csv") or ledger.get("artifacts", {}).get("gmail_linkedin_resolutions_csv")
    people = ledger.get("artifacts", {}).get("gmail_people_csv")
    if not resolutions or not people:
        mark_step(ledger, "gmail_apply_enrich", "skipped", reason="no gmail resolutions")
        return True
    begin_step(ledger_path, ledger, "gmail_apply_enrich", "Applying Gmail LinkedIn matches.")
    apply_cmd = py_cmd(
        "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py",
        "apply-resolutions",
        "--people-csv", str(people),
        "--resolutions-csv", str(resolutions),
        "--output-dir", str(DEFAULT_BASE_DIR),
        "--run-id", f"{ledger['run_id']}-gmail-resolved",
    )
    code, payload, stderr = run_cmd(apply_cmd)
    if code != 0:
        mark_step(ledger, "gmail_apply_enrich", "failed", error=stderr or payload)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "gmail_apply_enrich", "error": stderr or payload})
        return False
    ledger.setdefault("artifacts", {})["gmail_resolved_people_csv"] = payload.get("people_csv")
    if int(payload.get("resolved") or 0) <= 0:
        mark_step(ledger, "gmail_apply_enrich", "completed", payload=payload, reason="no resolved LinkedIns")
        emit_progress("Gmail LinkedIn matches applied; no profile enrichment needed.")
        return True
    emit_progress(f"Enriching {payload.get('resolved')} resolved Gmail LinkedIn profiles.")
    child_ledger = Path(ledger["run_dir"]) / "gmail-enrich-people.ledger.json"
    enrich_cmd = py_cmd(
        "packs/ingestion/primitives/enrich_people/enrich_people.py",
        "run",
        "--input", str(payload.get("people_csv")),
        "--ledger", str(child_ledger),
        "--run-id", f"{ledger['run_id']}-gmail-enrich",
    )
    code, enrich_payload, stderr = run_cmd(enrich_cmd)
    ledger.setdefault("artifacts", {})["gmail_enrich_people_ledger"] = str(child_ledger)
    if code == 20 or enrich_payload.get("status") == "blocked_approval":
        ledger["blocked"] = {"step_id": "gmail_apply_enrich", "child_ledger": str(child_ledger), "child": enrich_payload}
        mark_step(ledger, "gmail_apply_enrich", "blocked", payload=enrich_payload)
        save_ledger(ledger_path, ledger)
        emit({"status": "blocked_approval", "step_id": "gmail_apply_enrich", "ledger": str(ledger_path), "child": enrich_payload})
        return False
    if code != 0:
        mark_step(ledger, "gmail_apply_enrich", "failed", error=stderr or enrich_payload)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "gmail_apply_enrich", "error": stderr or enrich_payload})
        return False
    for key, value in (enrich_payload.get("artifacts") or {}).items():
        ledger.setdefault("artifacts", {})[f"gmail_enriched_{key}"] = value
    if enrich_payload.get("artifacts", {}).get("people_csv"):
        ledger.setdefault("artifacts", {})["gmail_people_csv"] = enrich_payload["artifacts"]["people_csv"]
    mark_step(ledger, "gmail_apply_enrich", "completed", payload={"apply": payload, "enrich": enrich_payload})
    emit_progress("Gmail LinkedIn enrichment completed.")
    return True


def run_merge(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    begin_step(ledger_path, ledger, "merge", "Merging network sources.")
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
    emit_progress(f"Merged network sources: {payload.get('merged_rows', 0)} people.")
    return True


def run_duckdb(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    begin_step(ledger_path, ledger, "duckdb", "Building local network DuckDB.")
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
    emit_progress("Local network DuckDB is ready.")
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
    if ledger.get("steps", {}).get("gmail_linkedin_resolution", {}).get("status") not in {"completed", "skipped"}:
        if not run_gmail_linkedin_resolution(ledger_path, ledger):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if ledger.get("steps", {}).get("gmail_apply_enrich", {}).get("status") not in {"completed", "skipped"}:
        if not run_gmail_apply_and_enrich(ledger_path, ledger):
            return 20 if ledger.get("blocked") else 1
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
    if args.dry_run or args.estimate:
        emit(dry_run_plan(args, ledger_path, run_id, run_dir))
        return 0
    if ledger_path.exists() and not args.force:
        existing = load_ledger(ledger_path)
        if existing.get("status") == "completed":
            emit({
                "status": "completed",
                "cached": True,
                "ledger": str(ledger_path),
                "run_dir": existing.get("run_dir"),
                "message": "Existing completed import-network run found; no work was run.",
                "artifact_check": check_artifact_paths(existing),
                "artifacts": existing.get("artifacts", {}),
            })
            return 0
        if existing.get("status") not in {"failed"}:
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
            "msgvault_db": resolve_msgvault_db(args),
            "gmail_account_email": args.gmail_account_email,
            "gmail_limit": args.gmail_limit,
            "include_automated_gmail": args.include_automated_gmail,
            "gmail_linkedin_provider": args.gmail_linkedin_provider,
            "gmail_linkedin_limit": args.gmail_linkedin_limit,
            "gmail_resolutions_csv": args.gmail_resolutions_csv,
            "include_existing_artifacts": args.include_existing_artifacts,
        },
        "steps": {},
        "artifacts": {},
    }
    save_ledger(ledger_path, ledger)
    return run_pipeline(ledger_path, resume=False)


def dry_run_plan(args: argparse.Namespace, ledger_path: Path, run_id: str, run_dir: Path) -> dict[str, Any]:
    if ledger_path.exists():
        ledger = load_ledger(ledger_path)
        steps = ledger.get("steps", {}) or {}
        would_run = [
            step for step in ["linkedin", "gmail_msgvault", "gmail_linkedin_resolution", "gmail_apply_enrich", "merge", "duckdb"]
            if (steps.get(step) or {}).get("status") not in {"completed", "skipped"}
        ]
        child_paid = {}
        for name, path_text in (ledger.get("artifacts") or {}).items():
            if not name.endswith("ledger") or not path_text:
                continue
            child = read_json(Path(path_text), {}) or {}
            if "paid_call_count" in child:
                child_paid[name] = child.get("paid_call_count", 0)
        return {
            "status": "dry_run",
            "ledger": str(ledger_path),
            "run_id": ledger.get("run_id") or run_id,
            "run_dir": ledger.get("run_dir") or str(run_dir),
            "existing_status": ledger.get("status", "unknown"),
            "would_run_steps": would_run,
            "estimated_paid_calls": 0 if not would_run else "unknown_without_running_child_stage_plans",
            "child_paid_call_counts": child_paid,
            "artifact_check": check_artifact_paths(ledger),
        }
    would_run = []
    if args.linkedin_csv:
        would_run.append("linkedin")
    if args.gmail_account_email or resolve_msgvault_db(args):
        would_run.append("gmail_msgvault")
    if args.gmail_linkedin_provider != "off":
        would_run.append("gmail_linkedin_resolution")
    if args.gmail_resolutions_csv:
        would_run.append("gmail_apply_enrich")
    would_run.extend(["merge", "duckdb"])
    return {
        "status": "dry_run",
        "ledger": str(ledger_path),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "existing_status": "missing",
        "would_run_steps": would_run,
        "estimated_paid_calls": "unknown_without_existing_stage_outputs",
        "message": "No existing import-network ledger was found; running would execute the listed stages until any child approval gate.",
    }


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
    if blocked.get("step_id") == "gmail_linkedin_resolution" and blocked.get("child_ledger"):
        code, payload, stderr = run_cmd(py_cmd("packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py", "approve", "--ledger", blocked["child_ledger"]))
        if code != 0:
            emit({"status": "failed", "step_id": "approve", "error": stderr or payload})
            return 1
        ledger.pop("blocked", None)
        save_ledger(ledger_path, ledger)
        emit({"status": "approved", "ledger": str(ledger_path), "child": payload})
        return 0
    if blocked.get("step_id") == "gmail_apply_enrich" and blocked.get("child_ledger"):
        code, payload, stderr = run_cmd(py_cmd("packs/ingestion/primitives/enrich_people/enrich_people.py", "approve", "--ledger", blocked["child_ledger"]))
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
    run.add_argument("--msgvault-db", default="", help=f"msgvault SQLite DB; defaults to {DEFAULT_MSGVAULT_DB} when --gmail-account-email is set")
    run.add_argument("--gmail-account-email", default="")
    run.add_argument("--gmail-limit", type=int)
    run.add_argument("--include-automated-gmail", action="store_true")
    run.add_argument("--gmail-linkedin-provider", choices=["off", "harness", "parallel"], default="off", help="Prepare/run Gmail email-to-LinkedIn resolution before merge. harness is local prompt prep; parallel is spend-bearing and approval-gated.")
    run.add_argument("--gmail-linkedin-limit", type=int, help=argparse.SUPPRESS)
    run.add_argument("--gmail-resolutions-csv", default="", help="Existing linkedin_resolutions.csv to apply to Gmail people before shared enrich_people")
    run.add_argument("--include-existing-artifacts", action="store_true", help="Merge all discovered existing LinkedIn/Gmail/Twitter/message artifacts instead of only artifacts produced by this run")
    run.add_argument("--dry-run", action="store_true", help="Inspect existing ledger/stage outputs and report work that would run")
    run.add_argument("--estimate", action="store_true", help="Alias for --dry-run")
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
