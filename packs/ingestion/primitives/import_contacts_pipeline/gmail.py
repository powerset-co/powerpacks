#!/usr/bin/env python3
"""Import/enrich discovered Gmail contacts."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        DEFAULT_DIRECTORY_CSV,
        emit,
        now_iso,
        read_accounts,
        read_json,
        source_slug,
        write_json,
    )
    from packs.ingestion.primitives.import_contacts_pipeline.common import (
        DEFAULT_ACCOUNTS,
        DEFAULT_IMPORT_DIR,
        DEFAULT_PROFILE_CACHE_DIR,
        copy_people_csv,
        csv_count,
        directory_source_account_quality,
        linked_gmail_accounts,
        load_legacy_discover_module,
        normalize_directory_source_accounts,
        write_manifest,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        DEFAULT_DIRECTORY_CSV,
        emit,
        now_iso,
        read_accounts,
        read_json,
        source_slug,
        write_json,
    )
    from packs.ingestion.primitives.import_contacts_pipeline.common import (
        DEFAULT_ACCOUNTS,
        DEFAULT_IMPORT_DIR,
        DEFAULT_PROFILE_CACHE_DIR,
        copy_people_csv,
        csv_count,
        directory_source_account_quality,
        linked_gmail_accounts,
        load_legacy_discover_module,
        normalize_directory_source_accounts,
        write_manifest,
    )


GMAIL_PARALLEL_AUTO_APPROVE_UNDER = 25


def _paid_checkpoint_every(default: int = 25) -> int:
    try:
        value = int(os.environ.get("POWERPACKS_PAID_CHECKPOINT_EVERY", str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def gmail_artifacts_from_discovery() -> dict[str, Any]:
    manifest = read_json(DEFAULT_BASE_DIR / "discover" / "gmail" / "manifest.json", {}) or {}
    artifacts: dict[str, Any] = {}
    contacts_csv = str(manifest.get("contacts_csv") or DEFAULT_BASE_DIR / "discover" / "gmail" / "contacts.csv")
    stable_queue_csv = str(manifest.get("linkedin_resolution_queue_csv") or DEFAULT_BASE_DIR / "discover" / "gmail" / "linkedin_resolution_queue.csv")
    if Path(contacts_csv).exists():
        artifacts["gmail_contacts_csv"] = contacts_csv
    if Path(stable_queue_csv).exists():
        artifacts["gmail_linkedin_resolution_queue_csv"] = stable_queue_csv
    queue_records: list[dict[str, Any]] = []
    people_records: list[dict[str, Any]] = []
    for child in manifest.get("children") or []:
        if not isinstance(child, dict):
            continue
        account_email = str(child.get("account_email") or "")
        payload = child.get("payload") if isinstance(child.get("payload"), dict) else {}
        child_artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
        queue_csv = child_artifacts.get("linkedin_resolution_queue_csv")
        people_csv = child_artifacts.get("people_csv")
        slug = source_slug(account_email or "gmail")
        if people_csv and Path(str(people_csv)).exists():
            people_records.append({"account_email": account_email, "people_csv": people_csv, "slug": slug})
        if queue_csv and Path(str(queue_csv)).exists():
            queue_records.append({
                "account_email": account_email,
                "queue_csv": queue_csv,
                "people_csv": people_csv if people_csv and Path(str(people_csv)).exists() else "",
                "slug": slug,
            })
    if Path(stable_queue_csv).exists():
        queue_records = [{
            "account_email": "",
            "queue_csv": stable_queue_csv,
            "people_csv": contacts_csv if Path(contacts_csv).exists() else people_records[0]["people_csv"] if people_records else "",
            "slug": "all",
        }]
    if queue_records:
        artifacts["gmail_linkedin_resolution_queue_csvs"] = queue_records
    if people_records:
        artifacts["gmail_people_records"] = people_records
    return artifacts


def _sum_queue_rows(records: Any) -> int:
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        return 0
    total = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        total += csv_count(str(record.get("queue_csv") or ""))
    return total


def pending_gmail_parallel_contacts(ledger: dict[str, Any]) -> int:
    artifacts = ledger.get("artifacts") if isinstance(ledger.get("artifacts"), dict) else {}
    unresolved = artifacts.get("gmail_unresolved_linkedin_resolution_queue_csvs")
    if unresolved is not None:
        return _sum_queue_rows(unresolved)
    queue_records = artifacts.get("gmail_linkedin_resolution_queue_csvs")
    if queue_records is not None:
        return _sum_queue_rows(queue_records)
    return csv_count(str(artifacts.get("gmail_linkedin_resolution_queue_csv") or ""))


def blocked_parallel_contacts(ledger: dict[str, Any]) -> int:
    blocked = ledger.get("blocked") if isinstance(ledger.get("blocked"), dict) else {}
    child = blocked.get("child") if isinstance(blocked.get("child"), dict) else {}
    for key in ("contacts", "would_submit"):
        try:
            value = int(child.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    payload = child.get("payload") if isinstance(child.get("payload"), dict) else {}
    try:
        return int(payload.get("would_submit") or payload.get("contacts") or 0)
    except (TypeError, ValueError):
        return 0


def auto_approve_gmail_parallel(ledger: dict[str, Any], contacts: int, reason: str) -> None:
    input_cfg = ledger.setdefault("input", {})
    input_cfg["approve_parallel_spend"] = True
    ledger.setdefault("auto_approvals", []).append({
        "approval_type": "parallel",
        "contacts": contacts,
        "threshold": GMAIL_PARALLEL_AUTO_APPROVE_UNDER,
        "reason": reason,
        "approved_at": now_iso(),
    })


def run(args: argparse.Namespace) -> dict[str, Any]:
    accounts = read_accounts(args.accounts)
    legacy = load_legacy_discover_module()
    import_dir = DEFAULT_IMPORT_DIR / "gmail"
    ledger_path = import_dir / "ledger.json"
    emails = linked_gmail_accounts(accounts)
    ledger = {
        "primitive": "import_contacts_gmail",
        "source": "gmail",
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "artifact_dir": str(import_dir),
        "input": {
            "operator_id": args.operator_id,
            "from_accounts": str(args.accounts),
            "gmail_account_emails": emails,
            "resolve_gmail_linkedin": True,
            "approve_parallel_spend": bool(args.approve_parallel_spend),
            "linkedin_directory_csv": str(DEFAULT_DIRECTORY_CSV),
            "profile_cache_dir": str(DEFAULT_PROFILE_CACHE_DIR),
        },
        "steps": {},
        "artifacts": gmail_artifacts_from_discovery(),
    }
    if not ledger["artifacts"].get("gmail_linkedin_resolution_queue_csvs") and not ledger["artifacts"].get("gmail_linkedin_resolution_queue_csv"):
        return write_manifest("gmail", {"status": "skipped", "reason": "no Gmail discovery queue", "artifact_dir": str(import_dir)})
    write_json(ledger_path, ledger)
    checkpoint_every = _paid_checkpoint_every()
    run_started = now_iso()
    t0 = time.monotonic()
    parallel_enrichment_seconds = 0.0
    for func_name in ("run_gmail_directory", "run_gmail_linkedin_resolution", "run_gmail_apply_and_enrich"):
        is_resolution = func_name == "run_gmail_linkedin_resolution"
        if is_resolution and not ledger.get("input", {}).get("approve_parallel_spend"):
            contacts = pending_gmail_parallel_contacts(ledger)
            if 0 < contacts < GMAIL_PARALLEL_AUTO_APPROVE_UNDER:
                auto_approve_gmail_parallel(ledger, contacts, "gmail_parallel_queue_below_threshold")
                legacy.save_ledger(ledger_path, ledger)
        if is_resolution:
            _resolution_t0 = time.monotonic()
            ok = getattr(legacy, func_name)(ledger_path, ledger)
            parallel_enrichment_seconds += time.monotonic() - _resolution_t0
        else:
            ok = getattr(legacy, func_name)(ledger_path, ledger)
        if not ok and is_resolution and not ledger.get("input", {}).get("approve_parallel_spend"):
            contacts = blocked_parallel_contacts(ledger)
            if 0 < contacts < GMAIL_PARALLEL_AUTO_APPROVE_UNDER:
                ledger.pop("blocked", None)
                auto_approve_gmail_parallel(ledger, contacts, "gmail_parallel_child_count_below_threshold")
                legacy.save_ledger(ledger_path, ledger)
                _resolution_t0 = time.monotonic()
                ok = getattr(legacy, func_name)(ledger_path, ledger)
                parallel_enrichment_seconds += time.monotonic() - _resolution_t0
        if not ok:
            status = "blocked_approval" if ledger.get("blocked") else "failed"
            return write_manifest("gmail", {
                "status": status,
                "ledger": str(ledger_path),
                "artifact_dir": str(import_dir),
                "blocked": ledger.get("blocked"),
                "steps": ledger.get("steps", {}),
                "artifacts": ledger.get("artifacts", {}),
            })
        legacy.save_ledger(ledger_path, ledger)
    ledger["status"] = "completed"
    legacy.save_ledger(ledger_path, ledger)
    people_csv = copy_people_csv("gmail", str(ledger.get("artifacts", {}).get("gmail_merged_people_csv") or ledger.get("artifacts", {}).get("gmail_people_csv") or ""))
    directory_normalization = normalize_directory_source_accounts("gmail")
    directory_quality = directory_source_account_quality("gmail")
    if directory_quality["status"] != "ok":
        return write_manifest("gmail", {
            "status": "failed",
            "reason": "directory_source_account_quality_failed",
            "ledger": str(ledger_path),
            "artifact_dir": str(import_dir),
            "outputs": {
                "people_csv": people_csv,
                "directory_csv": str(DEFAULT_DIRECTORY_CSV),
            },
            "directory_normalization": directory_normalization,
            "directory_quality": directory_quality,
            "steps": ledger.get("steps", {}),
            "auto_approvals": ledger.get("auto_approvals", []),
            "artifacts": ledger.get("artifacts", {}),
        })
    return write_manifest("gmail", {
        "status": "completed",
        "ledger": str(ledger_path),
        "artifact_dir": str(import_dir),
        "started_at": run_started,
        "duration_seconds": round(time.monotonic() - t0, 3),
        "parallel_enrichment_seconds": round(parallel_enrichment_seconds, 3),
        "checkpoint_every": checkpoint_every,
        "input": {
            "discovery_manifest": str(DEFAULT_BASE_DIR / "discover" / "gmail" / "manifest.json"),
            "contacts_csv": str(DEFAULT_BASE_DIR / "discover" / "gmail" / "contacts.csv"),
            "linkedin_resolution_queue_csv": str(DEFAULT_BASE_DIR / "discover" / "gmail" / "linkedin_resolution_queue.csv"),
        },
        "outputs": {
            "people_csv": people_csv,
            "directory_csv": str(DEFAULT_DIRECTORY_CSV),
        },
        "stats": {
            "people": csv_count(people_csv),
            "candidates": csv_count(str(DEFAULT_BASE_DIR / "discover" / "gmail" / "linkedin_resolution_queue.csv")),
        },
        "steps": ledger.get("steps", {}),
        "auto_approvals": ledger.get("auto_approvals", []),
        "directory_normalization": directory_normalization,
        "directory_quality": directory_quality,
        "artifacts": ledger.get("artifacts", {}),
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import/enrich discovered Gmail contacts")
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument("--operator-id", default="local")
    parser.add_argument("--approve-parallel-spend", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = run(args)
    emit(payload)
    return 20 if payload.get("status") == "blocked_approval" else 1 if payload.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
