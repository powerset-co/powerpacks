#!/usr/bin/env python3
"""Discover LinkedIn contacts from a local Connections.csv export."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        emit,
        now_iso,
        read_csv_rows,
        read_json,
        sha256_file,
        write_csv_rows,
        write_stage_manifest,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.discovery_config import (
        accounts_path as configured_accounts_path,
        output_path,
        source_config,
        state_value,
    )
    from packs.ingestion.schemas.people_schema import (
        extract_public_identifier,
        generate_person_id,
        normalize_linkedin_url,
    )
    from packs.shared.csv_io import CsvIO
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        emit,
        now_iso,
        read_csv_rows,
        read_json,
        sha256_file,
        write_csv_rows,
        write_stage_manifest,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.discovery_config import (
        accounts_path as configured_accounts_path,
        output_path,
        source_config,
        state_value,
    )
    from packs.ingestion.schemas.people_schema import (
        extract_public_identifier,
        generate_person_id,
        normalize_linkedin_url,
    )
    from packs.shared.csv_io import CsvIO

LINKEDIN_DISCOVERY_COLUMNS = [
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


def linkedin_export_header(line: str) -> bool:
    lowered = line.strip().lower()
    return lowered.startswith("first name,") or ("first name" in lowered and "url" in lowered and "," in lowered)


def source_user(accounts: dict[str, Any]) -> str:
    channel = ((accounts.get("accounts") or {}).get("linkedin_csv") or {}) if isinstance(accounts, dict) else {}
    cfg = channel.get("config") if isinstance(channel.get("config"), dict) else {}
    if cfg.get("source_label"):
        return str(cfg["source_label"])
    usernames = channel.get("usernames")
    if isinstance(usernames, list) and usernames:
        return str(usernames[0])
    return "local"


def csv_path(accounts: dict[str, Any]) -> Path:
    cfg = source_config("linkedin_csv")["inputs"]
    value = state_value(accounts, cfg["connections_csv_state_key"], "")
    return Path(str(value or "")).expanduser()


def parse_connections_csv(path: Path, user: str) -> tuple[list[dict[str, str]], dict[str, int]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    duplicates = 0
    skipped_invalid = 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        header_line = ""
        for line in handle:
            if linkedin_export_header(line):
                header_line = line.strip()
                break
        if not header_line:
            raise ValueError("Could not find LinkedIn export header row containing 'First Name' and 'URL'")
        reader = CsvIO.dict_reader(handle, fieldnames=next(CsvIO.reader([header_line])))
        for row in reader:
            linkedin_url = normalize_linkedin_url(row.get("URL", ""))
            public_identifier = extract_public_identifier(linkedin_url)
            if not public_identifier:
                skipped_invalid += 1
                continue
            if public_identifier in seen:
                duplicates += 1
                continue
            seen.add(public_identifier)
            rows.append({
                "person_id": generate_person_id(public_identifier),
                "public_identifier": public_identifier,
                "linkedin_url": linkedin_url,
                "first_name": (row.get("First Name") or "").strip(),
                "last_name": (row.get("Last Name") or "").strip(),
                "source_user": user,
                "linkedin_company": (row.get("Company") or "").strip(),
                "linkedin_position": (row.get("Position") or "").strip(),
                "linkedin_email": (row.get("Email Address") or "").strip(),
                "connected_on": (row.get("Connected On") or "").strip(),
            })
    return rows, {"parsed": len(rows), "duplicates": duplicates, "skipped_invalid": skipped_invalid}


def merge_contacts(existing: list[dict[str, str]], incoming: list[dict[str, str]]) -> list[dict[str, str]]:
    keyed: dict[str, dict[str, str]] = {}
    for row in [*existing, *incoming]:
        public_identifier = str(row.get("public_identifier") or "").strip().lower()
        if not public_identifier:
            continue
        current = keyed.get(public_identifier, {})
        merged = dict(current)
        for field in LINKEDIN_DISCOVERY_COLUMNS:
            value = str(row.get(field) or "")
            if value or field not in merged:
                merged[field] = value
        keyed[public_identifier] = merged
    return [{field: row.get(field, "") for field in LINKEDIN_DISCOVERY_COLUMNS} for _, row in sorted(keyed.items())]


def discover(
    *,
    accounts_file: Path | None = None,
    connections_csv: str | Path | None = None,
    source_user_label: str | None = None,
) -> dict[str, Any]:
    """Discover LinkedIn Connections.csv contacts. Strict keyword-only —
    unknown options raise instead of being silently swallowed (the old **_
    hid never-honored kwargs from the orchestrator)."""
    accounts_file = accounts_file or configured_accounts_path()
    accounts = read_json(accounts_file, {}) or {}
    source_csv = Path(str(connections_csv)).expanduser() if connections_csv else csv_path(accounts)
    user = str(source_user_label or "").strip() or source_user(accounts)
    source_out = output_path("linkedin_csv", "source_csv")
    contacts_csv = output_path("linkedin_csv", "contacts_csv")
    manifest_json = output_path("linkedin_csv", "manifest_json")

    if not source_csv.exists():
        payload = {
            "status": "skipped",
            "source": "linkedin_csv",
            "reason": "connections_csv_not_found",
            "connections_csv": str(source_csv),
            "contacts_csv": str(contacts_csv),
        }
        write_stage_manifest(manifest_json, payload)
        return payload

    contacts_csv.parent.mkdir(parents=True, exist_ok=True)
    if source_csv.resolve() != source_out.resolve():
        same_content = False
        if source_out.exists() and source_out.is_file() and source_csv.stat().st_size == source_out.stat().st_size:
            same_content = sha256_file(source_csv) == sha256_file(source_out)
        if not same_content:
            shutil.copyfile(source_csv, source_out)
    incoming, stats = parse_connections_csv(source_out, user)
    existing: list[dict[str, str]] = []
    if contacts_csv.exists():
        _fields, existing = read_csv_rows(contacts_csv)
    merged = merge_contacts(existing, incoming)
    write_csv_rows(contacts_csv, LINKEDIN_DISCOVERY_COLUMNS, merged)
    payload = {
        "status": "completed",
        "source": "linkedin_csv",
        "source_csv": str(source_out),
        "contacts_csv": str(contacts_csv),
        "contacts": len(merged),
        "source_user": user,
        "updated_at": now_iso(),
        "stats": stats,
        "privacy": {
            "rapidapi_called": False,
            "parallel_called": False,
            "upload_ran": False,
        },
    }
    return write_stage_manifest(manifest_json, payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover LinkedIn contacts from Connections.csv")
    parser.add_argument("command", choices=["discover"])
    parser.add_argument("--accounts", type=Path, default=None)
    parser.add_argument("--csv", default="")
    parser.add_argument("--source-user", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    emit(discover(accounts_file=args.accounts, connections_csv=args.csv, source_user_label=args.source_user))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
