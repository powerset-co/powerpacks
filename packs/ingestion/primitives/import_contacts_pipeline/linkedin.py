#!/usr/bin/env python3
"""Import/enrich discovered LinkedIn CSV contacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        DEFAULT_DIRECTORY_CSV,
        emit,
        py_cmd,
        read_accounts,
        run_cmd,
    )
    from packs.ingestion.primitives.import_contacts_pipeline.common import (
        DEFAULT_ACCOUNTS,
        DEFAULT_IMPORT_DIR,
        copy_people_csv,
        csv_count,
        linkedin_csv_path,
        linkedin_source_user,
        write_manifest,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        DEFAULT_DIRECTORY_CSV,
        emit,
        py_cmd,
        read_accounts,
        run_cmd,
    )
    from packs.ingestion.primitives.import_contacts_pipeline.common import (
        DEFAULT_ACCOUNTS,
        DEFAULT_IMPORT_DIR,
        copy_people_csv,
        csv_count,
        linkedin_csv_path,
        linkedin_source_user,
        write_manifest,
    )


def run(args: argparse.Namespace) -> dict:
    accounts = read_accounts(args.accounts)
    csv_path = linkedin_csv_path(accounts)
    import_dir = DEFAULT_IMPORT_DIR / "linkedin"
    ledger_path = import_dir / "ledger.json"
    if not csv_path:
        return write_manifest("linkedin", {"status": "skipped", "reason": "no LinkedIn CSV", "artifact_dir": str(import_dir)})
    cmd = py_cmd(
        "packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py",
        "run",
        "--csv", csv_path,
        "--source-user", linkedin_source_user(accounts),
        "--operator-id", args.operator_id,
        "--output-dir", str(import_dir),
        "--ledger", str(ledger_path),
        "--force",
    )
    code, child, stderr = run_cmd(cmd)
    status = "completed" if code == 0 and child.get("status") == "completed" else child.get("status") or "failed"
    artifacts = child.get("artifacts") or {}
    people_csv = copy_people_csv("linkedin", str(artifacts.get("people_csv") or ""))
    return write_manifest("linkedin", {
        "status": status,
        "ledger": str(ledger_path),
        "artifact_dir": str(import_dir),
        "command_status": code,
        "child": child,
        "error": stderr if code != 0 else "",
        "input": {
            "connections_csv": csv_path,
        },
        "outputs": {
            "people_csv": people_csv,
            "directory_csv": str(DEFAULT_DIRECTORY_CSV),
        },
        "stats": {
            "people": csv_count(people_csv),
            "candidates": csv_count(str(DEFAULT_BASE_DIR / "discover" / "linkedin" / "contacts.csv")),
        },
        "artifacts": artifacts,
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import/enrich discovered LinkedIn contacts")
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument("--operator-id", default="local")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = run(args)
    emit(payload)
    return 20 if payload.get("status") == "blocked_approval" else 1 if payload.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
