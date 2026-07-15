#!/usr/bin/env python3
"""Read-only per-source import status report.

Reports, for each fan-in source (gmail, linkedin, messages): whether discovery
ran, whether the import completed (manifest `status: completed` with an
existing `outputs.people_csv`), whether it is still current (fingerprints
match), and row counts — plus the merged people.csv summary. This is the
presence check the import skills use to suggest missing sources. It writes
nothing and always exits 0.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        emit,
        now_iso,
        read_json,
    )
    from packs.ingestion.primitives.import_contacts_pipeline.common import (
        DEFAULT_IMPORT_DIR,
        csv_count,
        import_manifest_current,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        emit,
        now_iso,
        read_json,
    )
    from packs.ingestion.primitives.import_contacts_pipeline.common import (
        DEFAULT_IMPORT_DIR,
        csv_count,
        import_manifest_current,
    )


FAN_IN_SOURCES = ["gmail", "linkedin", "messages"]
CANONICAL_MERGED_PEOPLE_CSV = Path(".powerpacks/network-import/merged/people.csv")


def discover_status(source: str, base_dir: Path) -> dict[str, Any]:
    discover_dir = base_dir / "discover" / source
    manifest_path = discover_dir / "manifest.json"
    manifest = read_json(manifest_path, {}) or {}
    contacts_csv = str(manifest.get("contacts_csv") or discover_dir / "contacts.csv")
    present = bool(manifest) and Path(contacts_csv).exists()
    return {
        "manifest": str(manifest_path),
        "present": present,
        "status": str(manifest.get("status") or ""),
        "contacts_csv": contacts_csv if Path(contacts_csv).exists() else "",
        "contacts": csv_count(contacts_csv),
        "updated_at": str(manifest.get("updated_at") or ""),
    }


def import_status(source: str, import_dir: Path) -> dict[str, Any]:
    manifest_path = import_dir / source / "manifest.json"
    manifest = read_json(manifest_path, {}) or {}
    outputs = manifest.get("outputs") if isinstance(manifest.get("outputs"), dict) else {}
    people_csv = str(outputs.get("people_csv") or "")
    candidates_csv = str(outputs.get("candidates_csv") or "")
    imported = (
        manifest.get("status") == "completed"
        and bool(people_csv)
        and Path(people_csv).exists()
    )
    current = bool(import_manifest_current(source, import_dir=import_dir)) if imported else False
    return {
        "manifest": str(manifest_path),
        "present": bool(manifest),
        "status": str(manifest.get("status") or ""),
        "imported": imported,
        "current": current,
        "people_csv": people_csv if imported else "",
        "people": csv_count(people_csv) if imported else 0,
        "candidates_csv": candidates_csv if candidates_csv and Path(candidates_csv).exists() else "",
        "candidates": csv_count(candidates_csv),
        "updated_at": str(manifest.get("updated_at") or ""),
    }


def merged_status() -> dict[str, Any]:
    return {
        "people_csv": str(CANONICAL_MERGED_PEOPLE_CSV),
        "exists": CANONICAL_MERGED_PEOPLE_CSV.exists(),
        "people": csv_count(str(CANONICAL_MERGED_PEOPLE_CSV)),
    }


def status_payload(sources: list[str]) -> dict[str, Any]:
    return {
        "primitive": "import_contacts_status",
        "status": "ok",
        "sources": {
            source: {
                "discover": discover_status(source, DEFAULT_BASE_DIR),
                "import": import_status(source, DEFAULT_IMPORT_DIR),
            }
            for source in sources
        },
        "merged": merged_status(),
        "updated_at": now_iso(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status"])
    parser.add_argument(
        "--source",
        choices=[*FAN_IN_SOURCES, "all"],
        default="all",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    sources = FAN_IN_SOURCES if args.source == "all" else [args.source]
    emit(status_payload(sources))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
