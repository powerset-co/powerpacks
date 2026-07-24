#!/usr/bin/env python3
"""Shared helpers for import/enrich contact stages.

Changelog:
  2026-07-23 (steps split): removed `load_gmail_import_steps` — the
    `importlib.util.spec_from_file_location` fossil that file-loaded
    gmail/import_steps.py as a synthetic module. `GmailImport` now lives in
    gmail/importer.py and its steps in gmail/steps/; consumers import them
    directly (normal package imports). The `importlib.util` / `sys` imports went
    with it.
  2026-07-24: removed the Gmail step-ledger constructor; imports persist only
    their output files and manifest.json.
  2026-07-23 (audit batch 18): import_steps.py moved home — from the discover
    package into this package's gmail/ vertical (it is import-stage code).
  2026-07-23 (audit batch 20A): package renamed `import_contacts_pipeline` →
    `imports` (Python reserved-word `import` could not be a package name). The
    dead local linkedin/importer.py was deleted and the Modal linkedin
    convert+enrich engine now lives at imports/linkedin/network_import.py.
  2026-07-23 (audit batch 21): directory helpers import updated from
    discover.directory → imports.directory (the module moved to this stage).
  2026-07-23 (audit consolidation): cross-vertical helpers now come from
    packs.ingestion.primitives.common — now_iso/read_json/write_json/
    unique_strings/sha256_file from common.jsonio, and DEFAULT_BASE_DIR/
    DEFAULT_DIRECTORY_CSV/DEFAULT_IMPORT_DIR from common.paths (kept as module
    globals so the existing mock.patch.object(import_common, ...) test hooks
    still bind). DEFAULT_ACCOUNTS/DEFAULT_PROFILE_CACHE_DIR moved to common.paths
    (their consumers import them from there directly).
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import (
    now_iso,
    read_json,
    sha256_file,
    unique_strings,
    write_json,
)
from packs.ingestion.primitives.common.paths import (
    DEFAULT_BASE_DIR,
    DEFAULT_DIRECTORY_CSV,
    DEFAULT_IMPORT_DIR,
)
from packs.ingestion.primitives.imports.directory import (
    DIRECTORY_COLUMNS,
    normalized_directory_row,
)
from packs.shared.csv_io import CsvIO


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
    if value and Path(str(value)).exists():
        return str(value)
    repo_local = DEFAULT_BASE_DIR / "discover" / "linkedin" / "Connections.csv"
    if repo_local.exists():
        return str(repo_local)
    return str(value or "")


def linkedin_source_user(accounts: dict[str, Any]) -> str:
    cfg = account_config(accounts, "linkedin_csv")
    channel = account_channel(accounts, "linkedin_csv")
    if cfg.get("source_label"):
        return str(cfg["source_label"])
    if isinstance(channel.get("usernames"), list) and channel["usernames"]:
        return str(channel["usernames"][0])
    return "local"


def artifact_fingerprint(path_text: str, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    path = Path(str(path_text or ""))
    if not path_text or not path.exists() or not path.is_file():
        return {"path": str(path_text or ""), "exists": False}
    stat = path.stat()
    existing = existing or {}
    mtime_ns = stat.st_mtime_ns
    if (
        existing.get("path") == str(path)
        and existing.get("exists") is True
        and existing.get("size") == stat.st_size
        and existing.get("mtime_ns") == mtime_ns
        and existing.get("sha256")
    ):
        return dict(existing)
    return {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": mtime_ns,
        "sha256": sha256_file(path),
    }


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
        if text.startswith(".powerpacks/") or Path(text).exists():
            paths.append(text)
    return list(dict.fromkeys(paths))


def manifest_fingerprints(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    existing_inputs = existing.get("input_artifacts") if isinstance(existing.get("input_artifacts"), dict) else {}
    existing_outputs = existing.get("output_artifacts") if isinstance(existing.get("output_artifacts"), dict) else {}
    input_paths = collect_artifact_paths(payload.get("input") or {})
    output_paths = collect_artifact_paths({"outputs": payload.get("outputs") or {}, "artifacts": payload.get("artifacts") or {}})
    return {
        "input_artifacts": {path: artifact_fingerprint(path, existing_inputs.get(path) if isinstance(existing_inputs, dict) else None) for path in input_paths},
        "output_artifacts": {path: artifact_fingerprint(path, existing_outputs.get(path) if isinstance(existing_outputs, dict) else None) for path in output_paths},
    }


def stable_manifest_signature(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the whole manifest payload without volatile timestamp fields."""
    signature = dict(payload)
    signature.pop("updated_at", None)
    signature.pop("created_at", None)
    return signature


