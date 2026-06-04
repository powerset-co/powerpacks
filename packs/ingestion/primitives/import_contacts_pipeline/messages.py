#!/usr/bin/env python3
"""Import/enrich reviewed Messages contacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        DEFAULT_DIRECTORY_CSV,
        emit,
        now_iso,
        read_csv_rows,
        read_accounts,
        write_json,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.directory import (
        directory_rows_from_people_csv,
        normalized_directory_row,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline import messages as messages_helpers
    from packs.ingestion.primitives.import_contacts_pipeline.common import (
        DEFAULT_ACCOUNTS,
        DEFAULT_IMPORT_DIR,
        copy_people_csv,
        csv_count,
        directory_source_account_quality,
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
        read_csv_rows,
        read_accounts,
        write_json,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.directory import (
        directory_rows_from_people_csv,
        normalized_directory_row,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline import messages as messages_helpers
    from packs.ingestion.primitives.import_contacts_pipeline.common import (
        DEFAULT_ACCOUNTS,
        DEFAULT_IMPORT_DIR,
        copy_people_csv,
        csv_count,
        directory_source_account_quality,
        normalize_directory_source_accounts,
        write_manifest,
    )


def directory_source_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    for row in read_csv_rows(path)[1]:
        normalized = normalized_directory_row(row, source="directory")
        if normalized.get("source_key"):
            keys.add(normalized["source_key"])
    return keys


def messages_import_diff(review_csv: Path) -> dict:
    import_dir = DEFAULT_IMPORT_DIR / "messages"
    preview_dir = import_dir / "messages"
    input_people = preview_dir / "people.input.csv"
    manifest_path = preview_dir / "people_manifest.json"
    materialized = messages_helpers.materialize_messages_review_people(review_csv, input_people, manifest_path)
    people_csv = Path(str(materialized.get("people_csv") or ""))
    candidate_rows = directory_rows_from_people_csv(people_csv, source="messages") if people_csv.exists() else []
    existing_keys = directory_source_keys(DEFAULT_DIRECTORY_CSV)
    new_rows = [row for row in candidate_rows if row.get("source_key") and row["source_key"] not in existing_keys]
    return {
        "materialized": materialized,
        "candidate_rows": len(candidate_rows),
        "new_rows": len(new_rows),
        "existing_directory_rows": len(existing_keys),
        "people_input_csv": str(people_csv) if people_csv.exists() else "",
        "people_input_manifest": str(manifest_path),
    }


def run(args: argparse.Namespace) -> dict:
    read_accounts(args.accounts)
    import_dir = DEFAULT_IMPORT_DIR / "messages"
    ledger_path = import_dir / "ledger.json"
    review_csv = Path(".powerpacks/messages/research_review.csv")
    diff = messages_import_diff(review_csv)
    if diff["candidate_rows"] > 0 and diff["new_rows"] == 0:
        directory_normalization = normalize_directory_source_accounts("messages")
        directory_quality = directory_source_account_quality("messages")
        return write_manifest("messages", {
            "status": "completed" if directory_quality["status"] == "ok" else "failed",
            "reason": "no_new_messages_directory_rows" if directory_quality["status"] == "ok" else "directory_source_account_quality_failed",
            "ledger": str(ledger_path),
            "artifact_dir": str(import_dir),
            "input": {
                "review_csv": str(review_csv),
                "discovery_manifest": str(DEFAULT_BASE_DIR / "discover" / "messages" / "manifest.json"),
                "contacts_csv": str(DEFAULT_BASE_DIR / "discover" / "messages" / "contacts.csv"),
            },
            "outputs": {
                "people_csv": str(import_dir / "people.csv") if (import_dir / "people.csv").exists() else "",
                "directory_csv": str(DEFAULT_DIRECTORY_CSV),
            },
            "stats": {
                "people": csv_count(str(import_dir / "people.csv")),
                "candidates": csv_count(str(review_csv)),
                "candidate_directory_rows": diff["candidate_rows"],
                "new_directory_rows": 0,
            },
            "diff": diff,
            "directory_normalization": directory_normalization,
            "directory_quality": directory_quality,
        })
    if diff["new_rows"] > 0 and not args.confirm_import:
        return write_manifest("messages", {
            "status": "blocked_approval",
            "approval_type": "import_confirmation",
            "message": f"Import {diff['new_rows']} reviewed Messages LinkedIn profiles into your local network?",
            "ledger": str(ledger_path),
            "artifact_dir": str(import_dir),
            "blocked": {
                "status": "blocked_approval",
                "approval_type": "import_confirmation",
                "source": "messages",
                "message": f"Import {diff['new_rows']} reviewed Messages LinkedIn profiles into your local network?",
                "payload": diff,
            },
            "input": {
                "review_csv": str(review_csv),
                "discovery_manifest": str(DEFAULT_BASE_DIR / "discover" / "messages" / "manifest.json"),
                "contacts_csv": str(DEFAULT_BASE_DIR / "discover" / "messages" / "contacts.csv"),
            },
            "outputs": {
                "people_csv": "",
                "directory_csv": str(DEFAULT_DIRECTORY_CSV),
            },
            "stats": {
                "people": 0,
                "candidates": csv_count(str(review_csv)),
                "candidate_directory_rows": diff["candidate_rows"],
                "new_directory_rows": diff["new_rows"],
            },
            "diff": diff,
        })
    ledger = {
        "primitive": "import_contacts_messages",
        "source": "messages",
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "artifact_dir": str(import_dir),
        "input": {
            "messages_review_csv": str(review_csv),
            "linkedin_directory_csv": str(DEFAULT_DIRECTORY_CSV),
        },
        "steps": {},
        "artifacts": {"messages_review_csv": str(review_csv)},
    }
    write_json(ledger_path, ledger)
    ok = messages_helpers.run_messages_enrichment(ledger_path, ledger)
    status = "completed" if ok else "blocked_approval" if ledger.get("blocked") else "failed"
    people_csv = copy_people_csv("messages", str(ledger.get("artifacts", {}).get("messages_merged_people_csv") or ledger.get("artifacts", {}).get("messages_people_csv") or ""))
    directory_normalization = normalize_directory_source_accounts("messages") if ok else {"status": "skipped", "reason": "messages_import_not_completed"}
    directory_quality = directory_source_account_quality("messages") if ok else {"status": "skipped", "reason": "messages_import_not_completed"}
    if ok and directory_quality["status"] != "ok":
        status = "failed"
    return write_manifest("messages", {
        "status": status,
        "reason": "directory_source_account_quality_failed" if status == "failed" and directory_quality.get("status") == "failed" else "",
        "ledger": str(ledger_path),
        "artifact_dir": str(import_dir),
        "blocked": ledger.get("blocked"),
        "input": {
            "review_csv": str(review_csv),
            "discovery_manifest": str(DEFAULT_BASE_DIR / "discover" / "messages" / "manifest.json"),
            "contacts_csv": str(DEFAULT_BASE_DIR / "discover" / "messages" / "contacts.csv"),
        },
        "outputs": {
            "people_csv": people_csv,
            "directory_csv": str(DEFAULT_DIRECTORY_CSV),
        },
        "stats": {
            "people": csv_count(people_csv),
            "candidates": csv_count(str(review_csv)),
        },
        "steps": ledger.get("steps", {}),
        "directory_normalization": directory_normalization,
        "directory_quality": directory_quality,
        "artifacts": ledger.get("artifacts", {}),
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import/enrich reviewed Messages contacts")
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument("--operator-id", default="local")
    parser.add_argument("--confirm-import", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = run(args)
    emit(payload)
    return 20 if payload.get("status") == "blocked_approval" else 1 if payload.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
