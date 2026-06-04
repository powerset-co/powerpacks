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