def write_manifest(source: str, payload: dict[str, Any], import_dir: Path | None = None) -> dict[str, Any]:
    import_dir = (import_dir or DEFAULT_IMPORT_DIR) / source
    manifest = import_dir / "manifest.json"
    existing = read_json(manifest, {}) or {}
    payload = {
        "source": source,
        "status": payload.get("status") or "completed",
        **payload,
    }
    payload["fingerprints"] = payload.get("fingerprints") or manifest_fingerprints(payload, existing.get("fingerprints") if isinstance(existing.get("fingerprints"), dict) else None)
    if existing and stable_manifest_signature(existing) == stable_manifest_signature(payload):
        return existing
    payload["updated_at"] = payload.get("updated_at") or now_iso()
    write_json(manifest, payload)
    return payload


def fingerprint_matches(path_text: str, fingerprint: dict[str, Any]) -> bool:
    current = artifact_fingerprint(path_text, fingerprint)
    return current == fingerprint


def is_shared_directory_csv(path_text: str) -> bool:
    if str(path_text) == str(DEFAULT_DIRECTORY_CSV):
        return True
    try:
        return Path(path_text).resolve() == DEFAULT_DIRECTORY_CSV.resolve()
    except (OSError, RuntimeError):
        return False


def import_manifest_current(source: str, expected_input: dict[str, Any] | None = None, import_dir: Path | None = None) -> dict[str, Any] | None:
    manifest = (import_dir or DEFAULT_IMPORT_DIR) / source / "manifest.json"
    existing = read_json(manifest, {}) or {}
    if not isinstance(existing, dict) or existing.get("status") != "completed":
        return None
    if expected_input:
        existing_input = existing.get("input") if isinstance(existing.get("input"), dict) else {}
        for key, expected in expected_input.items():
            if existing_input.get(key) != expected:
                return None
    fingerprints = existing.get("fingerprints") if isinstance(existing.get("fingerprints"), dict) else {}
    groups = [fingerprints.get("input_artifacts"), fingerprints.get("output_artifacts")]
    saw_file = False
    for group in groups:
        if not isinstance(group, dict):
            continue
        for path_text, fingerprint in group.items():
            if is_shared_directory_csv(str(path_text)):
                continue
            if not isinstance(fingerprint, dict) or not fingerprint.get("exists"):
                continue
            saw_file = True
            if not fingerprint_matches(str(path_text), fingerprint):
                return None
    if not saw_file:
        return None
    return {**existing, "noop": True, "reason": "import_manifest_current"}


def copy_people_csv(source: str, people_csv: str, import_dir: Path | None = None) -> str:
    if not people_csv:
        return ""
    src = Path(str(people_csv))
    if not src.exists():
        return ""
    dest = (import_dir or DEFAULT_IMPORT_DIR) / source / "people.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        if dest.exists() and dest.is_file() and src.stat().st_size == dest.stat().st_size and sha256_file(src) == sha256_file(dest):
            return str(dest)
        shutil.copyfile(src, dest)
    return str(dest)


def csv_count(path_text: str) -> int:
    path = Path(str(path_text or ""))
    if not path_text or not path.exists() or not path.is_file():
        return 0
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return sum(1 for _ in CsvIO.dict_reader(handle))


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
        rows = list(CsvIO.dict_reader(handle))
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
        for row in CsvIO.dict_reader(handle):
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
