#!/usr/bin/env python3
"""Shared helpers for import/enrich contact stages."""

from __future__ import annotations

import importlib.util
import csv
import shutil
import sys
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.discover_contacts_pipeline.common import (
    DEFAULT_BASE_DIR,
    DEFAULT_DIRECTORY_CSV,
    now_iso,
    read_accounts,
    read_json,
    source_slug,
    unique_strings,
    write_json,
)
from packs.ingestion.primitives.discover_contacts_pipeline.directory import (
    DIRECTORY_COLUMNS,
    normalized_directory_row,
)

DEFAULT_ACCOUNTS = Path(".powerpacks/ingestion/accounts.json")
DEFAULT_IMPORT_DIR = DEFAULT_BASE_DIR / "import"
DEFAULT_PROFILE_CACHE_DIR = DEFAULT_BASE_DIR / "profile_cache_v2"


def load_legacy_discover_module() -> Any:
    path = Path(__file__).resolve().parents[1] / "discover_contacts_pipeline" / "discover_contacts_pipeline.before_split.py"
    spec = importlib.util.spec_from_file_location("_powerpacks_legacy_discover_contacts_pipeline", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"could not load legacy import helpers: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def account_channel(accounts: dict[str, Any], name: str) -> dict[str, Any]:
    group = accounts.get("accounts") if isinstance(accounts.get("accounts"), dict) else {}
    return group.get(name) if isinstance(group.get(name), dict) else {}


def account_config(accounts: dict[str, Any], name: str) -> dict[str, Any]:
    channel = account_channel(accounts, name)
    cfg = channel.get("config")
    return cfg if isinstance(cfg, dict) else {}


def linked_gmail_accounts(accounts: dict[str, Any]) -> list[str]:
    cfg = account_config(accounts, "gmail")
    channel = account_channel(accounts, "gmail")
    return unique_strings(cfg.get("selected_accounts") or cfg.get("account_emails") or channel.get("usernames") or [])


def linkedin_csv_path(accounts: dict[str, Any]) -> str:
    cfg = account_config(accounts, "linkedin_csv")
    channel = account_channel(accounts, "linkedin_csv")
    value = cfg.get("csv_path") or ""
    if not value and isinstance(channel.get("artifacts"), list) and channel["artifacts"]:
        value = channel["artifacts"][0]
    return str(value or "")


def linkedin_source_user(accounts: dict[str, Any]) -> str:
    cfg = account_config(accounts, "linkedin_csv")
    channel = account_channel(accounts, "linkedin_csv")
    if cfg.get("source_label"):
        return str(cfg["source_label"])
    if isinstance(channel.get("usernames"), list) and channel["usernames"]:
        return str(channel["usernames"][0])
    return "local"


def write_manifest(source: str, payload: dict[str, Any]) -> dict[str, Any]:
    import_dir = DEFAULT_IMPORT_DIR / source
    manifest = import_dir / "manifest.json"
    payload = {
        "source": source,
        "status": payload.get("status") or "completed",
        "updated_at": payload.get("updated_at") or now_iso(),
        **payload,
    }
    write_json(manifest, payload)
    return payload


def copy_people_csv(source: str, people_csv: str) -> str:
    if not people_csv:
        return ""
    src = Path(str(people_csv))
    if not src.exists():
        return ""
    dest = DEFAULT_IMPORT_DIR / source / "people.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copyfile(src, dest)
    return str(dest)


def csv_count(path_text: str) -> int:
    path = Path(str(path_text or ""))
    if not path.exists():
        return 0
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def directory_row_matches_source(row: dict[str, str], source: str) -> bool:
    row_source = str(row.get("source") or "").strip()
    source_key = str(row.get("source_key") or "").strip()
    if source == "gmail":
        return row_source == "gmail_msgvault" or source_key.startswith("gmail:")
    return row_source == source


def normalize_directory_source_accounts(source: str, directory_csv: Path = DEFAULT_DIRECTORY_CSV) -> dict[str, Any]:
    if not directory_csv.exists():
        return {"status": "skipped", "reason": "directory_csv_missing", "updated_rows": 0}
    with directory_csv.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        rows = list(csv.DictReader(handle))
    changed = 0
    normalized_rows: list[dict[str, str]] = []
    for row in rows:
        if not directory_row_matches_source(row, source):
            normalized_rows.append(row)
            continue
        normalized = normalized_directory_row(row, source="directory")
        if not normalized:
            normalized_rows.append(row)
            continue
        normalized = {column: normalized.get(column, "") for column in DIRECTORY_COLUMNS}
        original = {column: row.get(column, "") for column in DIRECTORY_COLUMNS}
        if normalized != original:
            changed += 1
        normalized_rows.append(normalized)
    if changed:
        directory_csv.parent.mkdir(parents=True, exist_ok=True)
        with directory_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=DIRECTORY_COLUMNS)
            writer.writeheader()
            writer.writerows(normalized_rows)
    return {"status": "completed", "directory_csv": str(directory_csv), "updated_rows": changed}


def directory_source_account_quality(source: str, directory_csv: Path = DEFAULT_DIRECTORY_CSV) -> dict[str, Any]:
    if not directory_csv.exists():
        return {
            "status": "failed",
            "source": source,
            "directory_csv": str(directory_csv),
            "reason": "directory_csv_missing",
            "checked_rows": 0,
            "missing_source_account": 0,
            "invalid_source_channels": 0,
            "samples": [],
        }
    source_name = "gmail_msgvault" if source == "gmail" else source
    checked = 0
    missing_source_account = 0
    invalid_source_channels = 0
    samples: list[dict[str, str]] = []
    with directory_csv.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        for row in csv.DictReader(handle):
            row_source = str(row.get("source") or "").strip()
            source_key = str(row.get("source_key") or "").strip()
            if source == "gmail":
                matches = row_source == "gmail_msgvault" or source_key.startswith("gmail:")
            else:
                matches = row_source == source_name
            if not matches:
                continue
            checked += 1
            source_account = str(row.get("source_account") or "").strip()
            source_channels = str(row.get("source_channels") or "").strip()
            row_missing = not source_account
            row_invalid_channels = source == "messages" and not any(channel in source_channels.split(",") for channel in ("imessage", "whatsapp"))
            if row_missing:
                missing_source_account += 1
            if row_invalid_channels:
                invalid_source_channels += 1
            if (row_missing or row_invalid_channels) and len(samples) < 5:
                samples.append({
                    "source_key": source_key,
                    "source": row_source,
                    "source_account": source_account,
                    "source_channels": source_channels,
                    "email": str(row.get("email") or ""),
                    "phone": str(row.get("phone") or ""),
                    "linkedin_url": str(row.get("linkedin_url") or ""),
                })
    status = "ok" if missing_source_account == 0 and invalid_source_channels == 0 else "failed"
    return {
        "status": status,
        "source": source,
        "directory_csv": str(directory_csv),
        "checked_rows": checked,
        "missing_source_account": missing_source_account,
        "invalid_source_channels": invalid_source_channels,
        "samples": samples,
    }
