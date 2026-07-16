#!/usr/bin/env python3
"""Orchestrate local network ingestion sources into merged CSVs.

Sources handled here:
- LinkedIn Connections.csv -> linkedin_network_import
- Gmail msgvault SQLite -> gmail_network_import msgvault
- Existing Twitter artifacts may be picked up by merge discovery

Message contacts are intentionally not auto-merged from
`.powerpacks/messages/contacts.csv`. That file is a local extraction artifact
owned by `$import-contacts`; message contacts must pass that reviewed,
approval-gated flow before they are uploaded or promoted into a searchable
network index.

This orchestrator is local-first. It does not upload or mutate production. Child
primitives remain responsible for their own approval confirmations.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from packs.ingestion.schemas.people_schema import (
        LIST_VALUE_COLUMNS,
        PEOPLE_SCHEMA_COLUMNS,
        extract_public_identifier,
        merge_interaction_counts,
        normalize_linkedin_url,
        normalize_people_row,
    )
    from packs.shared.csv_io import CsvIO
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.schemas.people_schema import (
        LIST_VALUE_COLUMNS,
        PEOPLE_SCHEMA_COLUMNS,
        extract_public_identifier,
        merge_interaction_counts,
        normalize_linkedin_url,
        normalize_people_row,
    )
    from packs.shared.csv_io import CsvIO

DEFAULT_BASE_DIR = Path(".powerpacks/network-import")
DEFAULT_DISCOVER_DIR = DEFAULT_BASE_DIR / "discover"
DEFAULT_FINAL_DIR = DEFAULT_BASE_DIR / "final"
DEFAULT_LEDGER = DEFAULT_DISCOVER_DIR / "ledger.json"
DEFAULT_DIRECTORY_CSV = DEFAULT_BASE_DIR / "directory.csv"
DEFAULT_MSGVAULT_DB = Path.home() / ".msgvault" / "msgvault.db"
DEFAULT_CHILD_TIMEOUT_SECONDS = int(os.environ.get("POWERPACKS_IMPORT_NETWORK_CHILD_TIMEOUT_SECONDS", str(6 * 60 * 60)))
DEFAULT_GMAIL_ESTIMATE_MAX_PAGES = int(os.environ.get("POWERPACKS_GMAIL_ESTIMATE_MAX_PAGES", "0"))
DEFAULT_GMAIL_EXCLUDED_LABELS = ("CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_FORUMS", "CATEGORY_UPDATES")
DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
GMAIL_ESTIMATE_LABEL_IDS = ("INBOX", "SENT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_FORUMS", "CATEGORY_UPDATES", "SPAM", "TRASH")
GMAIL_LABEL_QUERY_TERMS = {
    "CATEGORY_SOCIAL": "-category:social",
    "CATEGORY_PROMOTIONS": "-category:promotions",
    "CATEGORY_FORUMS": "-category:forums",
    "CATEGORY_UPDATES": "-category:updates",
    "SPAM": "-in:spam",
    "TRASH": "-in:trash",
}
DIRECTORY_COLUMNS = [
    "source",
    "source_key",
    "source_account",
    "source_id",
    "source_channels",
    "status",
    "email",
    "phone",
    "name",
    "linkedin_url",
    "public_identifier",
    "confidence",
    "matched_name",
    "matched_headline",
    "evidence",
    "reasoning",
    "source_artifact",
    "updated_at",
]
LINKEDIN_RESOLUTION_COLUMNS = ["handle", "status", "linkedin_url", "confidence", "matched_name", "matched_headline", "evidence", "reasoning"]
RESOLUTION_FOUND_STATUSES = {"found", "completed", "success"}
RESOLUTION_NEGATIVE_STATUSES = {"not_found", "not-found", "missing", "failed", "error"}
LINKEDIN_URL_COLUMNS = [
    "confirmed_linkedin_url",
    "human_confirmed_linkedin",
    "final_linkedin_url",
    "proposed_linkedin_url",
    "linkedin_url",
    "linkedin_profile_url",
    "profile_url",
    "pass1_linkedin_url",
    "llm_selected_linkedin",
]
HIGH_CONFIDENCE_URL_COLUMNS = {"confirmed_linkedin_url", "human_confirmed_linkedin", "final_linkedin_url"}


SOURCE_NAMES = ["gmail", "linkedin_csv", "twitter", "messages"]
MERGED_ARTIFACT_KEYS = {
    "merged_people_csv",
    "network_contacts_csv",
    "network_contact_sources_csv",
    "network_companies_csv",
    "merge_manifest",
    # Retain retired keys here so a source refresh discards stale ledger state.
    "duckdb",
    "duckdb_manifest",
}
SOURCE_ARTIFACT_PREFIXES = {
    "gmail": ("gmail_",),
    "linkedin_csv": ("linkedin_",),
    "twitter": ("twitter_",),
    "messages": ("messages_",),
}
SOURCE_STEP_PREFIXES = {
    "gmail": ("gmail_msgvault", "gmail_directory", "gmail_linkedin_resolution", "gmail_apply_enrich"),
    "linkedin_csv": ("linkedin",),
    "twitter": ("twitter",),
    "messages": ("messages", "messages_enrich_people"),
}
MESSAGES_REVIEW_GATE_REASON = (
    "messages contacts require the reviewed $import-contacts flow before they can be merged into local network search"
)
TRUTHY = {"1", "true", "yes", "y", "on"}
FALSY = {"0", "false", "no", "n", "off"}
INCLUDE_DECISIONS = {"include", "approved", "approve", "yes", "true", "1"}
EXCLUDE_DECISIONS = {"exclude", "excluded", "skip", "skipped", "no", "false", "0"}
RESEARCH_REVIEW_SOURCES = {"llm_network_review", "retarget_research", "retarget_refresh", "deep_research"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def discover_source_dir(source: str) -> Path:
    if source == "linkedin_csv":
        return DEFAULT_DISCOVER_DIR / "linkedin"
    return DEFAULT_DISCOVER_DIR / source


def default_artifact_dir(args: argparse.Namespace, selected_sources: set[str]) -> Path:
    if getattr(args, "enrichment_only", False) and selected_sources and len(selected_sources) == 1:
        source = next(iter(selected_sources))
        return discover_source_dir(source)
    if getattr(args, "only_source", "") and selected_sources and len(selected_sources) == 1:
        source = next(iter(selected_sources))
        return discover_source_dir(source)
    return DEFAULT_FINAL_DIR


def artifact_dir_from_ledger(ledger: dict[str, Any]) -> Path:
    return Path(str(ledger.get("artifact_dir") or ledger.get("run_dir") or DEFAULT_DISCOVER_DIR))


def emit_progress(message: str) -> None:
    print(f"[discover-contacts] {message}", file=sys.stderr, flush=True)


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


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = CsvIO.dict_reader(handle)
        fields = list(reader.fieldnames or [])
        rows = [{str(key): value or "" for key, value in row.items() if key is not None} for row in reader]
    return fields, rows


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def csv_row_count(path: Path) -> int:
    if not path.exists():
        return 0
    _, rows = read_csv_rows(path)
    return len(rows)


def parse_jsonish(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def emails_from_value(value: Any) -> list[str]:
    parsed = parse_jsonish(value, None)
    found: list[str] = []
    if isinstance(parsed, list):
        for item in parsed:
            found.extend(emails_from_value(item))
    elif isinstance(parsed, dict):
        for item in parsed.values():
            found.extend(emails_from_value(item))
    else:
        found.extend(match.group(0).lower() for match in EMAIL_RE.finditer(str(value or "")))
    return sorted(set(found))


def emails_from_row(row: dict[str, str]) -> list[str]:
    emails: list[str] = []
    for key in ("primary_email", "email", "handle", "all_emails", "emails"):
        emails.extend(emails_from_value(row.get(key, "")))
    return sorted(set(emails))


def normalize_phone(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    plus = text.startswith("+")
    digits = re.sub(r"\D+", "", text)
    if len(digits) < 7:
        return ""
    return f"+{digits}" if plus else digits


def phones_from_value(value: Any) -> list[str]:
    parsed = parse_jsonish(value, None)
    found: list[str] = []
    if isinstance(parsed, list):
        for item in parsed:
            found.extend(phones_from_value(item))
    elif isinstance(parsed, dict):
        for item in parsed.values():
            found.extend(phones_from_value(item))
    else:
        phone = normalize_phone(value)
        if phone:
            found.append(phone)
    return sorted(set(found))


def phones_from_row(row: dict[str, str]) -> list[str]:
    phones: list[str] = []
    for key in ("primary_phone", "phone", "phone_e164", "all_phones", "phones"):
        phones.extend(phones_from_value(row.get(key, "")))
    return sorted(set(phones))


def directory_name(row: dict[str, str]) -> str:
    full = row.get("display_name") or row.get("full_name") or row.get("matched_name") or row.get("name") or row.get("harmonic_full_name") or ""
    if full:
        return full.strip()
    return f"{row.get('first_name', '').strip()} {row.get('last_name', '').strip()}".strip()


def normalize_name_key(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def choose_linkedin_url(row: dict[str, str]) -> tuple[str, str]:
    for column in LINKEDIN_URL_COLUMNS:
        url = normalize_linkedin_url(row.get(column) or "")
        if extract_public_identifier(url):
            return url, column
    return "", ""


def parse_confidence(value: Any, default: float = 0.0) -> float:
    raw = str(value or "").strip().lower()
    if raw in {"high", "confirmed", "exact"}:
        return 0.95
    if raw in {"medium", "med"}:
        return 0.8
    if raw == "low":
        return 0.5
    try:
        parsed = float(raw)
        return parsed / 100.0 if parsed > 1 else parsed
    except ValueError:
        return default


def directory_confidence(row: dict[str, str], url_col: str) -> float:
    if url_col in HIGH_CONFIDENCE_URL_COLUMNS:
        return 1.0
    for key in ("confidence", "llm_confidence", "basis.linkedin_url.confidence"):
        if row.get(key):
            return parse_confidence(row.get(key), 0.0)
    status = (row.get("status") or "").strip().lower()
    return 0.9 if status in {"completed", "found", "success"} else 0.8


def directory_source_kind(path: Path) -> str:
    name = path.name
    if name.startswith("linkedin_resolutions"):
        return "linkedin_resolutions"
    if name.startswith("linkedin_candidates"):
        return "linkedin_candidates"
    if name.startswith("parallel_enriched"):
        return "parallel_enriched"
    if name.startswith("confirmed_candidates"):
        return "confirmed_candidates"
    if name.startswith("llm_reviewed"):
        return "llm_reviewed"
    if name == "directory.csv":
        return "directory"
    return "other"


def directory_source_priority(source: str, url_col: str) -> int:
    if source == "directory":
        return 85
    if url_col in {"confirmed_linkedin_url", "human_confirmed_linkedin"}:
        return 100
    if source == "confirmed_candidates":
        return 90
    if source == "linkedin_resolutions":
        return 88
    if source == "parallel_enriched":
        return 80
    if url_col in {"final_linkedin_url", "linkedin_url"}:
        return 70
    if url_col in {"pass1_linkedin_url", "llm_selected_linkedin"}:
        return 60
    return 50


def directory_identity_key(email: str, phone: str, name: str, public_identifier: str, source_key: str = "") -> str:
    if email:
        return f"email:{email.lower()}"
    if phone:
        return f"phone:{normalize_phone(phone)}"
    if source_key:
        return f"source:{source_key.strip().lower()}"
    name_key = normalize_name_key(name)
    if name_key and public_identifier:
        return f"name:{name_key}|linkedin:{public_identifier}"
    return ""


def gmail_account_from_source_key(source_key: str) -> str:
    if not source_key.startswith("gmail:"):
        return ""
    parts = source_key.split(":", 3)
    if len(parts) < 2:
        return ""
    return parts[1].strip().lower()


def normalized_directory_row(row: dict[str, Any], *, source_artifact: str = "", source: str = "", updated_at: str = "") -> dict[str, str]:
    linkedin_url = normalize_linkedin_url(str(row.get("linkedin_url") or ""))
    public_identifier = extract_public_identifier(linkedin_url)
    email = (str(row.get("email") or row.get("primary_email") or "").strip().lower())
    phone = normalize_phone(row.get("phone") or row.get("primary_phone") or "")
    name = str(row.get("name") or row.get("matched_name") or row.get("display_name") or row.get("full_name") or "").strip()
    source_key = str(row.get("source_key") or "").strip()
    if not source_key:
        source_key = directory_identity_key(email, phone, name, public_identifier)
    if not source_key:
        return {}
    confidence = parse_confidence(row.get("confidence"), 0.0)
    status = str(row.get("status") or ("found" if public_identifier else "observed")).strip().lower()
    source_name = str(row.get("source") or source or "directory")
    source_account = str(row.get("source_account") or row.get("account_email") or "")
    if not source_account and (source_name == "gmail_msgvault" or source_key.startswith("gmail:")):
        source_account = gmail_account_from_source_key(source_key)
    if not source_account and source_name == "messages":
        source_account = str(row.get("source_channels") or "messages")
    output = {
        "source": source_name,
        "source_key": source_key,
        "source_account": source_account,
        "source_id": str(row.get("source_id") or ""),
        "source_channels": str(row.get("source_channels") or ""),
        "status": status,
        "email": email,
        "phone": phone,
        "name": name,
        "linkedin_url": linkedin_url,
        "public_identifier": public_identifier,
        "confidence": f"{confidence:.2f}",
        "matched_name": str(row.get("matched_name") or name),
        "matched_headline": str(row.get("matched_headline") or ""),
        "evidence": str(row.get("evidence") or ""),
        "reasoning": str(row.get("reasoning") or ""),
        "source_artifact": str(row.get("source_artifact") or source_artifact),
        "updated_at": str(row.get("updated_at") or updated_at),
    }
    priority = row.get("_priority")
    output["_priority"] = str(priority if priority is not None else directory_source_priority(output["source"], str(row.get("_url_column") or "")))
    return output


def directory_rows_from_resolutions(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    source = directory_source_kind(path)
    for row in read_csv_rows(path)[1]:
        status = (row.get("status") or "").strip().lower()
        linkedin_url = normalize_linkedin_url(row.get("linkedin_url") or "")
        public_identifier = extract_public_identifier(linkedin_url)
        if status != "found" or not public_identifier:
            continue
        confidence = parse_confidence(row.get("confidence"), 0.0)
        if confidence < 0.75:
            continue
        handle = (row.get("handle") or "").strip().lower()
        emails = emails_from_value(handle)
        phones = phones_from_value(handle)
        name = row.get("matched_name") or ""
        identities = [(email, "", directory_identity_key(email, "", name, public_identifier)) for email in emails]
        identities.extend(("", phone, directory_identity_key("", phone, name, public_identifier)) for phone in phones)
        if not identities:
            identities = [("", "", directory_identity_key("", "", name, public_identifier, handle))]
        for email, phone, source_key in identities:
            rows.append(normalized_directory_row({
                "source": source,
                "source_key": source_key,
                "email": email,
                "phone": phone,
                "name": name,
                "linkedin_url": linkedin_url,
                "confidence": f"{confidence:.2f}",
                "matched_name": name,
                "matched_headline": row.get("matched_headline") or "",
                "evidence": row.get("evidence") or "",
                "reasoning": row.get("reasoning") or "",
                "_priority": directory_source_priority(source, "linkedin_url"),
            }, source_artifact=str(path)))
    return [row for row in rows if row]


def directory_rows_from_candidates(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    source = directory_source_kind(path)
    if source not in {"confirmed_candidates", "linkedin_candidates", "llm_reviewed", "parallel_enriched"}:
        return rows
    for row in read_csv_rows(path)[1]:
        linkedin_url, url_col = choose_linkedin_url(row)
        public_identifier = extract_public_identifier(linkedin_url)
        if not public_identifier:
            continue
        confidence = directory_confidence(row, url_col)
        if confidence < 0.75:
            continue
        name = directory_name(row)
        emails = emails_from_row(row)
        phones = phones_from_row(row)
        identities = [(email, "", directory_identity_key(email, "", name, public_identifier)) for email in emails]
        identities.extend(("", phone, directory_identity_key("", phone, name, public_identifier)) for phone in phones)
        if not identities:
            identities = [("", "", directory_identity_key("", "", name, public_identifier))]
        evidence = {
            "source_file": str(path.resolve()),
            "source_kind": source,
            "url_column": url_col,
            "public_identifier": public_identifier,
        }
        for email, phone, source_key in identities:
            rows.append(normalized_directory_row({
                "source": source,
                "source_key": source_key,
                "email": email,
                "phone": phone,
                "name": name,
                "linkedin_url": linkedin_url,
                "confidence": f"{confidence:.2f}",
                "matched_name": name,
                "matched_headline": row.get("matched_headline") or row.get("headline") or row.get("harmonic_headline") or "",
                "evidence": json.dumps(evidence, sort_keys=True),
                "reasoning": row.get("llm_reasoning") or row.get("basis.linkedin_url.reasoning") or "",
                "_priority": directory_source_priority(source, url_col),
                "_url_column": url_col,
            }, source_artifact=str(path)))
    return [row for row in rows if row]


def default_directory_source_paths() -> list[Path]:
    return []


def directory_source_paths(input_cfg: dict[str, Any], artifacts: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for value in unique_strings(input_cfg.get("linkedin_directory_source_csvs")):
        path = Path(value)
        if path.exists():
            paths.append(path)
    if input_cfg.get("linkedin_directory_use_defaults", True):
        paths.extend(default_directory_source_paths())
    return list(dict.fromkeys(paths))


def merge_directory_rows(rows: list[dict[str, str]], existing_by_key: dict[str, dict[str, str]] | None = None) -> list[dict[str, str]]:
    best: dict[str, dict[str, str]] = dict(existing_by_key or {})
    for row in rows:
        normalized = normalized_directory_row(row)
        if not normalized:
            continue
        key = normalized["source_key"]
        current = best.get(key)
        confidence = parse_confidence(normalized.get("confidence"), 0.0)
        priority = int(normalized.get("_priority") or 0)
        if current:
            current_confidence = parse_confidence(current.get("confidence"), 0.0)
            current_priority = int(current.get("_priority") or 0)
            if (confidence, priority) <= (current_confidence, current_priority):
                continue
        best[key] = normalized
    output = []
    for row in sorted(best.values(), key=lambda item: item.get("source_key", "")):
        output.append({col: row.get(col, "") for col in DIRECTORY_COLUMNS})
    return output


def build_directory_checkpoint(input_cfg: dict[str, Any], artifacts: dict[str, Any], *, extra_sources: list[Path] | None = None) -> dict[str, Any]:
    directory_csv = Path(input_cfg.get("linkedin_directory_csv") or artifacts.get("directory_csv") or DEFAULT_DIRECTORY_CSV)
    existing: dict[str, dict[str, str]] = {}
    if directory_csv.exists():
        for row in read_csv_rows(directory_csv)[1]:
            normalized = normalized_directory_row(row, source="directory")
            if normalized:
                existing[normalized["source_key"]] = normalized
    source_paths: list[Path] = []
    for path_text in directory_source_paths(input_cfg, artifacts):
        path = Path(path_text)
        if path.exists() and path != directory_csv:
            source_paths.append(path)
    for path in extra_sources or []:
        if path.exists() and path != directory_csv:
            source_paths.append(path)
    source_paths = list(dict.fromkeys(source_paths))
    imported_rows: list[dict[str, str]] = []
    for path in source_paths:
        kind = directory_source_kind(path)
        if kind == "linkedin_resolutions" or path.name.startswith("linkedin_resolutions"):
            imported_rows.extend(directory_rows_from_resolutions(path))
        else:
            imported_rows.extend(directory_rows_from_candidates(path))
    merged = merge_directory_rows(imported_rows, existing)
    write_csv_rows(directory_csv, DIRECTORY_COLUMNS, merged)
    return {
        "directory_csv": str(directory_csv),
        "existing_rows": len(existing),
        "imported_rows": len(imported_rows),
        "rows": len(merged),
        "source_csvs": [str(path) for path in source_paths],
    }


def load_directory_lookup(directory_csv: Path, min_confidence: float = 0.75) -> dict[str, dict[str, dict[str, str]]]:
    lookup: dict[str, dict[str, dict[str, str]]] = {"email": {}, "phone": {}, "source_key": {}, "name": {}}
    if not directory_csv.exists():
        return lookup
    name_candidates: dict[str, list[dict[str, str]]] = {}
    for row in read_csv_rows(directory_csv)[1]:
        normalized = normalized_directory_row(row, source="directory")
        if not normalized:
            continue
        # Index confirmed not_found entries by email so they're excluded from resolution queues
        row_status = str(normalized.get("status") or "").strip().lower()
        if row_status == "not_found" and normalized.get("email"):
            lookup["email"].setdefault(normalized["email"].lower(), normalized)
            continue
        if (
            not normalized.get("linkedin_url")
            or not normalized.get("public_identifier")
            or parse_confidence(normalized.get("confidence"), 0.0) < min_confidence
        ):
            continue
        if normalized.get("email"):
            lookup["email"][normalized["email"].lower()] = normalized
        if normalized.get("phone"):
            lookup["phone"][normalize_phone(normalized["phone"])] = normalized
        if normalized.get("source_key"):
            lookup["source_key"][normalized["source_key"]] = normalized
        name_key = normalize_name_key(normalized.get("name") or normalized.get("matched_name") or "")
        if name_key:
            name_candidates.setdefault(name_key, []).append(normalized)
    for name_key, rows in name_candidates.items():
        urls = {row.get("linkedin_url") for row in rows}
        if len(urls) == 1:
            lookup["name"][name_key] = rows[0]
    return lookup


def directory_match_for_queue_row(row: dict[str, str], lookup: dict[str, dict[str, dict[str, str]]]) -> dict[str, str] | None:
    emails = emails_from_row(row)
    for email in emails:
        if email in lookup["email"]:
            return lookup["email"][email]
    phones = phones_from_row(row)
    for phone in phones:
        if phone in lookup["phone"]:
            return lookup["phone"][phone]
    name_key = normalize_name_key(directory_name(row))
    if name_key and name_key in lookup["name"]:
        return lookup["name"][name_key]
    return None


def resolution_from_directory_match(queue_row: dict[str, str], directory_row: dict[str, str]) -> dict[str, str]:
    handle = (queue_row.get("handle") or queue_row.get("primary_email") or directory_row.get("email") or directory_row.get("source_key") or "").strip().lower()
    evidence = {
        "source": "directory",
        "directory_source": directory_row.get("source", ""),
        "source_key": directory_row.get("source_key", ""),
        "source_artifact": directory_row.get("source_artifact", ""),
    }
    return {
        "handle": handle,
        "status": "found",
        "linkedin_url": directory_row.get("linkedin_url", ""),
        "confidence": directory_row.get("confidence", ""),
        "matched_name": directory_row.get("matched_name") or directory_row.get("name") or queue_row.get("display_name") or "",
        "matched_headline": directory_row.get("matched_headline", ""),
        "evidence": json.dumps(evidence, sort_keys=True),
        "reasoning": directory_row.get("reasoning") or "Matched from Powerpacks directory.csv",
    }


def resolution_email(row: dict[str, str]) -> str:
    for key in ("handle", "email", "primary_email"):
        value = str(row.get(key) or "").strip().lower()
        if "@" in value:
            return value
    return ""


def normalize_resolution_status(row: dict[str, str], linkedin_url: str) -> str:
    raw_status = str(row.get("status") or "").strip().lower()
    public_identifier = extract_public_identifier(linkedin_url)
    if raw_status in RESOLUTION_FOUND_STATUSES and public_identifier:
        return "found"
    if raw_status in RESOLUTION_NEGATIVE_STATUSES or (raw_status in RESOLUTION_FOUND_STATUSES and not public_identifier):
        return "not_found" if raw_status not in {"failed", "error"} else raw_status
    if public_identifier:
        return "found"
    return raw_status or "not_found"


def normalize_resolution_row(row: dict[str, str]) -> dict[str, str]:
    """Normalize resolver outputs to the Gmail apply-resolutions contract.

    The Parallel primitive writes rows as `email,status=completed/not_found`,
    while Gmail apply expects `handle,status=found/not_found`. This bridge keeps
    both positive and negative outcomes durable so repeated clicks do not spend
    on the same contacts again.
    """
    linkedin_url = normalize_linkedin_url(row.get("linkedin_url") or "")
    status = normalize_resolution_status(row, linkedin_url)
    confidence = parse_confidence(row.get("confidence"), 0.0)
    if status == "found" and confidence <= 0:
        confidence = 0.9
    if status in RESOLUTION_NEGATIVE_STATUSES or status == "not_found":
        confidence = max(confidence, 0.01)
    evidence = str(row.get("evidence") or "")
    if not evidence:
        evidence_payload = {"source": "linkedin_resolution"}
        if row.get("x_handle"):
            evidence_payload["x_handle"] = row.get("x_handle")
        evidence = json.dumps(evidence_payload, sort_keys=True)
    return {
        "handle": resolution_email(row) or str(row.get("handle") or "").strip().lower(),
        "status": status,
        "linkedin_url": linkedin_url,
        "confidence": f"{confidence:.2f}",
        "matched_name": str(row.get("matched_name") or row.get("full_name") or row.get("name") or ""),
        "matched_headline": str(row.get("matched_headline") or row.get("headline") or ""),
        "evidence": evidence,
        "reasoning": str(row.get("reasoning") or ""),
    }


def directory_row_is_found(row: dict[str, str], min_confidence: float = 0.75) -> bool:
    return (
        str(row.get("status") or "").strip().lower() == "found"
        and bool(row.get("linkedin_url"))
        and bool(row.get("public_identifier"))
        and parse_confidence(row.get("confidence"), 0.0) >= min_confidence
    )


def directory_row_is_prior_negative(row: dict[str, str]) -> bool:
    return str(row.get("status") or "").strip().lower() in (RESOLUTION_NEGATIVE_STATUSES | {"not_found"})


def _is_resolvable_person(row: dict[str, str]) -> bool:
    """Return True if the queue row looks like a real person worth resolving."""
    try:
        from packs.ingestion.primitives.resolve_linkedin_queue.resolve_linkedin_queue import (
            is_generic_or_non_person,
            is_likely_person_name,
        )
    except ImportError:
        return True
    email = (row.get("primary_email") or row.get("email") or row.get("handle") or "").strip()
    name = (row.get("display_name") or row.get("full_name") or "").strip()
    if not email or not name:
        return False
    return not is_generic_or_non_person(email) and is_likely_person_name(name)


def apply_directory_to_gmail_queue(record: dict[str, Any], directory_csv: Path, output_dir: Path) -> dict[str, Any]:
    queue_csv = Path(str(record.get("queue_csv") or ""))
    fields, rows = read_csv_rows(queue_csv)
    lookup = load_directory_lookup(directory_csv)
    resolved: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []
    cached_negative: list[dict[str, str]] = []
    filtered_non_person = 0
    for row in rows:
        match = directory_match_for_queue_row(row, lookup)
        if match and directory_row_is_found(match):
            resolved.append(resolution_from_directory_match(row, match))
        elif match and directory_row_is_prior_negative(match):
            cached_negative.append(row)
        elif not _is_resolvable_person(row):
            filtered_non_person += 1
        else:
            unresolved.append(row)
    output_dir.mkdir(parents=True, exist_ok=True)
    resolutions_csv = output_dir / "directory_linkedin_resolutions.csv"
    unresolved_csv = output_dir / "unresolved_linkedin_resolution_queue.csv"
    cached_negative_csv = output_dir / "cached_negative_linkedin_resolution_queue.csv"
    write_csv_rows(resolutions_csv, LINKEDIN_RESOLUTION_COLUMNS, resolved)
    write_csv_rows(unresolved_csv, fields, unresolved)
    write_csv_rows(cached_negative_csv, fields, cached_negative)
    result = dict(record)
    result.update({
        "directory_csv": str(directory_csv),
        "directory_resolutions_csv": str(resolutions_csv),
        "unresolved_queue_csv": str(unresolved_csv),
        "cached_negative_queue_csv": str(cached_negative_csv),
        "input_rows": len(rows),
        "resolved": len(resolved),
        "unresolved": len(unresolved),
        "cached_negative": len(cached_negative),
        "filtered_non_person": filtered_non_person,
    })
    return result


def parse_json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return unique_strings(value)
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = []
    if isinstance(parsed, list):
        return unique_strings(parsed)
    return []


def gmail_directory_source_key(account_email: str, email: str, fallback_id: str = "") -> str:
    account = (account_email or "unknown").strip().lower()
    if email:
        return f"gmail:{account}:email:{email.strip().lower()}"
    return f"gmail:{account}:source:{fallback_id.strip().lower()}"


def directory_rows_from_gmail_queue(record: dict[str, Any]) -> list[dict[str, str]]:
    queue_csv = Path(str(record.get("queue_csv") or ""))
    if not queue_csv.exists():
        return []
    account_email = str(record.get("account_email") or "").strip().lower()
    _fields, rows = read_csv_rows(queue_csv)
    output: list[dict[str, str]] = []
    for row in rows:
        email = str(row.get("primary_email") or row.get("handle") or "").strip().lower()
        if not email:
            continue
        accounts = parse_json_list(row.get("account_emails")) or unique_strings(account_email)
        source_ids = parse_json_list(row.get("source_ids"))
        if not accounts:
            accounts = [account_email or ""]
        if not source_ids:
            source_ids = [""]
        for account in accounts:
            output.append(normalized_directory_row({
                "source": "gmail_msgvault",
                "source_key": gmail_directory_source_key(account, email, row.get("id") or ""),
                "source_account": account,
                "source_id": json.dumps(source_ids, ensure_ascii=False),
                "source_channels": row.get("source_channels") or "gmail_msgvault",
                "status": "observed",
                "email": email,
                "name": row.get("display_name") or row.get("full_name") or "",
                "confidence": "0",
                "evidence": json.dumps({
                    "source": "gmail_msgvault",
                    "queue_csv": str(queue_csv),
                    "account_email": account,
                    "source_ids": source_ids,
                    "total_messages": row.get("total_messages", ""),
                    "thread_count": row.get("thread_count", ""),
                    "last_interaction": row.get("last_interaction", ""),
                }, sort_keys=True),
                "reasoning": "Observed in local Gmail metadata",
            }, source_artifact=str(queue_csv), updated_at=now_iso()))
    return [row for row in output if row]


def commit_directory_rows(directory_csv: Path, rows: list[dict[str, str]]) -> dict[str, Any]:
    existing: dict[str, dict[str, str]] = {}
    if directory_csv.exists():
        for row in read_csv_rows(directory_csv)[1]:
            normalized = normalized_directory_row(row, source="directory")
            if normalized:
                existing[normalized["source_key"]] = normalized
    merged = merge_directory_rows(rows, existing)
    write_csv_rows(directory_csv, DIRECTORY_COLUMNS, merged)
    return {"directory_csv": str(directory_csv), "existing_rows": len(existing), "imported_rows": len(rows), "rows": len(merged)}


def people_directory_source_key(row: dict[str, str], source: str, source_account: str, public_identifier: str) -> str:
    source_name = (source or row.get("source_channels") or "people").strip().lower()
    account = (source_account or "").strip().lower()
    email = str(row.get("primary_email") or "").strip().lower()
    phone = normalize_phone(row.get("primary_phone") or "")
    if source_name == "gmail_msgvault" and account and email:
        return gmail_directory_source_key(account, email)
    if source_name == "linkedin_csv":
        return f"linkedin_csv:{account or 'local'}:linkedin:{public_identifier}"
    if source_name == "messages":
        if phone:
            return f"messages:phone:{phone}"
        return f"messages:linkedin:{public_identifier}"
    if email:
        return f"{source_name}:email:{email}"
    if phone:
        return f"{source_name}:phone:{phone}"
    return f"{source_name}:linkedin:{public_identifier}"


def directory_rows_from_people_csv(path: Path, *, source: str = "", source_account: str = "") -> list[dict[str, str]]:
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    for row in read_csv_rows(path)[1]:
        linkedin_url = normalize_linkedin_url(row.get("linkedin_url") or "")
        public_identifier = extract_public_identifier(linkedin_url)
        if not public_identifier:
            continue
        source_name = source or (row.get("source_channels") or "").split(",", 1)[0] or directory_source_kind(path)
        row_source_account = source_account
        if not row_source_account and source_name == "messages":
            row_source_account = row.get("source_channels") or source_name
        email = str(row.get("primary_email") or "").strip().lower()
        phone = normalize_phone(row.get("primary_phone") or "")
        rows.append(normalized_directory_row({
            "source": source_name,
            "source_key": people_directory_source_key(row, source_name, row_source_account, public_identifier),
            "source_account": row_source_account,
            "source_id": row.get("id") or "",
            "source_channels": row.get("source_channels") or source_name,
            "status": "found",
            "email": email,
            "phone": phone,
            "name": row.get("full_name") or directory_name(row),
            "linkedin_url": linkedin_url,
            "confidence": "1.00",
            "matched_name": row.get("full_name") or directory_name(row),
            "matched_headline": row.get("headline") or "",
            "evidence": json.dumps({
                "source": source_name,
                "people_csv": str(path),
                "public_identifier": public_identifier,
            }, sort_keys=True),
            "reasoning": "Confirmed by enriched source people artifact",
            "_priority": 95 if source_name in {"linkedin_csv", "messages"} else 80,
        }, source_artifact=str(path), updated_at=now_iso()))
    return [row for row in rows if row]


def commit_people_csv_to_directory(
    input_cfg: dict[str, Any],
    artifacts: dict[str, Any],
    people_csv: str,
    *,
    source: str,
    source_account: str = "",
) -> dict[str, Any]:
    path = Path(str(people_csv or ""))
    directory_csv = Path(input_cfg.get("linkedin_directory_csv") or artifacts.get("directory_csv") or DEFAULT_DIRECTORY_CSV)
    rows = directory_rows_from_people_csv(path, source=source, source_account=source_account)
    result = commit_directory_rows(directory_csv, rows)
    result.update({"source": source, "source_account": source_account, "people_csv": str(path), "confirmed_rows": len(rows)})
    artifacts["directory_csv"] = str(directory_csv)
    return result


def commit_gmail_observations_to_directory(input_cfg: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    directory_csv = Path(input_cfg.get("linkedin_directory_csv") or artifacts.get("directory_csv") or DEFAULT_DIRECTORY_CSV)
    rows: list[dict[str, str]] = []
    for record in gmail_queue_records(artifacts):
        if isinstance(record, dict):
            rows.extend(directory_rows_from_gmail_queue(record))
    result = commit_directory_rows(directory_csv, rows)
    result["gmail_observation_rows"] = len(rows)
    artifacts["directory_csv"] = str(directory_csv)
    artifacts["gmail_directory_observation_checkpoint"] = result
    return result


def commit_gmail_resolutions_to_directory(input_cfg: dict[str, Any], artifacts: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    directory_csv = Path(input_cfg.get("linkedin_directory_csv") or artifacts.get("directory_csv") or DEFAULT_DIRECTORY_CSV)
    rows: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, dict) or not record.get("resolutions_csv"):
            continue
        account_email = str(record.get("account_email") or "").strip().lower()
        resolution_path = Path(str(record["resolutions_csv"]))
        if not resolution_path.exists():
            continue
        for raw_resolution in read_csv_rows(resolution_path)[1]:
            resolution = normalize_resolution_row(raw_resolution)
            email = str(resolution.get("handle") or "").strip().lower()
            if "@" not in email:
                continue
            linkedin_url = normalize_linkedin_url(resolution.get("linkedin_url") or "")
            public_identifier = extract_public_identifier(linkedin_url)
            status = str(resolution.get("status") or "").strip().lower()
            confidence = parse_confidence(resolution.get("confidence"), 0.0)
            if status == "found":
                if not public_identifier or confidence < 0.75:
                    continue
            elif status in (RESOLUTION_NEGATIVE_STATUSES | {"not_found"}):
                status = "not_found" if status not in {"failed", "error"} else status
                linkedin_url = ""
                confidence = max(confidence, 0.01)
            else:
                continue
            evidence = {
                "source": "gmail_linkedin_resolution",
                "account_email": account_email,
                "resolutions_csv": record.get("resolutions_csv"),
                "resolution_evidence": resolution.get("evidence", ""),
            }
            rows.append(normalized_directory_row({
                "source": "gmail_msgvault",
                "source_key": gmail_directory_source_key(account_email, email),
                "source_account": account_email,
                "source_channels": "gmail_msgvault",
                "status": status,
                "email": email,
                "name": resolution.get("matched_name") or "",
                "linkedin_url": linkedin_url,
                "confidence": f"{confidence:.2f}",
                "matched_name": resolution.get("matched_name") or "",
                "matched_headline": resolution.get("matched_headline") or "",
                "evidence": json.dumps(evidence, sort_keys=True),
                "reasoning": resolution.get("reasoning") or "",
                "_priority": 82,
            }, source_artifact=str(record.get("resolutions_csv") or ""), updated_at=now_iso()))
    result = commit_directory_rows(directory_csv, rows)
    result["gmail_resolution_rows"] = len(rows)
    result["gmail_resolution_found_rows"] = sum(1 for row in rows if row.get("status") == "found")
    result["gmail_resolution_negative_rows"] = sum(1 for row in rows if row.get("status") in (RESOLUTION_NEGATIVE_STATUSES | {"not_found"}))
    artifacts["directory_csv"] = str(directory_csv)
    artifacts["gmail_directory_resolution_checkpoint"] = result
    return result


def merge_resolution_rows(resolution_paths: list[Path]) -> list[dict[str, str]]:
    best: dict[str, dict[str, str]] = {}
    for path in resolution_paths:
        if not path.exists():
            continue
        for raw_row in read_csv_rows(path)[1]:
            row = normalize_resolution_row(raw_row)
            status = (row.get("status") or "").strip().lower()
            linkedin_url = normalize_linkedin_url(row.get("linkedin_url") or "")
            public_identifier = extract_public_identifier(linkedin_url)
            handle = (row.get("handle") or "").strip().lower()
            confidence = parse_confidence(row.get("confidence"), 0.0)
            if status != "found" or not public_identifier or not handle or confidence < 0.75:
                continue
            candidate = {col: row.get(col, "") for col in LINKEDIN_RESOLUTION_COLUMNS}
            candidate["handle"] = handle
            candidate["status"] = "found"
            candidate["linkedin_url"] = linkedin_url
            candidate["confidence"] = f"{confidence:.2f}"
            current = best.get(handle)
            if current and parse_confidence(current.get("confidence"), 0.0) >= confidence:
                continue
            best[handle] = candidate
    return [best[key] for key in sorted(best)]


def combine_gmail_resolution_records(records: list[dict[str, Any]], run_dir: Path) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not record.get("people_csv") or not record.get("resolutions_csv"):
            continue
        slug = source_slug(record.get("account_email") or record.get("slug") or record.get("people_csv") or "all")
        key = (slug, str(record["people_csv"]))
        group = grouped.setdefault(key, {"account_email": record.get("account_email", ""), "slug": slug, "people_csv": record["people_csv"], "resolution_paths": []})
        group["resolution_paths"].append(Path(str(record["resolutions_csv"])))
    combined: list[dict[str, Any]] = []
    for (slug, people_csv), group in sorted(grouped.items(), key=lambda item: item[0]):
        rows = merge_resolution_rows(group["resolution_paths"])
        if not rows:
            continue
        out_dir = run_dir / f"gmail-combined-resolutions-{slug}"
        out_path = out_dir / "linkedin_resolutions.csv"
        write_csv_rows(out_path, LINKEDIN_RESOLUTION_COLUMNS, rows)
        combined.append({
            "account_email": group.get("account_email", ""),
            "slug": slug,
            "people_csv": people_csv,
            "resolutions_csv": str(out_path),
            "resolution_sources": [str(path) for path in group["resolution_paths"]],
            "resolved": len(rows),
        })
    return combined


def materialize_gmail_provider_resolution_records(output_csv: str, queue_records: list[dict[str, Any]], run_dir: Path) -> list[dict[str, Any]]:
    """Split a combined provider output into per-account normalized resolution CSVs."""
    output_path = Path(str(output_csv or ""))
    if not output_path.exists():
        return []
    provider_rows_by_email: dict[str, dict[str, str]] = {}
    for raw_row in read_csv_rows(output_path)[1]:
        row = normalize_resolution_row(raw_row)
        email = resolution_email(row)
        if email:
            provider_rows_by_email[email] = row
    records: list[dict[str, Any]] = []
    for index, record in enumerate(queue_records):
        queue_path = Path(str(record.get("queue_csv") or ""))
        if not queue_path.exists():
            continue
        rows: list[dict[str, str]] = []
        for queue_row in read_csv_rows(queue_path)[1]:
            email = resolution_email(queue_row)
            if email and email in provider_rows_by_email:
                rows.append(provider_rows_by_email[email])
        if not rows:
            continue
        slug = source_slug(record.get("account_email") or record.get("slug") or f"queue-{index}")
        out_dir = run_dir / f"gmail-linkedin-resolution-{slug}"
        out_path = out_dir / "linkedin_resolutions.csv"
        write_csv_rows(out_path, LINKEDIN_RESOLUTION_COLUMNS, rows)
        found = sum(1 for row in rows if row.get("status") == "found")
        negative = sum(1 for row in rows if row.get("status") in (RESOLUTION_NEGATIVE_STATUSES | {"not_found"}))
        records.append({
            "account_email": record.get("account_email", ""),
            "resolutions_csv": str(out_path),
            "people_csv": record.get("people_csv"),
            "slug": slug,
            "source": "parallel",
            "raw_resolutions_csv": str(output_path),
            "processed": len(rows),
            "found": found,
            "not_found": negative,
        })
    return records


def merge_people_values(current: str, incoming: str) -> str:
    if not current and incoming:
        return incoming
    if incoming and current in {"", "[]", "{}"}:
        return incoming
    if incoming and len(incoming) > len(current) and current in incoming:
        return incoming
    return current


def merge_jsonish_lists(current: str, incoming: str) -> str:
    values: list[str] = []
    for value in (current, incoming):
        if not value:
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            values.extend(str(item) for item in parsed if str(item).strip())
        else:
            values.extend(part.strip() for part in str(value).split(",") if part.strip())
    return json.dumps(sorted(set(values)), ensure_ascii=False) if values else ""


def union_alias_list(current: str, incoming: str, primary_current: str = "", primary_incoming: str = "") -> str:
    """Set-union an all_emails/all_phones column, preserving first-seen order.

    Distinct work emails that resolve to the same LinkedIn person must accumulate
    here rather than overwrite each other (the resolved Gmail address used to be
    discarded when two rows collapsed onto one public_identifier). The matching
    primary_email/primary_phone values are folded in so a single-email row that
    only populated primary_* still contributes its address to the union.
    """
    seen: list[str] = []
    for value in (primary_current, primary_incoming):
        value = (value or "").strip()
        if value and value not in seen:
            seen.append(value)
    for blob in (current, incoming):
        parsed = parse_jsonish(blob, None)
        values = parsed if isinstance(parsed, list) else [part for part in re.split(r"[,;]", str(blob or "")) if part.strip()]
        for value in values:
            value = str(value).strip()
            if value and value not in seen:
                seen.append(value)
    return json.dumps(seen, ensure_ascii=False) if seen else ""


def materialize_source_merged_people_csv(input_csvs: list[str], output_csv: Path, *, default_source_channels: str) -> dict[str, Any]:
    """Write one stable source people artifact from one or more run outputs."""
    merged: dict[str, dict[str, str]] = {}
    for path_text in unique_strings(input_csvs):
        path = Path(path_text)
        if not path.exists():
            continue
        for raw in read_csv_rows(path)[1]:
            row = normalize_people_row(raw)
            public_identifier = row.get("public_identifier") or extract_public_identifier(row.get("linkedin_url") or "")
            key = f"linkedin:{public_identifier}" if public_identifier else ""
            if not key:
                email = str(row.get("primary_email") or "").strip().lower()
                key = f"email:{email}" if email else str(row.get("id") or "").strip()
            if not key:
                continue
            row["source_channels"] = row.get("source_channels") or default_source_channels
            row["source_artifacts"] = merge_jsonish_lists(row.get("source_artifacts", ""), str(path))
            if key not in merged:
                current = {col: row.get(col, "") for col in PEOPLE_SCHEMA_COLUMNS}
                # Seed the alias lists from this row's primary value so a single
                # row already carries its email/phone in all_emails/all_phones.
                for col in LIST_VALUE_COLUMNS:
                    primary_col = "primary_email" if col == "all_emails" else "primary_phone"
                    current[col] = union_alias_list(row.get(col, ""), "", row.get(primary_col, ""))
                merged[key] = current
                continue
            current = merged[key]
            for col in PEOPLE_SCHEMA_COLUMNS:
                if col == "source_channels":
                    current[col] = ",".join(unique_strings((current.get(col, "").split(",") if current.get(col) else []) + (row.get(col, "").split(",") if row.get(col) else [])))
                elif col == "source_artifacts":
                    current[col] = merge_jsonish_lists(current.get(col, ""), row.get(col, ""))
                elif col in LIST_VALUE_COLUMNS:
                    primary_col = "primary_email" if col == "all_emails" else "primary_phone"
                    current[col] = union_alias_list(current.get(col, ""), row.get(col, ""), current.get(primary_col, ""), row.get(primary_col, ""))
                elif col == "interaction_counts":
                    counts = merge_interaction_counts(current.get(col, ""), row.get(col, ""))
                    current[col] = json.dumps(counts, ensure_ascii=False) if counts else ""
                elif col == "last_interaction":
                    current[col] = max(current.get(col, ""), row.get(col, ""))
                else:
                    current[col] = merge_people_values(current.get(col, ""), row.get(col, ""))
            # Keep primary_email/primary_phone consistent with the union: keep the
            # existing primary, else promote the first aliased value.
            for col in LIST_VALUE_COLUMNS:
                primary_col = "primary_email" if col == "all_emails" else "primary_phone"
                if not current.get(primary_col):
                    aliases = parse_jsonish(current.get(col, ""), [])
                    if isinstance(aliases, list) and aliases:
                        current[primary_col] = str(aliases[0])
    rows = [merged[key] for key in sorted(merged)]
    if not rows:
        return {"status": "skipped", "people_csv": str(output_csv), "rows": 0, "input_csvs": input_csvs}
    write_csv_rows(output_csv, PEOPLE_SCHEMA_COLUMNS, rows)
    return {"status": "completed", "people_csv": str(output_csv), "rows": len(rows), "input_csvs": input_csvs}


def materialize_gmail_merged_people_csv(input_csvs: list[str], output_csv: Path) -> dict[str, Any]:
    """Write one stable Gmail people artifact from all Gmail account outputs."""
    return materialize_source_merged_people_csv(input_csvs, output_csv, default_source_channels="gmail_msgvault")


def materialize_messages_merged_people_csv(input_csvs: list[str], output_csv: Path) -> dict[str, Any]:
    """Write one stable Messages people artifact from reviewed/enriched outputs."""
    return materialize_source_merged_people_csv(input_csvs, output_csv, default_source_channels="messages")


def sha(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


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


def unique_strings(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw = [values]
    elif isinstance(values, list):
        raw = values
    else:
        raw = [values]
    out: list[str] = []
    for value in raw:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def source_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip().lower()).strip("-._")
    return slug or "source"


def truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def normalize_bool(value: Any) -> bool | None:
    raw = str(value or "").strip().lower()
    if raw in TRUTHY:
        return True
    if raw in FALSY:
        return False
    return None


def normalize_include_decision(value: Any) -> bool | None:
    raw = str(value or "").strip().lower()
    if raw in INCLUDE_DECISIONS:
        return True
    if raw in EXCLUDE_DECISIONS:
        return False
    return None


def normalize_exclude_decision(value: Any) -> bool | None:
    raw = str(value or "").strip().lower()
    if raw in TRUTHY:
        return False
    if raw in FALSY:
        return True
    return None


def explicitly_approved_message_review_row(row: dict[str, str]) -> bool:
    """Return True only for explicit human/upload approval, never bucket defaults."""
    approved = normalize_bool(row.get("approved", ""))
    upload_decision = normalize_include_decision(row.get("upload_decision", ""))
    exclude_decision = normalize_exclude_decision(row.get("exclude", ""))
    if approved is False or upload_decision is False or exclude_decision is False:
        return False
    return approved is True or upload_decision is True or exclude_decision is True


def review_row_to_messages_contact(row: dict[str, str]) -> dict[str, str]:
    name = row.get("network_name") or row.get("full_name") or ""
    return {
        "name": name,
        "phone": row.get("phone_e164", ""),
        "source": row.get("message_source") or "messages_review",
        "message_count": row.get("total_messages", ""),
        "last_message": row.get("last_message", ""),
        "matched_linkedin_url": row.get("network_linkedin_url", ""),
        "matched_name": name,
        "matched_person_id": row.get("network_person_id", ""),
        "match_method": row.get("network_match_method") or row.get("review_source") or "messages_review_approved",
    }


def materialize_approved_messages_review(review_csv: Path, scratch: Path) -> dict[str, Any] | None:
    if not review_csv.exists():
        return None
    _fields, rows = read_csv_rows(review_csv)
    approved_rows = [review_row_to_messages_contact(row) for row in rows if explicitly_approved_message_review_row(row)]
    if not approved_rows:
        return {
            "review_csv": str(review_csv),
            "approved_rows": 0,
            "total_rows": len(rows),
            "contacts_csv": "",
        }
    fields = ["name", "phone", "source", "message_count", "last_message", "matched_linkedin_url", "matched_name", "matched_person_id", "match_method"]
    write_csv_rows(scratch, fields, approved_rows)
    return {
        "review_csv": str(review_csv),
        "approved_rows": len(approved_rows),
        "total_rows": len(rows),
        "contacts_csv": str(scratch),
    }


def messages_review_linkedin_url(row: dict[str, str]) -> str:
    for key in ("retarget_linkedin_url", "linkedin_url", "network_linkedin_url"):
        normalized = normalize_linkedin_url(row.get(key, ""))
        if extract_public_identifier(normalized):
            return normalized
    return ""


def messages_review_row_rejected(row: dict[str, str]) -> bool:
    approved = normalize_bool(row.get("approved", ""))
    upload_decision = normalize_include_decision(row.get("upload_decision", ""))
    exclude_decision = normalize_exclude_decision(row.get("exclude", ""))
    enrich_decision = normalize_include_decision(row.get("enrich_decision", ""))
    return approved is False or upload_decision is False or exclude_decision is False or enrich_decision is False


def messages_review_row_in_network(row: dict[str, str]) -> bool:
    raw = normalize_bool(row.get("in_network", ""))
    if raw is not None:
        return raw
    return bool((row.get("network_person_id") or "").strip())


def messages_review_row_approved_for_local(row: dict[str, str]) -> bool:
    if messages_review_row_rejected(row):
        return False
    if explicitly_approved_message_review_row(row):
        return True
    return (row.get("bucket") or "").strip().lower() in {"yes", "confident"}


def messages_review_row_researched(row: dict[str, str]) -> bool:
    review_source = (row.get("review_source") or "").strip().lower()
    if review_source in RESEARCH_REVIEW_SOURCES:
        return True
    if review_source and review_source != "in_network_match":
        return True
    return any((row.get(key) or "").strip() for key in ("top_title_company_pairs", "top_titles", "top_companies", "schools", "short_reason"))


def messages_review_people_selection_reason(row: dict[str, str]) -> str:
    if messages_review_row_rejected(row):
        return ""
    if not messages_review_linkedin_url(row):
        return ""
    if messages_review_row_in_network(row):
        return "in_network"
    if messages_review_row_approved_for_local(row):
        return "approved"
    if normalize_include_decision(row.get("enrich_decision", "")) is True:
        return "enrich_decision"
    if messages_review_row_researched(row):
        return "researched"
    return ""


def split_full_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


def split_title_company(pair_text: str) -> tuple[str, str]:
    first = next((part.strip() for part in re.split(r"\s*\|\s*", pair_text or "") if part.strip()), "")
    for marker in (" at ", " @ ", " - "):
        if marker in first:
            title, company = first.split(marker, 1)
            return title.strip(), company.strip()
    return first.strip(), ""


def messages_source_channels(row: dict[str, str]) -> list[str]:
    channels: list[str] = []
    raw = (row.get("message_source") or "").strip().lower()
    for token in re.split(r"[,|+/;\s]+", raw):
        if token in {"imessage", "whatsapp"} and token not in channels:
            channels.append(token)
    for key, channel in (("imessage_message_count", "imessage"), ("whatsapp_message_count", "whatsapp")):
        try:
            count = int(float(row.get(key) or 0))
        except ValueError:
            count = 0
        if count > 0 and channel not in channels:
            channels.append(channel)
    return channels or ["messages"]


def review_row_to_messages_people(row: dict[str, str], review_csv: Path, reason: str) -> dict[str, str]:
    linkedin_url = messages_review_linkedin_url(row)
    public_identifier = extract_public_identifier(linkedin_url)
    full_name = (row.get("network_name") if reason == "in_network" else "") or row.get("full_name") or row.get("network_name") or ""
    first_name, last_name = split_full_name(full_name)
    current_title, current_company = split_title_company(row.get("top_title_company_pairs") or row.get("top_titles", ""))
    phone = (row.get("phone_e164") or "").strip()
    channels = messages_source_channels(row)
    summary_parts = [
        f"messages_total={row.get('total_messages') or '0'}",
        f"selection={reason}",
    ]
    if row.get("last_message"):
        summary_parts.append(f"last_message={row.get('last_message')}")
    if row.get("short_reason"):
        summary_parts.append(f"review_reason={row.get('short_reason')}")
    people = {
        "id": row.get("network_person_id") or f"message-linkedin:{sha(public_identifier or linkedin_url, 16)}",
        "public_identifier": public_identifier,
        "linkedin_url": linkedin_url,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "headline": row.get("top_title_company_pairs") or row.get("top_titles", ""),
        "summary": "; ".join(part for part in summary_parts if part),
        "city": row.get("location_city", ""),
        "country": row.get("location_country", ""),
        "current_title": current_title,
        "current_company": current_company,
        "primary_phone": phone,
        "all_phones": json.dumps([phone], ensure_ascii=False) if phone else "",
        "source_channels": ",".join(channels),
        "source_artifacts": str(review_csv),
        "enrichment_provider": f"messages_review:{reason}",
    }
    return normalize_people_row(people)


def merge_messages_people_candidate(existing: dict[str, str], incoming: dict[str, str]) -> dict[str, str]:
    merged = dict(existing)
    for key in ("primary_phone", "full_name", "first_name", "last_name", "headline", "summary", "city", "country", "current_title", "current_company"):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming[key]
    phones = unique_strings([merged.get("primary_phone", ""), incoming.get("primary_phone", "")])
    if phones:
        merged["primary_phone"] = merged.get("primary_phone") or phones[0]
        merged["all_phones"] = json.dumps(phones, ensure_ascii=False)
    channels = unique_strings((merged.get("source_channels", "").split(",") if merged.get("source_channels") else []) + (incoming.get("source_channels", "").split(",") if incoming.get("source_channels") else []))
    if channels:
        merged["source_channels"] = ",".join(channels)
    providers = unique_strings([merged.get("enrichment_provider", ""), incoming.get("enrichment_provider", "")])
    if providers:
        merged["enrichment_provider"] = ",".join(providers)
    return normalize_people_row(merged)


def materialize_messages_review_people(review_csv: Path, output_csv: Path, manifest_path: Path | None = None) -> dict[str, Any]:
    if not review_csv.exists():
        return {
            "review_csv": str(review_csv),
            "people_csv": "",
            "total_rows": 0,
            "eligible_rows": 0,
            "rows_written": 0,
            "selection_counts": {},
            "skipped": {"missing_review_csv": 1},
        }
    _fields, rows = read_csv_rows(review_csv)
    by_public_identifier: dict[str, dict[str, str]] = {}
    selection_counts: dict[str, int] = {}
    skipped = {
        "rejected": 0,
        "missing_linkedin_url": 0,
        "not_selected": 0,
        "duplicate_public_identifier": 0,
    }
    eligible_rows = 0
    for row in rows:
        linkedin_url = messages_review_linkedin_url(row)
        if not linkedin_url:
            skipped["missing_linkedin_url"] += 1
            continue
        if messages_review_row_rejected(row):
            skipped["rejected"] += 1
            continue
        reason = messages_review_people_selection_reason(row)
        if not reason:
            skipped["not_selected"] += 1
            continue
        eligible_rows += 1
        selection_counts[reason] = selection_counts.get(reason, 0) + 1
        candidate = review_row_to_messages_people(row, review_csv, reason)
        public_identifier = candidate.get("public_identifier", "")
        if public_identifier in by_public_identifier:
            skipped["duplicate_public_identifier"] += 1
            by_public_identifier[public_identifier] = merge_messages_people_candidate(by_public_identifier[public_identifier], candidate)
        else:
            by_public_identifier[public_identifier] = candidate
    output_rows = [by_public_identifier[key] for key in sorted(by_public_identifier)]
    if output_rows:
        write_csv_rows(output_csv, PEOPLE_SCHEMA_COLUMNS, output_rows)
    summary = {
        "review_csv": str(review_csv),
        "people_csv": str(output_csv) if output_rows else "",
        "total_rows": len(rows),
        "eligible_rows": eligible_rows,
        "rows_written": len(output_rows),
        "selection_counts": selection_counts,
        "skipped": skipped,
    }
    if manifest_path:
        write_json(manifest_path, summary)
        summary["manifest"] = str(manifest_path)
    return summary


def skip_msgvault_sync(input_cfg: dict[str, Any]) -> bool:
    return bool(input_cfg.get("skip_msgvault_sync") or truthy_env("POWERPACKS_SKIP_MSGVAULT_SYNC"))


def normalize_label_names(values: Any) -> list[str]:
    out: list[str] = []
    for value in unique_strings(values):
        label = value.strip().upper()
        if label and label not in out:
            out.append(label)
    return out


def gmail_excluded_labels(input_cfg: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    if not bool(input_cfg.get("include_category_mail")):
        labels.extend(DEFAULT_GMAIL_EXCLUDED_LABELS)
    labels.extend(normalize_label_names(input_cfg.get("gmail_exclude_labels")))
    return normalize_label_names(labels)


def gmail_label_to_query_term(label: str) -> str:
    normalized = str(label or "").strip().upper()
    if not normalized:
        return ""
    if normalized in GMAIL_LABEL_QUERY_TERMS:
        return GMAIL_LABEL_QUERY_TERMS[normalized]
    if normalized.startswith("CATEGORY_"):
        return f"-category:{normalized.removeprefix('CATEGORY_').lower()}"
    return f"-label:{normalized}"


def gmail_sync_query(input_cfg: dict[str, Any]) -> str:
    explicit = str(input_cfg.get("gmail_sync_query") or "").strip()
    if explicit:
        return explicit
    return " ".join(term for term in (gmail_label_to_query_term(label) for label in gmail_excluded_labels(input_cfg)) if term)


def gmail_sync_after(input_cfg: dict[str, Any]) -> str:
    value = str(input_cfg.get("gmail_sync_after") or "").strip()
    return value if DATE_ONLY_RE.fullmatch(value) else ""


def parse_msgvault_sync_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if DATE_ONLY_RE.fullmatch(text[:10]):
        return text[:10]
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        if numeric > 10_000_000_000:
            numeric = numeric / 1000
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def sqlite_table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def infer_msgvault_sync_after(db: str, email: str) -> dict[str, str]:
    path = Path(db or DEFAULT_MSGVAULT_DB).expanduser()
    if not email or not path.exists():
        return {}
    uri = f"file:{urllib.parse.quote(str(path), safe='/')}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=1)
    except sqlite3.Error:
        return {}
    try:
        source_cols = sqlite_table_columns(con, "sources")
        if not {"id", "source_type", "identifier"}.issubset(source_cols):
            return {}
        select_cols = ["id"]
        if "last_sync_at" in source_cols:
            select_cols.append("last_sync_at")
        source = con.execute(
            f"SELECT {', '.join(select_cols)} FROM sources WHERE lower(source_type) = 'gmail' AND lower(identifier) = lower(?) ORDER BY id DESC LIMIT 1",
            (email,),
        ).fetchone()
        if not source:
            return {}
        source_id = source[0]
        if "last_sync_at" in source_cols:
            source_date = parse_msgvault_sync_date(source[1])
            if source_date:
                return {"sync_after": source_date, "source": "msgvault.sources.last_sync_at"}

        message_cols = sqlite_table_columns(con, "messages")
        if "source_id" not in message_cols:
            return {}
        candidates: list[tuple[str, str]] = []
        for column in ("internal_date", "sent_at", "received_at"):
            if column not in message_cols:
                continue
            row = con.execute(f"SELECT max({column}) FROM messages WHERE source_id = ?", (source_id,)).fetchone()
            date = parse_msgvault_sync_date(row[0] if row else "")
            if date:
                candidates.append((date, f"msgvault.messages.{column}"))
        if not candidates:
            return {}
        date, source_name = max(candidates, key=lambda item: item[0])
        return {"sync_after": date, "source": source_name}
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def msgvault_home_for_db(db: str) -> Path:
    if db:
        return Path(db).expanduser().parent
    return DEFAULT_MSGVAULT_DB.expanduser().parent


def read_msgvault_config(home: Path) -> dict[str, Any]:
    config = home / "config.toml"
    if not config.exists():
        return {}
    try:
        return tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def configured_client_secret_paths(home: Path) -> list[Path]:
    paths: list[Path] = []
    cfg = read_msgvault_config(home)
    oauth = cfg.get("oauth") if isinstance(cfg.get("oauth"), dict) else {}
    default_secret = oauth.get("client_secrets") if isinstance(oauth.get("client_secrets"), str) else ""
    if default_secret:
        paths.append(Path(default_secret).expanduser())
    apps = oauth.get("apps") if isinstance(oauth.get("apps"), dict) else {}
    for app in apps.values():
        if isinstance(app, dict) and isinstance(app.get("client_secrets"), str):
            paths.append(Path(app["client_secrets"]).expanduser())
    fallback = home / "client_secret.json"
    if fallback not in paths:
        paths.append(fallback)
    out: list[Path] = []
    for path in paths:
        if path not in out:
            out.append(path)
    return out


def load_oauth_client(home: Path, client_id: str = "") -> tuple[dict[str, Any] | None, str]:
    last_error = ""
    for path in configured_client_secret_paths(home):
        if not path.exists():
            last_error = f"configured client secret not found: {path}"
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            last_error = f"could not read configured client secret: {exc}"
            continue
        installed = data.get("installed") if isinstance(data, dict) else None
        if not isinstance(installed, dict):
            last_error = "configured client secret is not an installed-app OAuth JSON"
            continue
        if client_id and installed.get("client_id") != client_id:
            last_error = "configured client secret does not match token client_id"
            continue
        if not installed.get("client_id") or not installed.get("client_secret"):
            last_error = "configured client secret is missing client_id/client_secret"
            continue
        return installed, ""
    return None, last_error or "msgvault OAuth client secret is not configured"


def token_path_for_email(home: Path, email: str) -> Path:
    exact = home / "tokens" / f"{email}.json"
    if exact.exists():
        return exact
    lower = home / "tokens" / f"{email.lower()}.json"
    if lower.exists():
        return lower
    return exact


def parse_google_expiry(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def token_needs_refresh(token: dict[str, Any]) -> bool:
    expiry = parse_google_expiry(token.get("expiry"))
    if expiry is None:
        return True
    return expiry <= datetime.now(timezone.utc) + timedelta(minutes=5)


def refresh_google_token(token: dict[str, Any], client: dict[str, Any], token_path: Path) -> tuple[dict[str, Any] | None, str]:
    refresh_token = str(token.get("refresh_token") or "")
    if not refresh_token:
        return None, "OAuth token has no refresh_token; reauthorize the Gmail account"
    token_uri = str(client.get("token_uri") or "https://oauth2.googleapis.com/token")
    form = urllib.parse.urlencode({
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    request = urllib.request.Request(token_uri, data=form, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            refreshed = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        return None, f"token refresh failed: {exc}"
    if not refreshed.get("access_token"):
        return None, "token refresh did not return an access_token"
    updated = dict(token)
    updated.update({
        "access_token": refreshed["access_token"],
        "token_type": refreshed.get("token_type", token.get("token_type") or "Bearer"),
        "expires_in": refreshed.get("expires_in", token.get("expires_in")),
        "expiry": (datetime.now(timezone.utc) + timedelta(seconds=int(refreshed.get("expires_in") or 3600))).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    })
    try:
        token_path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(token_path, 0o600)
    except OSError:
        pass
    return updated, ""


def gmail_api_get(access_token: str, path: str, params: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    query = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None})
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/{path}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8")), ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
        except OSError:
            body = ""
        return None, f"Gmail API HTTP {exc.code}: {body[:300] or exc.reason}"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"Gmail API request failed: {exc}"


def gmail_label_totals(access_token: str, labels: Iterable[str] = GMAIL_ESTIMATE_LABEL_IDS) -> tuple[dict[str, int], str]:
    totals: dict[str, int] = {}
    for label in labels:
        payload, error = gmail_api_get(access_token, f"labels/{label}", {"fields": "id,messagesTotal,threadsTotal"})
        if error:
            return totals, error
        totals[label] = int((payload or {}).get("messagesTotal") or 0)
    return totals, ""


def gmail_message_id_count(access_token: str, query: str, *, max_pages: int = DEFAULT_GMAIL_ESTIMATE_MAX_PAGES) -> tuple[dict[str, Any], str]:
    page_token = ""
    counted = 0
    pages = 0
    while True:
        params: dict[str, Any] = {"maxResults": 500, "fields": "messages/id,nextPageToken"}
        if query.strip():
            params["q"] = query.strip()
        if page_token:
            params["pageToken"] = page_token
        payload, error = gmail_api_get(access_token, "messages", params)
        if error:
            return {"count": counted, "complete": False, "pages": pages, "page_size": 500}, error
        pages += 1
        counted += len((payload or {}).get("messages") or [])
        page_token = str((payload or {}).get("nextPageToken") or "")
        if not page_token:
            return {"count": counted, "complete": True, "pages": pages, "page_size": 500}, ""
        if pages >= max_pages:
            return {"count": counted, "complete": False, "pages": pages, "page_size": 500, "truncated_at": counted}, ""


def estimate_gmail_account_via_api(home: Path, email: str, sync_query: str, excluded_labels: list[str], *, max_pages: int = DEFAULT_GMAIL_ESTIMATE_MAX_PAGES) -> dict[str, Any]:
    estimate: dict[str, Any] = {
        "scope": "gmail_api",
        "account_email": email,
        "sync_query": sync_query,
        "excluded_labels": excluded_labels,
        "privacy": {"message_bodies_read": False, "message_subjects_read": False, "snippets_read": False, "message_ids_listed": max_pages > 0},
    }
    if not email:
        estimate.update({"status": "skipped", "message": "No Gmail account email was provided."})
        return estimate
    token_path = token_path_for_email(home, email)
    if not token_path.exists():
        estimate.update({"status": "token_missing", "message": "No msgvault OAuth token found for this Gmail account."})
        return estimate
    try:
        token = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        estimate.update({"status": "token_invalid", "message": f"Could not read msgvault OAuth token: {exc}"})
        return estimate
    client, error = load_oauth_client(home, str(token.get("client_id") or ""))
    if error or client is None:
        estimate.update({"status": "oauth_client_missing", "message": error})
        return estimate
    if token_needs_refresh(token):
        refreshed, error = refresh_google_token(token, client, token_path)
        if error or refreshed is None:
            estimate.update({"status": "refresh_failed", "message": error})
            return estimate
        token = refreshed
    access_token = str(token.get("access_token") or "")
    if not access_token:
        estimate.update({"status": "token_invalid", "message": "OAuth token has no access_token."})
        return estimate
    label_totals, error = gmail_label_totals(access_token)
    if error:
        if "HTTP 401" in error:
            refreshed, refresh_error = refresh_google_token(token, client, token_path)
            if refreshed is not None:
                access_token = str(refreshed.get("access_token") or "")
                label_totals, error = gmail_label_totals(access_token)
            elif refresh_error:
                error = refresh_error
        if error:
            estimate.update({"status": "api_error", "message": error})
            return estimate
    visible_mailbox_estimate = int(label_totals.get("INBOX", 0)) + int(label_totals.get("SENT", 0))
    excluded_label_counts = {label: int(label_totals.get(label, 0)) for label in excluded_labels if label in label_totals}
    estimate.update({
        "status": "ok",
        "messages_total_estimate": visible_mailbox_estimate,
        "messages_total_estimate_basis": "Gmail INBOX + SENT label totals",
        "messages_matching_sync_query_estimate": None,
        "messages_matching_sync_query_count_complete": False,
        "messages_matching_sync_query_count_pages": 0,
        "messages_excluded_by_sync_query_estimate": None,
        "label_message_estimates": label_totals,
        "excluded_label_message_estimates": excluded_label_counts,
        "message": "Gmail API fetched label totals only; no message IDs, bodies, subjects, snippets, or raw MIME were fetched.",
    })
    if max_pages <= 0:
        return estimate
    query_count, error = gmail_message_id_count(access_token, sync_query, max_pages=max_pages)
    if error:
        estimate.update({
            "status": "partial",
            "messages_matching_sync_query_estimate": int(query_count.get("count") or 0),
            "messages_matching_sync_query_count_pages": int(query_count.get("pages") or 0),
            "message": f"Gmail label totals fetched, but sync-query ID counting did not finish: {error}",
        })
        return estimate
    estimate.update({
        "messages_matching_sync_query_estimate": int(query_count.get("count") or 0),
        "messages_matching_sync_query_count_complete": bool(query_count.get("complete")),
        "messages_matching_sync_query_count_pages": int(query_count.get("pages") or 0),
        "messages_excluded_by_sync_query_estimate": max(0, visible_mailbox_estimate - int(query_count.get("count") or 0)) if query_count.get("complete") else None,
        "message": "Gmail API fetched label totals and listed message IDs for the sync query; no message bodies, subjects, snippets, or raw MIME were fetched.",
    })
    return estimate


def estimate_gmail_accounts_via_api(input_cfg: dict[str, Any], emails: list[str]) -> list[dict[str, Any]]:
    if input_cfg.get("skip_gmail_estimate"):
        return [{
            "scope": "gmail_api",
            "status": "skipped",
            "message": "Gmail API estimate skipped by configuration.",
            "account_email": email,
            "sync_query": gmail_sync_query(input_cfg),
            "excluded_labels": gmail_excluded_labels(input_cfg),
        } for email in emails]
    home = msgvault_home_for_db(str(input_cfg.get("msgvault_db") or ""))
    labels = gmail_excluded_labels(input_cfg)
    query = gmail_sync_query(input_cfg)
    max_pages = int(input_cfg.get("gmail_estimate_max_pages") or DEFAULT_GMAIL_ESTIMATE_MAX_PAGES)
    return [estimate_gmail_account_via_api(home, email, query, labels, max_pages=max_pages) for email in emails]


def summarize_gmail_estimates(estimates: list[dict[str, Any]]) -> str:
    ok = [estimate for estimate in estimates if estimate.get("status") in {"ok", "partial"}]
    if not ok:
        statuses = ", ".join(str(estimate.get("status") or "unknown") for estimate in estimates) or "unavailable"
        return f"Gmail API estimate unavailable ({statuses}); large first syncs can take minutes."
    total = sum(int(estimate.get("messages_total_estimate") or 0) for estimate in ok)
    matching_counts = [estimate.get("messages_matching_sync_query_estimate") for estimate in ok]
    has_matching_counts = any(value is not None for value in matching_counts)
    filtered = sum(int(value or 0) for value in matching_counts if value is not None)
    complete = all(bool(estimate.get("messages_matching_sync_query_count_complete")) for estimate in ok)
    excluded_values = [estimate.get("messages_excluded_by_sync_query_estimate") for estimate in ok]
    excluded = sum(int(value or 0) for value in excluded_values if value is not None)
    if not has_matching_counts:
        return f"Gmail API rough estimate: {total:,} messages from label totals. Large initial syncs can take minutes."
    qualifier = "" if complete else "at least "
    excluded_text = f"{excluded:,} excluded" if complete else "exclusion count still being bounded"
    return (
        f"Gmail API estimate: {qualifier}{filtered:,}/{total:,} messages match the sync query "
        f"({excluded_text}). Large initial syncs can take minutes."
    )


def ordered_records(records: list[dict[str, Any]], account_order: list[str] | None = None) -> list[dict[str, Any]]:
    order = {email: index for index, email in enumerate(account_order or []) if email}
    return sorted(
        records,
        key=lambda record: (
            order.get(str(record.get("account_email") or ""), len(order)),
            str(record.get("account_email") or record.get("slug") or record.get("people_csv") or record.get("queue_csv") or ""),
        ),
    )


def account_channels(path: str) -> dict[str, Any]:
    if not path:
        return {}
    data = read_json(Path(path), {}) or {}
    channels = data.get("accounts") or data.get("channels") or {}
    return channels if isinstance(channels, dict) else {}


def account_record_is_linked(record: dict[str, Any]) -> bool:
    """Return true for both v2 boolean records and handoff/status records.

    Registry v2 writes explicit ``linked`` / ``skipped`` booleans, while setup
    summaries and older tests may use ``status: linked``. Legacy v1 registries
    have neither field and are considered linked when they carry source values.
    """
    if not isinstance(record, dict) or record.get("skipped") is True:
        return False
    linked = record.get("linked")
    if isinstance(linked, bool):
        return linked
    status = record.get("status")
    if isinstance(status, str):
        return status == "linked"
    cfg = record.get("config") if isinstance(record.get("config"), dict) else {}
    return bool(record.get("usernames") or record.get("artifacts") or any(cfg.values()))


def gmail_record_has_import_identity(record: dict[str, Any]) -> bool:
    if not isinstance(record, dict):
        return False
    cfg = record.get("config") if isinstance(record.get("config"), dict) else {}
    return bool(cfg.get("selected_accounts") or cfg.get("account_emails") or record.get("usernames") or record.get("artifacts"))


def extract_accounts_path_from_setup(path: str) -> str:
    if not path:
        return ""
    data = read_json(Path(path), {}) or {}
    for key in ["accounts", "accounts_path"]:
        value = data.get(key)
        if isinstance(value, str):
            return value
    handoff = data.get("handoff") if isinstance(data.get("handoff"), dict) else {}
    for key in ["accounts", "accounts_path"]:
        value = handoff.get(key)
        if isinstance(value, str):
            return value
    commands = handoff.get("commands") if isinstance(handoff.get("commands"), dict) else {}
    cmd = str(commands.get("discover_contacts_run") or "")
    if "--from-accounts" in cmd:
        parts = cmd.split()
        try:
            return parts[parts.index("--from-accounts") + 1]
        except (ValueError, IndexError):
            return ""
    return ""


def apply_account_sources(args: argparse.Namespace) -> argparse.Namespace:
    accounts_path = str(getattr(args, "from_accounts", "") or "").strip()
    if not accounts_path:
        accounts_path = extract_accounts_path_from_setup(str(getattr(args, "from_setup", "") or "").strip())
        if accounts_path:
            setattr(args, "from_accounts", accounts_path)
    channels = account_channels(accounts_path)
    gmail = channels.get("gmail") if isinstance(channels.get("gmail"), dict) else {}
    if gmail and (not account_record_is_linked(gmail) or not gmail_record_has_import_identity(gmail)):
        gmail = {}
    gmail_cfg = gmail.get("config") if isinstance(gmail.get("config"), dict) else {}
    linkedin = channels.get("linkedin_csv") if isinstance(channels.get("linkedin_csv"), dict) else {}
    if linkedin and not account_record_is_linked(linkedin):
        linkedin = {}
    linkedin_cfg = linkedin.get("config") if isinstance(linkedin.get("config"), dict) else {}
    twitter = channels.get("twitter") if isinstance(channels.get("twitter"), dict) else {}
    if twitter and not account_record_is_linked(twitter):
        twitter = {}
    twitter_cfg = twitter.get("config") if isinstance(twitter.get("config"), dict) else {}
    messages = channels.get("messages") if isinstance(channels.get("messages"), dict) else {}
    if messages and not account_record_is_linked(messages):
        messages = {}
    messages_cfg = messages.get("config") if isinstance(messages.get("config"), dict) else {}

    if not getattr(args, "msgvault_db", "") and gmail_cfg.get("msgvault_db"):
        args.msgvault_db = str(gmail_cfg.get("msgvault_db") or "")
    emails = unique_strings(getattr(args, "gmail_account_emails", []))
    if getattr(args, "gmail_account_email", ""):
        emails = unique_strings([args.gmail_account_email, *emails])
    if not emails:
        emails = unique_strings(gmail_cfg.get("selected_accounts") or gmail_cfg.get("account_emails") or gmail.get("usernames"))
    args.gmail_account_emails = emails
    args.gmail_account_email = args.gmail_account_email or (emails[0] if len(emails) == 1 else "")
    if not getattr(args, "linkedin_csv", ""):
        args.linkedin_csv = str(linkedin_cfg.get("csv_path") or "")
        if not args.linkedin_csv and linkedin.get("artifacts"):
            args.linkedin_csv = str((linkedin.get("artifacts") or [""])[0])
    if not getattr(args, "linkedin_source_user", ""):
        args.linkedin_source_user = str(linkedin_cfg.get("source_label") or "")
        if not args.linkedin_source_user and linkedin.get("usernames"):
            args.linkedin_source_user = str((linkedin.get("usernames") or [""])[0])
    if not getattr(args, "twitter_handle", ""):
        args.twitter_handle = str(twitter_cfg.get("handle") or "")
        if not args.twitter_handle and twitter.get("usernames"):
            args.twitter_handle = str((twitter.get("usernames") or [""])[0])
    if not getattr(args, "messages_review_csv", ""):
        args.messages_review_csv = str(messages_cfg.get("review_csv") or "")
    return args


def resolve_msgvault_db(args: argparse.Namespace) -> str:
    explicit = str(getattr(args, "msgvault_db", "") or "").strip()
    if explicit:
        return explicit
    if str(getattr(args, "gmail_account_email", "") or "").strip() or unique_strings(getattr(args, "gmail_account_emails", [])):
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


def child_error(payload: dict[str, Any], stderr: str) -> Any:
    """Prefer structured child failures; stderr is often progress logging."""
    if payload:
        for key in ("error", "message", "reason"):
            value = payload.get(key)
            if value:
                return value
        return payload
    return stderr


def py_cmd(script: str, *args: str) -> list[str]:
    return [sys.executable, script, *args]


def load_ledger(path: Path) -> dict[str, Any]:
    ledger = read_json(path, {}) or {}
    ledger.setdefault("primitive", "discover_contacts_pipeline")
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


def run_linkedin_child(ledger: dict[str, Any], mode: str) -> dict[str, Any]:
    input_cfg = ledger.get("input", {})
    artifact_dir = discover_source_dir("linkedin_csv")
    child_ledger = artifact_dir / "linkedin.ledger.json"
    if mode == "run":
        cmd = py_cmd(
            "packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py",
            "run",
            "--csv", input_cfg["linkedin_csv"],
            "--source-user", input_cfg.get("linkedin_source_user") or "local",
            "--operator-id", input_cfg.get("operator_id") or "local",
            "--output-dir", str(DEFAULT_DISCOVER_DIR),
            "--ledger", str(child_ledger),
            "--force",
        )
        if input_cfg.get("linkedin_limit") is not None:
            cmd.extend(["--limit", str(input_cfg["linkedin_limit"])])
        if input_cfg.get("source_import_only"):
            cmd.append("--convert-only")
    else:
        cmd = py_cmd("packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py", "continue", "--ledger", str(child_ledger))
    code, payload, stderr = run_cmd(cmd)
    return {"id": "linkedin_csv", "source": "linkedin_csv", "child_ledger": str(child_ledger), "command": cmd, "code": code, "payload": payload, "stderr": stderr}


def record_linkedin_worker_result(ledger_path: Path, ledger: dict[str, Any], result: dict[str, Any]) -> bool:
    code = int(result.get("code") or 0)
    payload = result.get("payload") or {}
    stderr = result.get("stderr") or ""
    child_ledger = result.get("child_ledger") or str(artifact_dir_from_ledger(ledger) / "linkedin.ledger.json")
    ledger.setdefault("artifacts", {})["linkedin_ledger"] = str(child_ledger)
    if code == 20 or payload.get("status") == "blocked_approval":
        ledger["blocked"] = {"step_id": "linkedin", "child_ledger": str(child_ledger), "child": payload}
        mark_step(ledger, "linkedin", "blocked", payload=payload)
        save_ledger(ledger_path, ledger)
        emit({"status": "blocked_approval", "step_id": "linkedin", "ledger": str(ledger_path), "child": payload})
        return False
    if code != 0:
        error = child_error(payload, stderr)
        mark_step(ledger, "linkedin", "failed", error=error)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "linkedin", "error": error})
        return False
    mark_step(ledger, "linkedin", "completed", payload=payload)
    for key, value in (payload.get("artifacts") or {}).items():
        ledger.setdefault("artifacts", {})[f"linkedin_{key}"] = value
    people_csv = (payload.get("artifacts") or {}).get("people_csv")
    if people_csv:
        checkpoint = commit_people_csv_to_directory(
            ledger.get("input", {}),
            ledger.setdefault("artifacts", {}),
            str(people_csv),
            source="linkedin_csv",
            source_account=str(ledger.get("input", {}).get("linkedin_source_user") or "local"),
        )
        ledger.setdefault("artifacts", {})["linkedin_directory_checkpoint"] = checkpoint
    emit_progress("LinkedIn import completed.")
    return True


def run_linkedin(ledger_path: Path, ledger: dict[str, Any], mode: str) -> bool:
    input_cfg = ledger.get("input", {})
    if not input_cfg.get("linkedin_csv"):
        mark_step(ledger, "linkedin", "skipped", reason="no --linkedin-csv")
        return True
    begin_step(ledger_path, ledger, "linkedin", "Importing LinkedIn CSV and enriching profiles.")
    return record_linkedin_worker_result(ledger_path, ledger, run_linkedin_child(ledger, mode))


def run_gmail_msgvault_account(ledger: dict[str, Any], email: str, index: int = 0) -> dict[str, Any]:
    input_cfg = ledger.get("input", {})
    db = input_cfg.get("msgvault_db") or str(DEFAULT_MSGVAULT_DB)
    artifact_dir = DEFAULT_DISCOVER_DIR / "gmail" / source_slug(email or "all")
    excluded_labels = gmail_excluded_labels(input_cfg)
    sync_query = gmail_sync_query(input_cfg)
    sync_after = gmail_sync_after(input_cfg)
    sync_after_source = "explicit" if sync_after else ""
    if not sync_after and email:
        inferred = infer_msgvault_sync_after(str(db), email)
        sync_after = inferred.get("sync_after", "")
        sync_after_source = inferred.get("source", "")
    existing_estimates = ledger.get("artifacts", {}).get("gmail_api_estimates") or []
    estimate = next((item for item in existing_estimates if isinstance(item, dict) and item.get("account_email") == email), None)
    if estimate is None:
        estimates = estimate_gmail_accounts_via_api(input_cfg, [email])
        estimate = estimates[0] if estimates else {}
    sync_command: list[str] = []
    sync_skipped_reason = ""
    if email and skip_msgvault_sync(input_cfg):
        sync_skipped_reason = "msgvault sync skipped by configuration; using existing msgvault DB"
    elif email and shutil.which("msgvault"):
        sync_cmd = ["msgvault"]
        db_home = Path(db).expanduser().parent
        default_home = Path(DEFAULT_MSGVAULT_DB).expanduser().parent
        if db_home != default_home:
            sync_cmd.extend(["--home", str(db_home)])
        sync_cmd.extend(["sync-full", email])
        if sync_after:
            sync_cmd.extend(["--after", sync_after])
        if sync_query:
            sync_cmd.extend(["--query", sync_query])
        sync_command = sync_cmd
        after_text = f" After: {sync_after}." if sync_after else ""
        emit_progress(f"Starting msgvault sync for {email}.{after_text} Query: {sync_query or '(all mail)'}. {summarize_gmail_estimates([estimate])}")
        sync_code, sync_payload, sync_stderr = run_cmd(sync_cmd)
        if sync_code != 0:
            return {
                "id": f"gmail:{email}",
                "source": "gmail",
                "account_email": email,
                "artifact_dir": str(artifact_dir),
                "sync_command": sync_cmd,
                "excluded_labels": excluded_labels,
                "sync_query": sync_query,
                "sync_after": sync_after,
                "sync_after_source": sync_after_source,
                "gmail_estimate": estimate,
                "code": sync_code,
                "payload": sync_payload,
                "stderr": sync_stderr,
                "phase": "msgvault_sync",
            }
    elif email:
        sync_skipped_reason = "msgvault command not found; using existing msgvault DB if present"
    cmd = py_cmd(
        "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py",
        "msgvault",
        "--db", db,
        "--operator-id", input_cfg.get("operator_id") or "local",
        "--output-dir", str(DEFAULT_BASE_DIR),
    )
    if email:
        cmd.extend(["--account-email", email])
    if input_cfg.get("gmail_limit") is not None:
        cmd.extend(["--limit", str(input_cfg["gmail_limit"])])
    if input_cfg.get("include_automated_gmail"):
        cmd.append("--include-automated")
    if input_cfg.get("include_category_mail"):
        cmd.append("--include-category-mail")
    for label in excluded_labels:
        cmd.extend(["--exclude-label", label])
    code, payload, stderr = run_cmd(cmd)
    return {"id": f"gmail:{email or 'all'}", "source": "gmail", "account_email": email, "artifact_dir": str(artifact_dir), "sync_command": sync_command, "sync_skipped_reason": sync_skipped_reason, "excluded_labels": excluded_labels, "sync_query": sync_query, "sync_after": sync_after, "sync_after_source": sync_after_source, "gmail_estimate": estimate, "command": cmd, "code": code, "payload": payload, "stderr": stderr, "phase": "gmail_network_import"}


def record_gmail_worker_result(ledger: dict[str, Any], result: dict[str, Any]) -> bool:
    email = result.get("account_email") or "all"
    step_id = f"gmail_msgvault:{source_slug(email)}"
    payload = result.get("payload") or {}
    code = int(result.get("code") or 0)
    if code != 0:
        mark_step(ledger, step_id, "failed", error=result.get("stderr") or payload.get("error") or payload, account_email=email, phase=result.get("phase"))
        ledger["status"] = "failed"
        return False
    mark_step(ledger, step_id, "completed", payload=payload, account_email=email, sync_command=result.get("sync_command"), sync_skipped_reason=result.get("sync_skipped_reason"), excluded_labels=result.get("excluded_labels"), sync_query=result.get("sync_query"), sync_after=result.get("sync_after"), sync_after_source=result.get("sync_after_source"), gmail_estimate=result.get("gmail_estimate"))
    ledger.setdefault("source_imports", {})[step_id] = {"status": "completed", "source": "gmail", "account_email": email, "artifact_dir": result.get("artifact_dir"), "sync_command": result.get("sync_command"), "sync_skipped_reason": result.get("sync_skipped_reason"), "excluded_labels": result.get("excluded_labels"), "sync_query": result.get("sync_query"), "sync_after": result.get("sync_after"), "sync_after_source": result.get("sync_after_source"), "gmail_estimate": result.get("gmail_estimate")}
    slug = source_slug(email)
    people_csv = ""
    for key, value in (payload.get("artifacts") or {}).items():
        ledger.setdefault("artifacts", {})[f"gmail_{slug}_{key}"] = value
        if key == "people_csv":
            people_csv = str(value or "")
            ledger.setdefault("artifacts", {})["gmail_people_csv"] = value
            ledger.setdefault("artifacts", {}).setdefault("gmail_people_csvs", []).append(value)
            ledger.setdefault("artifacts", {}).setdefault("gmail_people_records", []).append({"account_email": email, "people_csv": people_csv, "slug": slug})
    for key, value in (payload.get("artifacts") or {}).items():
        if key == "linkedin_resolution_queue_csv":
            queue_record = {"account_email": email, "queue_csv": value, "people_csv": people_csv, "slug": slug}
            ledger.setdefault("artifacts", {}).setdefault("gmail_linkedin_resolution_queue_csvs", []).append(queue_record)
            if "gmail_linkedin_resolution_queue_csv" not in ledger.setdefault("artifacts", {}):
                ledger["artifacts"]["gmail_linkedin_resolution_queue_csv"] = value
    counts = payload.get("counts") or {}
    if counts:
        emit_progress(f"Gmail metadata import completed for {email}: {counts.get('contacts_written', 0)} contacts from {counts.get('contacts_seen', 0)} seen.")
    else:
        emit_progress(f"Gmail metadata import completed for {email}.")
    return True


def run_gmail_msgvault(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    input_cfg = ledger.get("input", {})
    emails = unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email"))
    if not emails and input_cfg.get("msgvault_db"):
        emails = [""]
    if not emails:
        mark_step(ledger, "gmail_msgvault", "skipped", reason="no Gmail account emails/msgvault DB")
        return True
    estimates = estimate_gmail_accounts_via_api(input_cfg, emails)
    ledger.setdefault("artifacts", {})["gmail_api_estimates"] = estimates
    begin_step(ledger_path, ledger, "gmail_msgvault", f"Importing Gmail metadata for {len(emails)} msgvault account(s). {summarize_gmail_estimates(estimates)}")
    ok = True
    max_workers = min(len(emails), int(os.environ.get("POWERPACKS_IMPORT_NETWORK_GMAIL_MAX_WORKERS", "1"))) or 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(run_gmail_msgvault_account, ledger, email, index) for index, email in enumerate(emails)]
        results = [future.result() for future in futures]
    for result in results:
        if not record_gmail_worker_result(ledger, result):
            ok = False
    if ok:
        mark_step(ledger, "gmail_msgvault", "completed", accounts=emails, parallelizable=True)
    return ok


def source_worker_group(input_cfg: dict[str, Any]) -> dict[str, Any]:
    jobs: list[dict[str, Any]] = []
    gmail_emails = unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email"))
    if gmail_emails or input_cfg.get("msgvault_db"):
        emails = gmail_emails or [""]
        for email in emails:
            jobs.append({
                "id": f"gmail:{email or 'all'}",
                "source": "gmail",
                "account_email": email,
                "step_id": f"gmail_msgvault:{source_slug(email or 'all')}",
                "artifact_root": str(DEFAULT_DISCOVER_DIR / "gmail" / source_slug(email or "all")),
                "sync_query": gmail_sync_query(input_cfg),
                "sync_after": gmail_sync_after(input_cfg),
                "excluded_labels": gmail_excluded_labels(input_cfg),
                "parallelizable": True,
                "reason": "local msgvault metadata read into a stable discover folder",
            })
    if input_cfg.get("linkedin_csv"):
        jobs.append({
            "id": "linkedin_csv",
            "source": "linkedin_csv",
            "step_id": "linkedin",
            "ledger": str(DEFAULT_DISCOVER_DIR / "linkedin" / "linkedin.ledger.json"),
            "artifact_root": str(DEFAULT_DISCOVER_DIR / "linkedin"),
            "parallelizable": True,
            "reason": "CSV conversion/enrichment writes into a stable discover folder",
            "requires_approval": ["rapidapi_linkedin_profile_enrichment"],
        })
    if input_cfg.get("twitter_handle"):
        jobs.append({
            "id": "twitter",
            "source": "twitter",
            "handle": input_cfg.get("twitter_handle"),
            "parallelizable": True,
            "reason": "twitter import has no dependency on Gmail/LinkedIn but still requires spend confirmation",
            "requires_approval": ["rapidapi_twitter", "openai_moe", "rapidapi_linkedin_validation"],
            "status": "existing_artifacts_or_explicit_import_required",
        })
    messages_review_csv = input_cfg.get("messages_review_csv") or ""
    if messages_review_csv and not Path(str(messages_review_csv)).exists():
        messages_review_csv = ""
    if input_cfg.get("messages_contacts_csv") or messages_review_csv:
        jobs.append({
            "id": "messages",
            "source": "messages",
            "review_csv": messages_review_csv,
            "contacts_csv": input_cfg.get("messages_contacts_csv") or ".powerpacks/messages/contacts.csv",
            "parallelizable": True,
            "reason": "reviewed Messages LinkedIn rows are materialized locally, then hydrated through enrich_people before fan-in" if messages_review_csv else MESSAGES_REVIEW_GATE_REASON,
            "requires_approval": ["rapidapi_linkedin_profile_enrichment"] if messages_review_csv else ["messages_review_flow"],
            "status": "approved_review_artifact" if messages_review_csv else "review_required",
        })
    return {"parallel": True, "fan_in": "merge_network_sources_after_nonblocked_workers", "jobs": jobs}


def run_source_import_workers(ledger_path: Path, ledger: dict[str, Any], *, resume: bool = False) -> bool:
    input_cfg = ledger.get("input", {})
    group = source_worker_group(input_cfg)
    ledger["worker_groups"] = {"import": group}
    selected = set(unique_strings(input_cfg.get("only_sources")))
    runnable_sources = {"gmail", "linkedin_csv"}
    if selected:
        runnable_sources &= selected
    mark_step(ledger, "source_imports", "running", worker_group=group)
    save_ledger(ledger_path, ledger)
    futures: dict[concurrent.futures.Future[dict[str, Any]], str] = {}
    gmail_emails = unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email"))
    if not gmail_emails and input_cfg.get("msgvault_db"):
        gmail_emails = [""]
    gmail_max_workers = min(len(gmail_emails), int(os.environ.get("POWERPACKS_IMPORT_NETWORK_GMAIL_MAX_WORKERS", "1"))) if gmail_emails else 0
    total_workers = gmail_max_workers + (1 if input_cfg.get("linkedin_csv") else 0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, total_workers or 1))) as executor:
        if "linkedin_csv" in runnable_sources and input_cfg.get("linkedin_csv") and ledger.get("steps", {}).get("linkedin", {}).get("status") not in {"completed", "skipped"}:
            futures[executor.submit(run_linkedin_child, ledger, "continue" if resume else "run")] = "linkedin_csv"
        if "gmail" in runnable_sources and ledger.get("steps", {}).get("gmail_msgvault", {}).get("status") not in {"completed", "skipped"} and gmail_emails:
            estimates = estimate_gmail_accounts_via_api(input_cfg, gmail_emails)
            ledger.setdefault("artifacts", {})["gmail_api_estimates"] = estimates
            begin_step(ledger_path, ledger, "gmail_msgvault", f"Importing Gmail metadata for {len(gmail_emails)} msgvault account(s). {summarize_gmail_estimates(estimates)}")
            for index, email in enumerate(gmail_emails):
                futures[executor.submit(run_gmail_msgvault_account, ledger, email, index)] = "gmail"
        for future in concurrent.futures.as_completed(futures):
            source = futures[future]
            result = future.result()
            if source == "linkedin_csv":
                if not record_linkedin_worker_result(ledger_path, ledger, result):
                    mark_step(ledger, "source_imports", "blocked" if ledger.get("blocked") else "failed", worker_group=group)
                    save_ledger(ledger_path, ledger)
                    return False
            elif source == "gmail":
                if not record_gmail_worker_result(ledger, result):
                    emit({"status": "failed", "step_id": f"gmail_msgvault:{source_slug(result.get('account_email') or 'all')}", "error": result.get("stderr") or result.get("payload")})
                    mark_step(ledger, "source_imports", "failed", worker_group=group)
                    save_ledger(ledger_path, ledger)
                    return False
                save_ledger(ledger_path, ledger)
    if gmail_emails and "gmail" in runnable_sources:
        mark_step(ledger, "gmail_msgvault", "completed", accounts=gmail_emails, parallelizable=True)
    # Mark skipped sources after parallel fan-out finishes.
    if "linkedin_csv" in runnable_sources and not input_cfg.get("linkedin_csv"):
        mark_step(ledger, "linkedin", "skipped", reason="no --linkedin-csv")
    if "gmail" in runnable_sources and not (unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")) or input_cfg.get("msgvault_db")):
        mark_step(ledger, "gmail_msgvault", "skipped", reason="no Gmail account emails/msgvault DB")
    for source in ["twitter", "messages"]:
        if not selected or source in selected:
            if source == "twitter" and not input_cfg.get("twitter_handle"):
                continue
            if source == "messages" and not (input_cfg.get("messages_contacts_csv") or input_cfg.get("messages_review_csv") or input_cfg.get("include_existing_artifacts")):
                continue
            if source == "messages" and input_cfg.get("messages_review_csv"):
                ledger.setdefault("artifacts", {})["messages_review_csv"] = input_cfg.get("messages_review_csv")
                mark_step(ledger, source, "completed", reason="approved rows from reviewed messages CSV will be included in fan-in merge", review_csv=input_cfg.get("messages_review_csv"))
                continue
            if source == "messages" and input_cfg.get("messages_contacts_csv"):
                if input_cfg.get("allow_unreviewed_messages"):
                    ledger.setdefault("artifacts", {})["messages_contacts_csv"] = input_cfg.get("messages_contacts_csv")
                    mark_step(ledger, source, "completed", reason="explicit unreviewed messages override; contacts CSV will be included in fan-in merge", contacts_csv=input_cfg.get("messages_contacts_csv"))
                else:
                    mark_step(ledger, source, "skipped", reason=MESSAGES_REVIEW_GATE_REASON, contacts_csv=input_cfg.get("messages_contacts_csv"))
            else:
                mark_step(ledger, source, "skipped", reason=MESSAGES_REVIEW_GATE_REASON)
    mark_step(ledger, "source_imports", "completed", worker_group=group)
    save_ledger(ledger_path, ledger)
    return True


def gmail_queue_records(artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    queue_records = artifacts.get("gmail_linkedin_resolution_queue_csvs") or []
    if not queue_records and artifacts.get("gmail_linkedin_resolution_queue_csv"):
        queue_records = [{"account_email": "", "queue_csv": artifacts.get("gmail_linkedin_resolution_queue_csv"), "people_csv": artifacts.get("gmail_people_csv"), "slug": "all"}]
    return [record for record in queue_records if isinstance(record, dict) and record.get("queue_csv")]


def run_gmail_directory(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    input_cfg = ledger.get("input", {})
    artifacts = ledger.setdefault("artifacts", {})
    queue_records = ordered_records(gmail_queue_records(artifacts), unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")))
    if not queue_records:
        checkpoint = build_directory_checkpoint(input_cfg, artifacts)
        artifacts["directory_csv"] = checkpoint["directory_csv"]
        mark_step(ledger, "gmail_directory", "skipped", reason="no Gmail LinkedIn queue", checkpoint=checkpoint)
        return True
    begin_step(ledger_path, ledger, "gmail_directory", f"Applying directory LinkedIn mappings to {len(queue_records)} Gmail queue(s).")
    observation_checkpoint = commit_gmail_observations_to_directory(input_cfg, artifacts)
    checkpoint = build_directory_checkpoint(input_cfg, artifacts)
    directory_csv = Path(checkpoint["directory_csv"])
    artifacts["directory_csv"] = str(directory_csv)
    artifacts["gmail_directory_by_slug"] = {}
    by_slug = artifacts["gmail_directory_by_slug"]
    artifacts["gmail_directory_resolution_records"] = []
    artifacts["gmail_unresolved_linkedin_resolution_queue_csvs"] = []
    artifacts["gmail_cached_negative_linkedin_resolution_queue_csvs"] = []
    results = []
    total_resolved = 0
    total_unresolved = 0
    total_cached_negative = 0
    for index, record in enumerate(queue_records):
        slug = source_slug(record.get("account_email") or record.get("slug") or f"queue-{index}")
        if True:
            out_dir = artifact_dir_from_ledger(ledger) / f"gmail-directory-{slug}"
            result = apply_directory_to_gmail_queue(record, directory_csv, out_dir)
            result["slug"] = slug
            by_slug[slug] = result
        total_resolved += int(result.get("resolved") or 0)
        total_unresolved += int(result.get("unresolved") or 0)
        total_cached_negative += int(result.get("cached_negative") or 0)
        if int(result.get("resolved") or 0) > 0:
            artifacts["gmail_directory_resolution_records"].append({
                "account_email": record.get("account_email", ""),
                "resolutions_csv": result.get("directory_resolutions_csv"),
                "people_csv": record.get("people_csv"),
                "slug": slug,
                "source": "directory",
                "resolved": result.get("resolved"),
            })
        if int(result.get("unresolved") or 0) > 0:
            artifacts["gmail_unresolved_linkedin_resolution_queue_csvs"].append({
                "account_email": record.get("account_email", ""),
                "queue_csv": result.get("unresolved_queue_csv"),
                "people_csv": record.get("people_csv"),
                "slug": slug,
                "source": "directory_unresolved",
                "unresolved": result.get("unresolved"),
            })
        if int(result.get("cached_negative") or 0) > 0:
            artifacts["gmail_cached_negative_linkedin_resolution_queue_csvs"].append({
                "account_email": record.get("account_email", ""),
                "queue_csv": result.get("cached_negative_queue_csv"),
                "people_csv": record.get("people_csv"),
                "slug": slug,
                "source": "directory_cached_negative",
                "cached_negative": result.get("cached_negative"),
            })
        results.append(result)
    mark_step(ledger, "gmail_directory", "completed", checkpoint=checkpoint, observation_checkpoint=observation_checkpoint, resolved=total_resolved, unresolved=total_unresolved, cached_negative=total_cached_negative, payload={"results": results})
    if total_cached_negative:
        emit_progress(f"Gmail directory mappings applied: {total_resolved} resolved, {total_cached_negative} already attempted, {total_unresolved} unresolved.")
    else:
        emit_progress(f"Gmail directory mappings applied: {total_resolved} resolved, {total_unresolved} unresolved.")
    return True


def run_gmail_linkedin_resolution(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    input_cfg = ledger.get("input", {})
    provider = "parallel" if input_cfg.get("resolve_gmail_linkedin") else "off"
    if input_cfg.get("gmail_linkedin_provider") and input_cfg.get("gmail_linkedin_provider") != "off":
        provider = input_cfg.get("gmail_linkedin_provider")
    artifacts = ledger.setdefault("artifacts", {})
    if "gmail_unresolved_linkedin_resolution_queue_csvs" in artifacts:
        queue_records = artifacts.get("gmail_unresolved_linkedin_resolution_queue_csvs") or []
    else:
        queue_records = gmail_queue_records(artifacts)
    if provider == "off" or not queue_records:
        mark_step(ledger, "gmail_linkedin_resolution", "skipped", reason="provider off or no queue")
        return True
    queue_records = ordered_records([record for record in queue_records if isinstance(record, dict) and record.get("queue_csv") and csv_row_count(Path(str(record.get("queue_csv")))) > 0], unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")))
    if not queue_records:
        mark_step(ledger, "gmail_linkedin_resolution", "skipped", reason="all Gmail queue rows resolved by directory")
        return True
    # Combine all unresolved queues into one file and submit as a single batch
    combined_csv = artifact_dir_from_ledger(ledger) / "gmail-combined-unresolved-queue.csv"
    combined_fields: list[str] = []
    combined_rows: list[dict[str, str]] = []
    for record in queue_records:
        queue = record.get("queue_csv")
        if not queue:
            continue
        fields, rows = read_csv_rows(Path(str(queue)))
        if not combined_fields and fields:
            combined_fields = fields
        combined_rows.extend(rows)
    if not combined_rows:
        mark_step(ledger, "gmail_linkedin_resolution", "skipped", reason="combined queue is empty")
        return True
    write_csv_rows(combined_csv, combined_fields, combined_rows)
    artifacts["gmail_linkedin_combined_queue_csv"] = str(combined_csv)
    total_contacts = len(combined_rows)
    begin_step(ledger_path, ledger, "gmail_linkedin_resolution", f"Resolving {total_contacts} Gmail contacts to LinkedIn in one batch.")
    child_ledger = artifact_dir_from_ledger(ledger) / "gmail-linkedin-resolution.combined.ledger.json"
    out_dir = artifact_dir_from_ledger(ledger) / "gmail-linkedin-resolution-combined"
    cmd = py_cmd(
        "packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py",
        "run",
        "--provider", provider,
        "--input", str(combined_csv),
        "--output-dir", str(out_dir),
        "--ledger", str(child_ledger),
    )
    if input_cfg.get("gmail_linkedin_limit") is not None:
        cmd.extend(["--limit", str(input_cfg["gmail_linkedin_limit"])])
    if input_cfg.get("approve_parallel_spend"):
        cmd.append("--approve-spend")
    code, payload, stderr = run_cmd(cmd)
    artifacts["gmail_linkedin_resolution_ledger"] = str(child_ledger)
    artifacts["gmail_linkedin_resolution_ledgers"] = [str(child_ledger)]
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
    if payload.get("output"):
        combined_output = payload.get("output")
        artifacts["gmail_linkedin_raw_resolutions_csv"] = combined_output
        artifacts["gmail_linkedin_combined_resolutions_csv"] = combined_output
        provider_records = materialize_gmail_provider_resolution_records(combined_output, queue_records, artifact_dir_from_ledger(ledger))
        if provider_records:
            artifacts["gmail_linkedin_resolutions_csvs"] = provider_records
            artifacts["gmail_linkedin_resolutions_by_slug"] = {record.get("slug", ""): record for record in provider_records if record.get("slug")}
            # Keep the singular key for status/debug callers, but point it at a
            # normalized per-account file rather than the raw Parallel schema.
            artifacts["gmail_linkedin_resolutions_csv"] = provider_records[0].get("resolutions_csv")
        else:
            artifacts["gmail_linkedin_resolutions_csv"] = combined_output
            artifacts["gmail_linkedin_resolutions_csvs"] = [{"resolutions_csv": combined_output, "slug": "combined", "source": "parallel_combined"}]
    if payload.get("prompts_jsonl"):
        artifacts["gmail_linkedin_harness_prompts_jsonl"] = payload.get("prompts_jsonl")
        artifacts["gmail_linkedin_harness_prompts_jsonls"] = [payload.get("prompts_jsonl")]
    if payload.get("instructions"):
        artifacts["gmail_linkedin_harness_instructions"] = payload.get("instructions")
    mark_step(ledger, "gmail_linkedin_resolution", "completed", payload=payload)
    emit_progress(f"Gmail LinkedIn resolution completed: {payload.get('found', 0)} found, {payload.get('not_found', 0)} not found.")
    return True


def run_gmail_apply_and_enrich(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    input_cfg = ledger.get("input", {})
    artifacts = ledger.setdefault("artifacts", {})
    raw_resolution_records: list[dict[str, Any]] = []
    if input_cfg.get("gmail_resolutions_csv"):
        people_records = [
            record for record in ordered_records(
                artifacts.get("gmail_people_records") or [],
                unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")),
            )
            if isinstance(record, dict) and record.get("people_csv")
        ]
        raw_resolution_records.extend([
            {
                "account_email": record.get("account_email", ""),
                "resolutions_csv": input_cfg.get("gmail_resolutions_csv"),
                "people_csv": record.get("people_csv"),
                "slug": record.get("slug") or record.get("account_email") or f"account-{index}",
                "source": "explicit",
            }
            for index, record in enumerate(people_records)
        ])
    raw_resolution_records.extend(record for record in artifacts.get("gmail_directory_resolution_records") or [] if isinstance(record, dict))
    raw_resolution_records.extend(record for record in artifacts.get("gmail_linkedin_resolutions_csvs") or [] if isinstance(record, dict))
    if raw_resolution_records:
        commit_gmail_resolutions_to_directory(input_cfg, artifacts, raw_resolution_records)
    resolution_records = combine_gmail_resolution_records(raw_resolution_records, artifact_dir_from_ledger(ledger))
    if not resolution_records:
        mark_step(ledger, "gmail_apply_enrich", "skipped", reason="no gmail resolutions")
        return True
    resolution_records = ordered_records(resolution_records, unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")))
    checkpoint = build_directory_checkpoint(input_cfg, artifacts)
    artifacts["directory_csv"] = checkpoint["directory_csv"]
    artifacts["directory_checkpoint"] = checkpoint
    artifacts["gmail_apply_enrich_by_slug"] = {}
    by_slug = artifacts["gmail_apply_enrich_by_slug"]
    artifacts["gmail_resolved_people_csvs"] = []
    artifacts["gmail_enrich_people_ledgers"] = []
    artifacts["gmail_final_people_csvs"] = []
    artifacts["gmail_combined_resolutions_csvs"] = resolution_records
    begin_step(ledger_path, ledger, "gmail_apply_enrich", f"Applying Gmail LinkedIn matches for {len(resolution_records)} account file(s).")
    results = []
    final_people_csvs = []
    for index, record in enumerate(resolution_records):
        slug = source_slug(record.get("account_email") or record.get("slug") or f"account-{index}")
        account_dir = Path(str(record.get("people_csv") or "")).parent
        resolved_dir = account_dir / "resolved"
        apply_cmd = py_cmd(
            "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py",
            "apply-resolutions",
            "--people-csv", str(record["people_csv"]),
            "--resolutions-csv", str(record["resolutions_csv"]),
            "--output-dir", str(resolved_dir),
        )
        code, payload, stderr = run_cmd(apply_cmd)
        if code != 0:
            mark_step(ledger, "gmail_apply_enrich", "failed", error=stderr or payload)
            ledger["status"] = "failed"
            save_ledger(ledger_path, ledger)
            emit({"status": "failed", "step_id": "gmail_apply_enrich", "error": stderr or payload})
            return False
        resolved_people = payload.get("people_csv") or record["people_csv"]
        artifacts.setdefault("gmail_resolved_people_csvs", []).append(resolved_people)
        artifacts["gmail_resolved_people_csv"] = resolved_people
        result = {"account_email": record.get("account_email", ""), "slug": slug, "apply": payload, "people_csv": resolved_people}
        # enrich_resolved=False: attach resolutions only, no RapidAPI profile
        # hydration (the deep-context processing layer owns enrichment).
        if int(payload.get("resolved") or 0) > 0 and input_cfg.get("enrich_resolved", True):
            emit_progress(f"Enriching {payload.get('resolved')} resolved Gmail LinkedIn profiles for {record.get('account_email') or slug}.")
            enrich_dir = account_dir / "enrichment"
            child_ledger = account_dir / "enrich_people.ledger.json"
            enrich_cmd = py_cmd(
                "packs/ingestion/primitives/enrich_people/enrich_people.py",
                "run",
                "--input", str(resolved_people),
                "--ledger", str(child_ledger),
                "--artifact-dir", str(enrich_dir),
            )
            code, enrich_payload, stderr = run_cmd(enrich_cmd)
            artifacts.setdefault("gmail_enrich_people_ledgers", []).append(str(child_ledger))
            artifacts.setdefault("gmail_enrich_people_ledger", str(child_ledger))
            result["enrich_ledger"] = str(child_ledger)
            if code == 20 or enrich_payload.get("status") == "blocked_approval":
                ledger["blocked"] = {"step_id": "gmail_apply_enrich", "child_ledger": str(child_ledger), "child": enrich_payload, "account_email": record.get("account_email", "")}
                mark_step(ledger, "gmail_apply_enrich", "blocked", payload=enrich_payload)
                save_ledger(ledger_path, ledger)
                emit({"status": "blocked_approval", "step_id": "gmail_apply_enrich", "ledger": str(ledger_path), "child": enrich_payload})
                return False
            if code != 0:
                error = child_error(enrich_payload, stderr)
                mark_step(ledger, "gmail_apply_enrich", "failed", error=error)
                ledger["status"] = "failed"
                save_ledger(ledger_path, ledger)
                emit({"status": "failed", "step_id": "gmail_apply_enrich", "error": error})
                return False
            for key, value in (enrich_payload.get("artifacts") or {}).items():
                artifacts[f"gmail_{slug}_enriched_{key}"] = value
            if enrich_payload.get("artifacts", {}).get("people_csv"):
                resolved_people = enrich_payload["artifacts"]["people_csv"]
            result["enrich"] = enrich_payload
        final_people_csvs.append(resolved_people)
        artifacts["gmail_people_csv"] = resolved_people
        result["final_people_csv"] = resolved_people
        by_slug[slug] = result
        results.append(result)
    artifacts["gmail_account_final_people_csvs"] = final_people_csvs
    artifacts["gmail_final_people_csvs"] = final_people_csvs
    gmail_merge = materialize_gmail_merged_people_csv(final_people_csvs, DEFAULT_BASE_DIR / "gmail" / "people.gmail.csv")
    artifacts["gmail_merged_people"] = gmail_merge
    if gmail_merge.get("status") == "completed" and gmail_merge.get("people_csv"):
        artifacts["gmail_merged_people_csv"] = gmail_merge.get("people_csv")
        artifacts["gmail_final_people_csvs"] = [str(gmail_merge.get("people_csv"))]
        artifacts["gmail_people_csv"] = str(gmail_merge.get("people_csv"))
    mark_step(ledger, "gmail_apply_enrich", "completed", payload={"results": results, "gmail_merged_people": gmail_merge})
    emit_progress("Gmail LinkedIn matches applied and enrichment completed.")
    return True


def resolve_messages_review_csv(ledger: dict[str, Any]) -> str:
    input_cfg = ledger.get("input", {}) or {}
    artifacts = ledger.get("artifacts", {}) or {}
    review_csv = artifacts.get("messages_review_csv") or input_cfg.get("messages_review_csv") or ""
    return str(review_csv or "")


def enrich_people_payload_from_ledger(child_ledger: Path) -> dict[str, Any]:
    child = read_json(child_ledger, {}) or {}
    if child.get("status") != "completed":
        return {}
    return {"status": "completed", "ledger": str(child_ledger), "artifact_dir": child.get("artifact_dir") or child.get("run_dir"), "artifacts": child.get("artifacts", {})}


def run_messages_enrichment(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    artifacts = ledger.setdefault("artifacts", {})
    review_csv_text = resolve_messages_review_csv(ledger)
    if not review_csv_text:
        mark_step(ledger, "messages_enrich_people", "skipped", reason="no messages research_review.csv")
        return True
    review_csv = Path(review_csv_text)
    artifacts["messages_review_csv"] = str(review_csv)
    run_dir = artifact_dir_from_ledger(ledger) / "messages"
    input_people = run_dir / "people.input.csv"
    manifest_path = run_dir / "people_manifest.json"
    begin_step(ledger_path, ledger, "messages_enrich_people", "Preparing reviewed Messages LinkedIn rows for local profile enrichment.")
    materialized = materialize_messages_review_people(review_csv, input_people, manifest_path)
    artifacts["messages_people_input_manifest"] = str(manifest_path)
    if materialized.get("people_csv"):
        artifacts["messages_people_input_csv"] = materialized["people_csv"]
    if not materialized.get("rows_written"):
        mark_step(ledger, "messages_enrich_people", "skipped", summary=materialized, reason="no eligible reviewed Messages LinkedIn rows")
        save_ledger(ledger_path, ledger)
        emit_progress("No reviewed Messages LinkedIn rows need local enrichment.")
        return True

    child_ledger = artifact_dir_from_ledger(ledger) / "messages-enrich-people.ledger.json"
    artifacts["messages_enrich_people_ledger"] = str(child_ledger)
    enrich_cmd = py_cmd(
        "packs/ingestion/primitives/enrich_people/enrich_people.py",
        "run",
        "--input", str(input_people),
        "--ledger", str(child_ledger),
        "--artifact-dir", str(run_dir / "enrichment"),
    )
    code, enrich_payload, stderr = run_cmd(enrich_cmd)
    if code == 20 or enrich_payload.get("status") == "blocked_approval":
        ledger["blocked"] = {"step_id": "messages_enrich_people", "child_ledger": str(child_ledger), "child": enrich_payload}
        mark_step(ledger, "messages_enrich_people", "blocked", summary=materialized, payload=enrich_payload)
        save_ledger(ledger_path, ledger)
        emit({"status": "blocked_approval", "step_id": "messages_enrich_people", "ledger": str(ledger_path), "child": enrich_payload})
        return False
    if code != 0:
        mark_step(ledger, "messages_enrich_people", "failed", summary=materialized, error=stderr or enrich_payload)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "messages_enrich_people", "error": stderr or enrich_payload})
        return False
    for key, value in (enrich_payload.get("artifacts") or {}).items():
        artifacts[f"messages_enriched_{key}"] = value
    enriched_people = enrich_payload.get("artifacts", {}).get("people_csv") or materialized.get("people_csv")
    final_people = str(enriched_people or "")
    messages_merge: dict[str, Any] = {"status": "skipped", "people_csv": str(DEFAULT_BASE_DIR / "messages" / "people.messages.csv"), "rows": 0, "input_csvs": []}
    if enriched_people:
        messages_merge = materialize_messages_merged_people_csv(
            [str(enriched_people)],
            DEFAULT_BASE_DIR / "messages" / "people.messages.csv",
        )
        artifacts["messages_merged_people"] = messages_merge
        if messages_merge.get("status") == "completed" and messages_merge.get("people_csv"):
            final_people = str(messages_merge["people_csv"])
            artifacts["messages_merged_people_csv"] = final_people
        artifacts["messages_people_csv"] = final_people
        artifacts["messages_final_people_csvs"] = [final_people] if final_people else []
        # Keep the legacy plural key as an alias, but never let it accumulate
        # old run dirs. Fan-in should see one canonical Messages artifact.
        artifacts["messages_people_csvs"] = [final_people] if final_people else []
    enrich_payload = {**enrich_payload, "messages_merged_people": messages_merge}
    if final_people:
        artifacts["messages_directory_checkpoint"] = commit_people_csv_to_directory(
            ledger.get("input", {}),
            artifacts,
            final_people,
            source="messages",
        )
    mark_step(ledger, "messages_enrich_people", "completed", summary=materialized, payload=enrich_payload)
    emit_progress(f"Messages profile enrichment completed: {materialized.get('rows_written', 0)} LinkedIn rows prepared.")
    return True


def merge_input_paths(ledger: dict[str, Any], merge_dir: Path) -> list[str]:
    input_cfg = ledger.get("input", {}) or {}
    artifacts = ledger.get("artifacts", {}) or {}
    include_existing = bool(input_cfg.get("include_existing_artifacts"))
    explicit_inputs: list[str] = []
    canonical_people = DEFAULT_BASE_DIR / "merged" / "people.csv"
    if include_existing and canonical_people.exists():
        explicit_inputs.append(str(canonical_people))

    gmail_inputs = unique_strings(artifacts.get("gmail_final_people_csvs") or [])
    if not gmail_inputs and artifacts.get("gmail_merged_people_csv"):
        gmail_inputs = [str(artifacts["gmail_merged_people_csv"])]

    explicit_inputs.extend(
        value for key, value in sorted(artifacts.items())
        if key in {"linkedin_people_csv"} and value
    )
    if gmail_inputs:
        explicit_inputs.extend(str(path) for path in gmail_inputs if path)

    messages_people_inputs = unique_strings(artifacts.get("messages_final_people_csvs") or [])
    if not messages_people_inputs and artifacts.get("messages_merged_people_csv"):
        messages_people_inputs = [str(artifacts["messages_merged_people_csv"])]
    explicit_inputs.extend(str(path) for path in messages_people_inputs if path)

    messages_contacts = ""
    if input_cfg.get("allow_unreviewed_messages"):
        messages_contacts = artifacts.get("messages_contacts_csv") or input_cfg.get("messages_contacts_csv")
    if messages_contacts:
        message_input = Path(messages_contacts)
        # `merge_network_sources` recognizes message contact CSVs by a
        # `/messages/contacts.csv` path segment. A linked source may live
        # elsewhere, so copy it into this run's fan-in scratch area before
        # passing it as an explicit input.
        if message_input.exists():
            scratch = merge_dir / "source-inputs" / "messages" / "contacts.csv"
            scratch.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(message_input, scratch)
            explicit_inputs.append(str(scratch))
        else:
            explicit_inputs.append(str(message_input))

    return list(dict.fromkeys(explicit_inputs))


def run_merge(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    begin_step(ledger_path, ledger, "merge", "Merging network sources.")
    merge_dir = artifact_dir_from_ledger(ledger) / "merged"
    cmd = py_cmd(
        "packs/ingestion/primitives/merge_network_sources/merge_network_sources.py",
        "run",
        "--output-dir", str(merge_dir),
    )
    explicit_inputs = merge_input_paths(ledger, merge_dir)
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


def run_pipeline(ledger_path: Path, *, resume: bool = False) -> int:
    ledger = load_ledger(ledger_path)
    if not ledger.get("input", {}).get("fan_in_only") and ledger.get("steps", {}).get("source_imports", {}).get("status") not in {"completed", "skipped"}:
        if not run_source_import_workers(ledger_path, ledger, resume=resume):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if ledger.get("input", {}).get("source_import_only"):
        ledger["status"] = "source_import_completed"
        save_ledger(ledger_path, ledger)
        emit({"status": "source_import_completed", "ledger": str(ledger_path), "artifact_dir": str(artifact_dir_from_ledger(ledger)), "steps": ledger.get("steps", {}), "artifacts": ledger.get("artifacts", {})})
        return 0
    selected_sources = set(unique_strings(ledger.get("input", {}).get("only_sources")))
    enrichment_only = bool(ledger.get("input", {}).get("enrichment_only"))
    merge_only = bool(ledger.get("input", {}).get("merge_only"))
    selected_fan_in_sources = selected_sources if ledger.get("input", {}).get("fan_in_only") or enrichment_only else set()
    run_gmail_enrichment = (not selected_fan_in_sources or "gmail" in selected_fan_in_sources) and not merge_only
    run_messages_profile_enrichment = (not selected_fan_in_sources or "messages" in selected_fan_in_sources) and not merge_only
    if enrichment_only:
        if run_gmail_enrichment:
            if not run_gmail_directory(ledger_path, ledger):
                return 1
            save_ledger(ledger_path, ledger)
            if not run_gmail_linkedin_resolution(ledger_path, ledger):
                return 20 if ledger.get("blocked") else 1
            save_ledger(ledger_path, ledger)
            if not run_gmail_apply_and_enrich(ledger_path, ledger):
                return 20 if ledger.get("blocked") else 1
            save_ledger(ledger_path, ledger)
        if run_messages_profile_enrichment:
            if not run_messages_enrichment(ledger_path, ledger):
                return 20 if ledger.get("blocked") else 1
            save_ledger(ledger_path, ledger)
        ledger["status"] = "source_enrichment_completed"
        ledger.pop("blocked", None)
        save_ledger(ledger_path, ledger)
        emit({"status": "source_enrichment_completed", "ledger": str(ledger_path), "artifact_dir": str(artifact_dir_from_ledger(ledger)), "steps": ledger.get("steps", {}), "artifacts": ledger.get("artifacts", {})})
        return 0
    if ledger.get("input", {}).get("only_sources") and not ledger.get("input", {}).get("fan_in_only"):
        if "messages" in selected_sources and ledger.get("steps", {}).get("messages_enrich_people", {}).get("status") not in {"completed", "skipped"}:
            if not run_messages_enrichment(ledger_path, ledger):
                return 20 if ledger.get("blocked") else 1
            save_ledger(ledger_path, ledger)
        ledger["status"] = "source_import_completed"
        save_ledger(ledger_path, ledger)
        emit({"status": "source_import_completed", "ledger": str(ledger_path), "artifact_dir": str(artifact_dir_from_ledger(ledger)), "steps": ledger.get("steps", {}), "artifacts": ledger.get("artifacts", {})})
        return 0
    if run_gmail_enrichment:
        if not run_gmail_directory(ledger_path, ledger):
            return 1
        save_ledger(ledger_path, ledger)
    if run_gmail_enrichment:
        if not run_gmail_linkedin_resolution(ledger_path, ledger):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if run_gmail_enrichment:
        if not run_gmail_apply_and_enrich(ledger_path, ledger):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if run_messages_profile_enrichment:
        if not run_messages_enrichment(ledger_path, ledger):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if not run_merge(ledger_path, ledger):
        return 1
    ledger["status"] = "completed"
    ledger.pop("blocked", None)
    save_ledger(ledger_path, ledger)
    emit({"status": "completed", "ledger": str(ledger_path), "artifact_dir": str(artifact_dir_from_ledger(ledger)), "artifacts": ledger.get("artifacts", {})})
    return 0


def step_matches_source(step_id: str, selected_sources: set[str]) -> bool:
    for source in selected_sources:
        for prefix in SOURCE_STEP_PREFIXES.get(source, ()):
            if step_id == prefix or step_id.startswith(f"{prefix}:"):
                return True
    return False


def artifact_matches_source(key: str, selected_sources: set[str]) -> bool:
    for source in selected_sources:
        for prefix in SOURCE_ARTIFACT_PREFIXES.get(source, ()):
            if key.startswith(prefix):
                return True
    return False


def preserved_state_for_source_refresh(existing: dict[str, Any], selected_sources: set[str]) -> dict[str, Any]:
    """Carry untouched source outputs across one-source refreshes on the shared setup ledger."""
    if not existing:
        return {}
    artifacts = {
        key: copy.deepcopy(value)
        for key, value in (existing.get("artifacts") or {}).items()
        if key not in MERGED_ARTIFACT_KEYS and not artifact_matches_source(key, selected_sources)
    }
    steps = {
        key: copy.deepcopy(value)
        for key, value in (existing.get("steps") or {}).items()
        if key not in {"source_imports", "merge", "duckdb"} and not step_matches_source(key, selected_sources)
    }
    source_imports = {
        key: copy.deepcopy(value)
        for key, value in (existing.get("source_imports") or {}).items()
        if not step_matches_source(key, selected_sources)
    }
    return {"artifacts": artifacts, "steps": steps, "source_imports": source_imports}


GMAIL_ENRICHMENT_ARTIFACT_KEYS = {
    "gmail_directory_resolution_records",
    "gmail_unresolved_linkedin_resolution_queue_csvs",
    "gmail_cached_negative_linkedin_resolution_queue_csvs",
    "gmail_linkedin_resolutions_csvs",
    "gmail_linkedin_resolution_ledgers",
    "gmail_linkedin_resolution_ledger",
    "gmail_linkedin_resolutions_csv",
    "gmail_linkedin_raw_resolutions_csv",
    "gmail_linkedin_resolutions_by_slug",
    "gmail_linkedin_harness_prompts_jsonls",
    "gmail_linkedin_harness_prompts_jsonl",
    "gmail_linkedin_harness_instructions",
    "gmail_resolved_people_csvs",
    "gmail_resolved_people_csv",
    "gmail_enrich_people_ledgers",
    "gmail_enrich_people_ledger",
    "gmail_final_people_csvs",
    "gmail_account_final_people_csvs",
    "gmail_merged_people_csv",
    "gmail_merged_people",
    "gmail_combined_resolutions_csvs",
    "gmail_apply_enrich_by_slug",
}


def reset_selected_fan_in_state(preserved: dict[str, Any], selected_sources: set[str]) -> dict[str, Any]:
    if not preserved or not selected_sources:
        return preserved
    steps = preserved.setdefault("steps", {})
    artifacts = preserved.setdefault("artifacts", {})
    if "gmail" in selected_sources:
        for step in ["gmail_directory", "gmail_linkedin_resolution", "gmail_apply_enrich"]:
            steps.pop(step, None)
        for key in list(artifacts):
            if key in GMAIL_ENRICHMENT_ARTIFACT_KEYS or key.startswith("gmail_directory_by_slug") or key.startswith("gmail_") and "_enriched_" in key:
                artifacts.pop(key, None)
    if "messages" in selected_sources:
        steps.pop("messages_enrich_people", None)
        for key in list(artifacts):
            if key.startswith("messages_enriched_") or key in {"messages_people_csv", "messages_people_csvs", "messages_final_people_csvs", "messages_merged_people_csv", "messages_merged_people", "messages_people_input_csv", "messages_people_input_manifest", "messages_enrich_people_ledger"}:
                artifacts.pop(key, None)
    return preserved


def cmd_run(args: argparse.Namespace) -> int:
    args = apply_account_sources(args)
    selected_sources = set(unique_strings(getattr(args, "only_source", [])))
    artifact_dir = default_artifact_dir(args, selected_sources)
    ledger_path = Path(args.ledger)
    if args.dry_run or args.estimate:
        emit(dry_run_plan(args, ledger_path, artifact_dir))
        return 0
    existing = load_ledger(ledger_path) if ledger_path.exists() else {}
    if ledger_path.exists() and not args.force:
        if existing.get("status") == "completed":
            emit({
                "status": "completed",
                "cached": True,
                "ledger": str(ledger_path),
                "artifact_dir": existing.get("artifact_dir") or existing.get("run_dir"),
                "message": "Existing completed discover-contacts ledger found; no work was run.",
                "artifact_check": check_artifact_paths(existing),
                "artifacts": existing.get("artifacts", {}),
            })
            return 0
        if existing.get("status") not in {"failed"}:
            emit({"status": "active_run_exists", "ledger": str(ledger_path), "message": "Use continue/approve or --force."})
            return 0
    preserve_sources = set() if args.fan_in_only else selected_sources
    preserved = preserved_state_for_source_refresh(existing, preserve_sources) if args.force and (selected_sources or args.fan_in_only) else {}
    if args.force and args.fan_in_only and selected_sources:
        preserved = reset_selected_fan_in_state(preserved, selected_sources)
    ledger = {
        "primitive": "discover_contacts_pipeline",
        "version": 1,
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "artifact_dir": str(artifact_dir),
        "ledger": str(ledger_path),
        "input": {
            "operator_id": args.operator_id,
            "linkedin_csv": args.linkedin_csv,
            "linkedin_source_user": args.linkedin_source_user,
            "linkedin_limit": args.linkedin_limit,
            "msgvault_db": resolve_msgvault_db(args),
            "gmail_account_email": args.gmail_account_email,
            "gmail_account_emails": unique_strings(getattr(args, "gmail_account_emails", [])),
            "gmail_limit": args.gmail_limit,
            "include_automated_gmail": args.include_automated_gmail,
            "gmail_exclude_labels": normalize_label_names(getattr(args, "gmail_exclude_label", [])),
            "include_category_mail": bool(getattr(args, "include_category_mail", False)),
            "gmail_sync_query": str(getattr(args, "gmail_sync_query", "") or "").strip(),
            "gmail_sync_after": gmail_sync_after({"gmail_sync_after": getattr(args, "gmail_sync_after", "")}),
            "skip_gmail_estimate": bool(getattr(args, "skip_gmail_estimate", False)),
            "gmail_estimate_max_pages": int(getattr(args, "gmail_estimate_max_pages", DEFAULT_GMAIL_ESTIMATE_MAX_PAGES) or DEFAULT_GMAIL_ESTIMATE_MAX_PAGES),
            "gmail_linkedin_provider": args.gmail_linkedin_provider,
            "resolve_gmail_linkedin": args.resolve_gmail_linkedin,
            "approve_parallel_spend": bool(getattr(args, "approve_parallel_spend", False)),
            "gmail_linkedin_limit": args.gmail_linkedin_limit,
            "gmail_resolutions_csv": args.gmail_resolutions_csv,
            "linkedin_directory_csv": args.linkedin_directory_csv,
            "linkedin_directory_source_csvs": unique_strings(getattr(args, "linkedin_directory_source_csv", [])),
            "linkedin_directory_use_defaults": not bool(getattr(args, "no_default_linkedin_directory_sources", False)),
            "include_existing_artifacts": args.include_existing_artifacts,
            "skip_msgvault_sync": args.skip_msgvault_sync,
            "from_accounts": args.from_accounts,
            "from_setup": args.from_setup,
            "only_sources": unique_strings(getattr(args, "only_source", [])),
            "fan_in_only": args.fan_in_only,
            "source_import_only": args.source_import_only,
            "enrichment_only": bool(getattr(args, "enrichment_only", False)),
            "merge_only": bool(getattr(args, "merge_only", False)),
            "twitter_handle": getattr(args, "twitter_handle", ""),
            "messages_review_csv": getattr(args, "messages_review_csv", ""),
            "messages_contacts_csv": getattr(args, "messages_contacts_csv", ""),
            "allow_unreviewed_messages": bool(getattr(args, "allow_unreviewed_messages", False)),
        },
        "steps": {},
        "artifacts": {},
    }
    if preserved:
        ledger["steps"].update(preserved.get("steps") or {})
        ledger["artifacts"].update(preserved.get("artifacts") or {})
        if preserved.get("source_imports"):
            ledger["source_imports"] = preserved["source_imports"]
    save_ledger(ledger_path, ledger)
    return run_pipeline(ledger_path, resume=False)


def dry_run_plan(args: argparse.Namespace, ledger_path: Path, artifact_dir: Path) -> dict[str, Any]:
    args = apply_account_sources(args)
    if ledger_path.exists():
        ledger = load_ledger(ledger_path)
        steps = ledger.get("steps", {}) or {}
        if ledger.get("status") == "completed":
            would_run = []
        else:
            would_run = [
                step for step in ["linkedin", "gmail_msgvault", "gmail_directory", "gmail_linkedin_resolution", "gmail_apply_enrich", "messages_enrich_people", "merge"]
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
            "artifact_dir": ledger.get("artifact_dir") or ledger.get("run_dir") or str(artifact_dir),
            "existing_status": ledger.get("status", "unknown"),
            "would_run_steps": would_run,
            "estimated_paid_calls": 0 if not would_run else "unknown_without_running_child_stage_plans",
            "child_paid_call_counts": child_paid,
            "gmail_api_estimates": (ledger.get("artifacts") or {}).get("gmail_api_estimates") or [],
            "artifact_check": check_artifact_paths(ledger),
        }
    would_run = []
    if args.linkedin_csv:
        would_run.append("linkedin")
    messages_review_csv = getattr(args, "messages_review_csv", "")
    if messages_review_csv and not Path(str(messages_review_csv)).exists():
        messages_review_csv = ""
    input_cfg = {
        "linkedin_csv": args.linkedin_csv,
        "msgvault_db": resolve_msgvault_db(args),
        "gmail_account_email": args.gmail_account_email,
        "gmail_account_emails": unique_strings(getattr(args, "gmail_account_emails", [])),
        "gmail_exclude_labels": normalize_label_names(getattr(args, "gmail_exclude_label", [])),
        "include_category_mail": bool(getattr(args, "include_category_mail", False)),
        "gmail_sync_query": str(getattr(args, "gmail_sync_query", "") or "").strip(),
        "gmail_sync_after": gmail_sync_after({"gmail_sync_after": getattr(args, "gmail_sync_after", "")}),
        "skip_gmail_estimate": bool(getattr(args, "skip_gmail_estimate", False)),
        "gmail_estimate_max_pages": int(getattr(args, "gmail_estimate_max_pages", DEFAULT_GMAIL_ESTIMATE_MAX_PAGES) or DEFAULT_GMAIL_ESTIMATE_MAX_PAGES),
        "twitter_handle": getattr(args, "twitter_handle", ""),
        "messages_review_csv": messages_review_csv,
        "messages_contacts_csv": getattr(args, "messages_contacts_csv", ""),
        "include_existing_artifacts": getattr(args, "include_existing_artifacts", False),
        "linkedin_directory_csv": getattr(args, "linkedin_directory_csv", str(DEFAULT_DIRECTORY_CSV)),
        "linkedin_directory_source_csvs": unique_strings(getattr(args, "linkedin_directory_source_csv", [])),
        "linkedin_directory_use_defaults": not bool(getattr(args, "no_default_linkedin_directory_sources", False)),
    }
    gmail_emails = unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email"))
    gmail_estimates = estimate_gmail_accounts_via_api(input_cfg, gmail_emails) if gmail_emails else []
    fan_in_only = bool(getattr(args, "fan_in_only", False))
    source_import_only = bool(getattr(args, "source_import_only", False))
    if not fan_in_only and (args.gmail_account_email or unique_strings(getattr(args, "gmail_account_emails", [])) or resolve_msgvault_db(args)):
        would_run.append("gmail_msgvault")
    if not source_import_only and (args.gmail_account_email or unique_strings(getattr(args, "gmail_account_emails", [])) or resolve_msgvault_db(args)):
        would_run.append("gmail_directory")
    if not source_import_only and (getattr(args, "resolve_gmail_linkedin", False) or getattr(args, "gmail_linkedin_provider", "off") != "off"):
        would_run.append("gmail_linkedin_resolution")
    if not source_import_only and getattr(args, "gmail_resolutions_csv", ""):
        would_run.append("gmail_apply_enrich")
    if not source_import_only and messages_review_csv:
        would_run.append("messages_enrich_people")
    if not source_import_only:
        would_run.append("merge")
    return {
        "status": "dry_run",
        "ledger": str(ledger_path),
        "artifact_dir": str(artifact_dir),
        "existing_status": "missing",
        "would_run_steps": would_run,
        "worker_groups": {} if fan_in_only else {"import": source_worker_group(input_cfg)},
        "gmail_api_estimates": gmail_estimates,
        "gmail_estimate_summary": summarize_gmail_estimates(gmail_estimates) if gmail_estimates else "",
        "estimated_paid_calls": "unknown_without_existing_stage_outputs",
        "message": "No existing discover-contacts ledger was found; running would execute the listed stages until any child approval confirmation.",
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
    if blocked.get("step_id") == "messages_enrich_people" and blocked.get("child_ledger"):
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
        "artifact_dir": ledger.get("artifact_dir") or ledger.get("run_dir"),
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
    run.add_argument("--from-accounts", default="", help="Account registry path produced by onboarding; fills source-specific args unless explicit flags override it")
    run.add_argument("--from-setup", default="", help="Setup ledger/handoff path containing an accounts path")
    run.add_argument("--operator-id", default="local")
    run.add_argument("--linkedin-csv", default="")
    run.add_argument("--linkedin-source-user", default="")
    run.add_argument("--linkedin-limit", type=int)
    run.add_argument("--msgvault-db", default="", help=f"msgvault SQLite DB; defaults to {DEFAULT_MSGVAULT_DB} when --gmail-account-email is set")
    run.add_argument("--gmail-account-email", default="")
    run.add_argument("--gmail-account-emails", action="append", default=[], help="Gmail/msgvault account email to import; may be repeated")
    run.add_argument("--gmail-limit", type=int)
    run.add_argument("--include-automated-gmail", action="store_true")
    run.add_argument("--gmail-exclude-label", action="append", default=[], help="Exclude this Gmail/msgvault label during sync/import; may be repeated. Defaults to Social, Promotions, Forums, Updates.")
    run.add_argument("--include-category-mail", action="store_true", help="Do not exclude Gmail Social, Promotions, Forums, and Updates categories during sync/import")
    run.add_argument("--gmail-sync-query", default="", help="Override the Gmail search query passed to msgvault sync-full and the Gmail API estimate")
    run.add_argument("--gmail-sync-after", default="", help="Pass --after YYYY-MM-DD to msgvault sync-full for bounded Gmail refreshes")
    run.add_argument("--skip-gmail-estimate", action="store_true", help="Skip the pre-sync Gmail API label/count estimate")
    run.add_argument("--gmail-estimate-max-pages", type=int, default=DEFAULT_GMAIL_ESTIMATE_MAX_PAGES, help=argparse.SUPPRESS)
    run.add_argument("--resolve-gmail-linkedin", action="store_true", help="Resolve Gmail contacts to LinkedIn with Parallel before applying Gmail enrichment.")
    run.add_argument("--approve-parallel-spend", action="store_true", help="Auto-approve Parallel.ai spend without blocking for confirmation.")
    run.add_argument("--gmail-linkedin-provider", choices=["off", "harness", "parallel"], default="off", help=argparse.SUPPRESS)
    run.add_argument("--gmail-linkedin-limit", type=int, help=argparse.SUPPRESS)
    run.add_argument("--gmail-resolutions-csv", default="", help="Existing linkedin_resolutions.csv to apply to Gmail people before shared enrich_people")
    run.add_argument("--linkedin-directory-csv", default=str(DEFAULT_DIRECTORY_CSV), help=argparse.SUPPRESS)
    run.add_argument("--linkedin-directory-source-csv", action="append", default=[], help=argparse.SUPPRESS)
    run.add_argument("--no-default-linkedin-directory-sources", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--include-existing-artifacts", action="store_true", help="Merge all discovered existing LinkedIn/Gmail/Twitter/message artifacts instead of only artifacts produced by this run")
    run.add_argument("--skip-msgvault-sync", action="store_true", help="Skip import-time msgvault sync-full and read the existing DB as-is")
    run.add_argument("--twitter-handle", default="", help=argparse.SUPPRESS)
    run.add_argument("--messages-review-csv", default="", help=argparse.SUPPRESS)
    run.add_argument("--messages-contacts-csv", default="", help=argparse.SUPPRESS)
    run.add_argument("--allow-unreviewed-messages", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--only-source", action="append", default=[], choices=SOURCE_NAMES, help="Run only a source import worker; skips fan-in merge unless --fan-in-only is set separately")
    run.add_argument("--fan-in-only", action="store_true", help="Skip source import workers and merge existing artifacts")
    run.add_argument("--source-import-only", action="store_true", help="Run raw source imports only; skip resolution, enrichment, and merge")
    run.add_argument("--enrichment-only", action="store_true", help="Run source-specific enrichment and stop before merge")
    run.add_argument("--merge-only", action="store_true", help="Run only merge materialization; skip source-specific enrichment")
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
