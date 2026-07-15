#!/usr/bin/env python3
"""Import discovered Gmail contacts (directory-only by default).

Default mode is free and local: apply the shared identity directory to the
discovered Gmail queues, materialize `import/gmail/people.csv`, and write the
still-unresolved contacts to `import/gmail/candidates.csv` for the deep-setup
processing layer (which owns Parallel.ai resolution + RapidAPI enrichment).
`--resolve-legacy` restores the old in-import Parallel + RapidAPI behavior.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.schemas.candidates_schema import (
        CANDIDATES_SCHEMA_COLUMNS,
        candidate_key_for,
        normalize_candidate_row,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        DEFAULT_DIRECTORY_CSV,
        emit,
        now_iso,
        read_csv_rows,
        read_accounts,
        read_json,
        source_slug,
        write_csv_rows,
        write_json,
    )
    from packs.ingestion.primitives.import_contacts_pipeline.common import (
        DEFAULT_ACCOUNTS,
        DEFAULT_IMPORT_DIR,
        DEFAULT_PROFILE_CACHE_DIR,
        copy_people_csv,
        csv_count,
        directory_source_account_quality,
        import_manifest_current,
        linked_gmail_accounts,
        load_legacy_discover_module,
        normalize_directory_source_accounts,
        write_manifest,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.schemas.candidates_schema import (
        CANDIDATES_SCHEMA_COLUMNS,
        candidate_key_for,
        normalize_candidate_row,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        DEFAULT_DIRECTORY_CSV,
        emit,
        now_iso,
        read_csv_rows,
        read_accounts,
        read_json,
        source_slug,
        write_csv_rows,
        write_json,
    )
    from packs.ingestion.primitives.import_contacts_pipeline.common import (
        DEFAULT_ACCOUNTS,
        DEFAULT_IMPORT_DIR,
        DEFAULT_PROFILE_CACHE_DIR,
        copy_people_csv,
        csv_count,
        directory_source_account_quality,
        import_manifest_current,
        linked_gmail_accounts,
        load_legacy_discover_module,
        normalize_directory_source_accounts,
        write_manifest,
    )


GMAIL_PARALLEL_AUTO_APPROVE_UNDER = 25
GMAIL_IMPORT_CONTRACT = "gmail-directory-only-v2"


def _child_artifacts(child: dict[str, Any]) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    payload = child.get("payload") if isinstance(child.get("payload"), dict) else {}
    payload_artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    artifacts.update(payload_artifacts)
    direct_artifacts = child.get("artifacts") if isinstance(child.get("artifacts"), dict) else {}
    artifacts.update(direct_artifacts)
    if child.get("people_csv"):
        artifacts["people_csv"] = child.get("people_csv")
    if child.get("linkedin_resolution_queue_csv"):
        artifacts["linkedin_resolution_queue_csv"] = child.get("linkedin_resolution_queue_csv")
    return artifacts


def _valid_gmail_people_csv(path_text: Any) -> bool:
    path = Path(str(path_text or ""))
    if not path.exists() or not path.is_file():
        return False
    try:
        fields, _rows = read_csv_rows(path)
    except OSError:
        return False
    return "primary_email" in fields and "interaction_counts" in fields


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
    invalid_records: list[dict[str, Any]] = []
    for child in manifest.get("children") or []:
        if not isinstance(child, dict):
            continue
        account_email = str(child.get("account_email") or "")
        child_artifacts = _child_artifacts(child)
        queue_csv = child_artifacts.get("linkedin_resolution_queue_csv")
        people_csv = child_artifacts.get("people_csv")
        slug = source_slug(account_email or "gmail")
        valid_people = _valid_gmail_people_csv(people_csv)
        if valid_people:
            people_records.append({"account_email": account_email, "people_csv": people_csv, "slug": slug})
        elif people_csv:
            invalid_records.append({
                "account_email": account_email,
                "people_csv": people_csv,
                "queue_csv": queue_csv or "",
                "reason": "missing_people_schema_or_interaction_counts",
            })
        if queue_csv and Path(str(queue_csv)).exists() and valid_people:
            queue_records.append({
                "account_email": account_email,
                "queue_csv": queue_csv,
                "people_csv": people_csv,
                "slug": slug,
            })
    if queue_records:
        artifacts["gmail_linkedin_resolution_queue_csvs"] = queue_records
    if people_records:
        artifacts["gmail_people_records"] = people_records
    if invalid_records:
        artifacts["gmail_invalid_discovery_records"] = invalid_records
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


def queue_row_to_candidate(row: dict[str, str], *, cached_negative: bool) -> dict[str, str] | None:
    primary_email = (row.get("primary_email") or "").strip().lower()
    if not primary_email or "@" not in primary_email:
        return None
    total_messages = 0
    try:
        total_messages = int(float(row.get("total_messages") or 0))
    except (TypeError, ValueError):
        total_messages = 0
    evidence: dict[str, Any] = {
        "handle": (row.get("handle") or "").strip(),
        "account_emails": (row.get("account_emails") or "").strip(),
        "primary_email_type": (row.get("primary_email_type") or "").strip(),
        "thread_count": (row.get("thread_count") or "").strip(),
        "cached_negative": cached_negative,
    }
    candidate = {
        "candidate_key": candidate_key_for(primary_email, ""),
        "source": "gmail",
        "full_name": (row.get("full_name") or row.get("display_name") or "").strip(),
        "primary_email": primary_email,
        "all_emails": json.dumps([primary_email], ensure_ascii=False),
        "company_guess": (row.get("company_guess") or "").strip(),
        "interaction_counts": (
            json.dumps({"gmail": total_messages}, ensure_ascii=False) if total_messages else ""
        ),
        "last_interaction": (row.get("last_interaction") or "").strip(),
        "evidence": evidence,
    }
    return normalize_candidate_row(candidate)


def write_gmail_candidates(artifacts: dict[str, Any], import_dir: Path) -> dict[str, Any]:
    """Union the post-directory unresolved (+ cached-negative, flagged) queues
    into import/gmail/candidates.csv for the deep-setup processing layer."""
    candidates_csv = import_dir / "candidates.csv"
    by_key: dict[str, dict[str, str]] = {}
    skipped = {"no_email": 0, "duplicate_email": 0}
    groups = (
        (artifacts.get("gmail_unresolved_linkedin_resolution_queue_csvs") or [], False),
        (artifacts.get("gmail_cached_negative_linkedin_resolution_queue_csvs") or [], True),
    )
    for records, cached_negative in groups:
        for record in records:
            if not isinstance(record, dict) or not record.get("queue_csv"):
                continue
            queue_path = Path(str(record["queue_csv"]))
            if not queue_path.exists():
                continue
            for row in read_csv_rows(queue_path)[1]:
                candidate = queue_row_to_candidate(row, cached_negative=cached_negative)
                if candidate is None:
                    skipped["no_email"] += 1
                    continue
                key = candidate.get("candidate_key", "")
                if not key:
                    skipped["no_email"] += 1
                    continue
                if key in by_key:
                    skipped["duplicate_email"] += 1
                    continue
                by_key[key] = candidate
    rows = [by_key[key] for key in sorted(by_key)]
    write_csv_rows(candidates_csv, CANDIDATES_SCHEMA_COLUMNS, rows)
    return {
        "candidates_csv": str(candidates_csv),
        "candidates": len(rows),
        "skipped": skipped,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    resolve_legacy = bool(getattr(args, "resolve_legacy", False))
    expected_input = {
        "pipeline_contract": GMAIL_IMPORT_CONTRACT,
        "mode": "resolve-legacy" if resolve_legacy else "directory-only",
    }
    current = import_manifest_current("gmail", expected_input, import_dir=DEFAULT_IMPORT_DIR)
    if current and not getattr(args, "force", False):
        return current
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
            # Directory-only default: the Parallel stage self-skips when
            # resolve_gmail_linkedin is falsy, and enrich_resolved=False keeps
            # apply-resolutions free of RapidAPI hydration. deep-setup owns both.
            "resolve_gmail_linkedin": resolve_legacy,
            "enrich_resolved": resolve_legacy,
            "approve_parallel_spend": bool(args.approve_parallel_spend),
            "linkedin_directory_csv": str(DEFAULT_DIRECTORY_CSV),
            "profile_cache_dir": str(DEFAULT_PROFILE_CACHE_DIR),
        },
        "steps": {},
        "artifacts": gmail_artifacts_from_discovery(),
    }
    if not ledger["artifacts"].get("gmail_linkedin_resolution_queue_csvs"):
        reason = "no Gmail discovery queue"
        status = "skipped"
        if ledger["artifacts"].get("gmail_linkedin_resolution_queue_csv") or ledger["artifacts"].get("gmail_invalid_discovery_records"):
            reason = "gmail_discovery_missing_per_account_people_csv"
        return write_manifest("gmail", {
            "status": status,
            "reason": reason,
            "artifact_dir": str(import_dir),
            "artifacts": ledger.get("artifacts", {}),
        }, import_dir=DEFAULT_IMPORT_DIR)
    write_json(ledger_path, ledger)
    for func_name in ("run_gmail_directory", "run_gmail_linkedin_resolution", "run_gmail_apply_and_enrich"):
        if resolve_legacy and func_name == "run_gmail_linkedin_resolution" and not ledger.get("input", {}).get("approve_parallel_spend"):
            contacts = pending_gmail_parallel_contacts(ledger)
            if 0 < contacts < GMAIL_PARALLEL_AUTO_APPROVE_UNDER:
                auto_approve_gmail_parallel(ledger, contacts, "gmail_parallel_queue_below_threshold")
                legacy.save_ledger(ledger_path, ledger)
        ok = getattr(legacy, func_name)(ledger_path, ledger)
        if not ok and resolve_legacy and func_name == "run_gmail_linkedin_resolution" and not ledger.get("input", {}).get("approve_parallel_spend"):
            contacts = blocked_parallel_contacts(ledger)
            if 0 < contacts < GMAIL_PARALLEL_AUTO_APPROVE_UNDER:
                ledger.pop("blocked", None)
                auto_approve_gmail_parallel(ledger, contacts, "gmail_parallel_child_count_below_threshold")
                legacy.save_ledger(ledger_path, ledger)
                ok = getattr(legacy, func_name)(ledger_path, ledger)
        if not ok:
            status = "blocked_approval" if ledger.get("blocked") else "failed"
            return write_manifest("gmail", {
                "status": status,
                "ledger": str(ledger_path),
                "artifact_dir": str(import_dir),
                "blocked": ledger.get("blocked"),
                "steps": ledger.get("steps", {}),
                "artifacts": ledger.get("artifacts", {}),
            }, import_dir=DEFAULT_IMPORT_DIR)
        legacy.save_ledger(ledger_path, ledger)
    ledger["status"] = "completed"
    legacy.save_ledger(ledger_path, ledger)
    people_csv = copy_people_csv("gmail", str(ledger.get("artifacts", {}).get("gmail_merged_people_csv") or ledger.get("artifacts", {}).get("gmail_people_csv") or ""), import_dir=DEFAULT_IMPORT_DIR)
    candidates = write_gmail_candidates(ledger.get("artifacts", {}), import_dir)
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
        }, import_dir=DEFAULT_IMPORT_DIR)
    return write_manifest("gmail", {
        "status": "completed",
        "ledger": str(ledger_path),
        "artifact_dir": str(import_dir),
        "input": {
            **expected_input,
            "discovery_manifest": str(DEFAULT_BASE_DIR / "discover" / "gmail" / "manifest.json"),
            "contacts_csv": str(DEFAULT_BASE_DIR / "discover" / "gmail" / "contacts.csv"),
            "linkedin_resolution_queue_csv": str(DEFAULT_BASE_DIR / "discover" / "gmail" / "linkedin_resolution_queue.csv"),
        },
        "outputs": {
            "people_csv": people_csv,
            "candidates_csv": candidates["candidates_csv"],
            "directory_csv": str(DEFAULT_DIRECTORY_CSV),
        },
        "stats": {
            "people": csv_count(people_csv),
            "candidates": candidates["candidates"],
        },
        "candidates": candidates,
        "steps": ledger.get("steps", {}),
        "auto_approvals": ledger.get("auto_approvals", []),
        "directory_normalization": directory_normalization,
        "directory_quality": directory_quality,
        "artifacts": ledger.get("artifacts", {}),
    }, import_dir=DEFAULT_IMPORT_DIR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import discovered Gmail contacts (directory-only by default)")
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument("--operator-id", default="local")
    parser.add_argument(
        "--resolve-legacy", action="store_true",
        help="Legacy in-import identity resolution: Parallel.ai + RapidAPI enrichment (deep-setup owns this now)",
    )
    parser.add_argument("--approve-parallel-spend", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-run even if the import manifest is current (no no-op skip)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = run(args)
    emit(payload)
    return 20 if payload.get("status") == "blocked_approval" else 1 if payload.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
