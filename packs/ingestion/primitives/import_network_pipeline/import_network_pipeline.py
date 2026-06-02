#!/usr/bin/env python3
"""Orchestrate local network ingestion sources into merged CSVs + DuckDB.

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
from typing import Any

try:
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        extract_public_identifier,
        normalize_linkedin_url,
        normalize_people_row,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.schemas.people_schema import (
        PEOPLE_SCHEMA_COLUMNS,
        extract_public_identifier,
        normalize_linkedin_url,
        normalize_people_row,
    )

DEFAULT_LEDGER = Path(".powerpacks/network-import/import-network-run.json")
DEFAULT_BASE_DIR = Path(".powerpacks/network-import")
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


def emit_progress(message: str) -> None:
    print(f"[import-network] {message}", file=sys.stderr, flush=True)


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
        reader = csv.DictReader(handle)
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


def normalized_directory_row(row: dict[str, Any], *, source_artifact: str = "", source: str = "", updated_at: str = "") -> dict[str, str]:
    linkedin_url = normalize_linkedin_url(str(row.get("linkedin_url") or ""))
    public_identifier = extract_public_identifier(linkedin_url)
    email = (str(row.get("email") or row.get("primary_email") or "").strip().lower())
    phone = normalize_phone(row.get("phone") or row.get("primary_phone") or "")
    name = str(row.get("name") or row.get("matched_name") or row.get("display_name") or row.get("full_name") or "").strip()
    source_key = str(row.get("source_key") or "").strip()
    if not source_key:
        source_key = directory_identity_key(email, phone, name, public_identifier)
    if not linkedin_url or not public_identifier or not source_key:
        return {}
    confidence = parse_confidence(row.get("confidence"), 0.0)
    output = {
        "source": str(row.get("source") or source or "directory"),
        "source_key": source_key,
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
    root = DEFAULT_BASE_DIR.parent
    patterns = [
        "operator-bootstrap/import/resolution/linkedin_resolutions*.csv",
        "network-bootstrap/operators/*/resolution/linkedin_resolutions*.csv",
        "operator-bootstrap/import/linkedin_candidates/linkedin_candidates*.csv",
        "network-bootstrap/operators/*/inputs/linkedin_candidates/linkedin_candidates*.csv",
        "network-import/network-runs/*/gmail-linkedin-resolution-*/linkedin_resolutions.csv",
    ]
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(root.glob(pattern))
    return sorted({path for path in paths if path.is_file()})


def directory_source_paths(input_cfg: dict[str, Any], artifacts: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for value in unique_strings(input_cfg.get("linkedin_directory_source_csvs")):
        path = Path(value)
        if path.exists():
            paths.append(path)
    for value in unique_strings(input_cfg.get("gmail_resolutions_csv")):
        path = Path(value)
        if path.exists():
            paths.append(path)
    for record in artifacts.get("gmail_linkedin_resolutions_csvs") or []:
        if isinstance(record, dict) and record.get("resolutions_csv"):
            path = Path(record["resolutions_csv"])
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
        if not normalized or parse_confidence(normalized.get("confidence"), 0.0) < min_confidence:
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


def apply_directory_to_gmail_queue(record: dict[str, Any], directory_csv: Path, output_dir: Path) -> dict[str, Any]:
    queue_csv = Path(str(record.get("queue_csv") or ""))
    fields, rows = read_csv_rows(queue_csv)
    lookup = load_directory_lookup(directory_csv)
    resolved: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []
    for row in rows:
        match = directory_match_for_queue_row(row, lookup)
        if match:
            resolved.append(resolution_from_directory_match(row, match))
        else:
            unresolved.append(row)
    output_dir.mkdir(parents=True, exist_ok=True)
    resolutions_csv = output_dir / "directory_linkedin_resolutions.csv"
    unresolved_csv = output_dir / "unresolved_linkedin_resolution_queue.csv"
    write_csv_rows(resolutions_csv, LINKEDIN_RESOLUTION_COLUMNS, resolved)
    write_csv_rows(unresolved_csv, fields, unresolved)
    result = dict(record)
    result.update({
        "directory_csv": str(directory_csv),
        "directory_resolutions_csv": str(resolutions_csv),
        "unresolved_queue_csv": str(unresolved_csv),
        "input_rows": len(rows),
        "resolved": len(resolved),
        "unresolved": len(unresolved),
    })
    return result


def merge_resolution_rows(resolution_paths: list[Path]) -> list[dict[str, str]]:
    best: dict[str, dict[str, str]] = {}
    for path in resolution_paths:
        if not path.exists():
            continue
        for row in read_csv_rows(path)[1]:
            status = (row.get("status") or "").strip().lower()
            linkedin_url = normalize_linkedin_url(row.get("linkedin_url") or "")
            public_identifier = extract_public_identifier(linkedin_url)
            handle = (row.get("handle") or "").strip().lower()
            confidence = parse_confidence(row.get("confidence"), 0.0)
            if status != "found" or not public_identifier or not handle or confidence < 0.75:
                continue
            candidate = {col: row.get(col, "") for col in LINKEDIN_RESOLUTION_COLUMNS}
            candidate["handle"] = handle
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


def sha(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def default_messages_review_csv() -> Path:
    return DEFAULT_BASE_DIR.parent / "messages" / "research_review.csv"


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
    cmd = str(commands.get("import_network_run") or "")
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
    ledger.setdefault("primitive", "import_network_pipeline")
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
    child_ledger = Path(ledger["run_dir"]) / "linkedin.ledger.json"
    if mode == "run":
        cmd = py_cmd(
            "packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py",
            "run",
            "--csv", input_cfg["linkedin_csv"],
            "--source-user", input_cfg.get("linkedin_source_user") or "local",
            "--operator-id", input_cfg.get("operator_id") or "local",
            "--output-dir", str(DEFAULT_BASE_DIR),
            "--ledger", str(child_ledger),
            "--run-id", f"{ledger['run_id']}-linkedin",
            "--force",
        )
        if input_cfg.get("linkedin_limit") is not None:
            cmd.extend(["--limit", str(input_cfg["linkedin_limit"])])
    else:
        cmd = py_cmd("packs/ingestion/primitives/linkedin_network_import/linkedin_network_import.py", "continue", "--ledger", str(child_ledger))
    code, payload, stderr = run_cmd(cmd)
    return {"id": "linkedin_csv", "source": "linkedin_csv", "child_ledger": str(child_ledger), "command": cmd, "code": code, "payload": payload, "stderr": stderr}


def record_linkedin_worker_result(ledger_path: Path, ledger: dict[str, Any], result: dict[str, Any]) -> bool:
    code = int(result.get("code") or 0)
    payload = result.get("payload") or {}
    stderr = result.get("stderr") or ""
    child_ledger = result.get("child_ledger") or str(Path(ledger["run_dir"]) / "linkedin.ledger.json")
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
    run_id = f"{ledger['run_id']}-gmail-{source_slug(email or 'all') or index}"
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
                "run_id": run_id,
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
        "--run-id", run_id,
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
    return {"id": f"gmail:{email or 'all'}", "source": "gmail", "account_email": email, "run_id": run_id, "sync_command": sync_command, "sync_skipped_reason": sync_skipped_reason, "excluded_labels": excluded_labels, "sync_query": sync_query, "sync_after": sync_after, "sync_after_source": sync_after_source, "gmail_estimate": estimate, "command": cmd, "code": code, "payload": payload, "stderr": stderr, "phase": "gmail_network_import"}


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
    ledger.setdefault("source_imports", {})[step_id] = {"status": "completed", "source": "gmail", "account_email": email, "run_id": result.get("run_id"), "sync_command": result.get("sync_command"), "sync_skipped_reason": result.get("sync_skipped_reason"), "excluded_labels": result.get("excluded_labels"), "sync_query": result.get("sync_query"), "sync_after": result.get("sync_after"), "sync_after_source": result.get("sync_after_source"), "gmail_estimate": result.get("gmail_estimate")}
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
    max_workers = min(len(emails), int(os.environ.get("POWERPACKS_IMPORT_NETWORK_GMAIL_MAX_WORKERS", "4"))) or 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(run_gmail_msgvault_account, ledger, email, index) for index, email in enumerate(emails)]
        results = [future.result() for future in futures]
    for result in results:
        if not record_gmail_worker_result(ledger, result):
            ok = False
    if ok:
        mark_step(ledger, "gmail_msgvault", "completed", accounts=emails, parallelizable=True)
    return ok


def source_worker_group(input_cfg: dict[str, Any], run_id: str) -> dict[str, Any]:
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
                "artifact_root": str(Path(DEFAULT_BASE_DIR) / "gmail" / f"{run_id}-gmail-{source_slug(email or 'all')}"),
                "sync_query": gmail_sync_query(input_cfg),
                "sync_after": gmail_sync_after(input_cfg),
                "excluded_labels": gmail_excluded_labels(input_cfg),
                "parallelizable": True,
                "reason": "local msgvault metadata read with isolated output run id",
            })
    if input_cfg.get("linkedin_csv"):
        jobs.append({
            "id": "linkedin_csv",
            "source": "linkedin_csv",
            "step_id": "linkedin",
            "ledger": str(Path(DEFAULT_BASE_DIR) / "network-runs" / run_id / "linkedin.ledger.json"),
            "artifact_root": str(Path(DEFAULT_BASE_DIR) / "linkedin" / f"{run_id}-linkedin"),
            "parallelizable": True,
            "reason": "CSV conversion/enrichment uses its own child ledger and cache confirmations",
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
    default_review_csv = default_messages_review_csv()
    messages_review_csv = input_cfg.get("messages_review_csv") or (str(default_review_csv) if input_cfg.get("include_existing_artifacts") and default_review_csv.exists() else "")
    if messages_review_csv and not Path(str(messages_review_csv)).exists():
        messages_review_csv = ""
    if input_cfg.get("messages_contacts_csv") or messages_review_csv or input_cfg.get("include_existing_artifacts"):
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
    return {"parallel": True, "fan_in": "merge_network_sources_then_duckdb_after_nonblocked_workers", "jobs": jobs}


def run_source_import_workers(ledger_path: Path, ledger: dict[str, Any], *, resume: bool = False) -> bool:
    input_cfg = ledger.get("input", {})
    group = source_worker_group(input_cfg, ledger["run_id"])
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
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, len(gmail_emails) + (1 if input_cfg.get("linkedin_csv") else 0) or 1))) as executor:
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
    checkpoint = build_directory_checkpoint(input_cfg, artifacts)
    directory_csv = Path(checkpoint["directory_csv"])
    artifacts["directory_csv"] = str(directory_csv)
    by_slug = artifacts.setdefault("gmail_directory_by_slug", {})
    artifacts["gmail_directory_resolution_records"] = []
    artifacts["gmail_unresolved_linkedin_resolution_queue_csvs"] = []
    results = []
    total_resolved = 0
    total_unresolved = 0
    for index, record in enumerate(queue_records):
        slug = source_slug(record.get("account_email") or record.get("slug") or f"queue-{index}")
        existing = by_slug.get(slug) if isinstance(by_slug.get(slug), dict) else {}
        if existing.get("unresolved_queue_csv") and existing.get("directory_resolutions_csv"):
            result = existing
        else:
            out_dir = Path(ledger["run_dir"]) / f"gmail-directory-{slug}"
            result = apply_directory_to_gmail_queue(record, directory_csv, out_dir)
            result["slug"] = slug
            by_slug[slug] = result
        total_resolved += int(result.get("resolved") or 0)
        total_unresolved += int(result.get("unresolved") or 0)
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
        results.append(result)
    mark_step(ledger, "gmail_directory", "completed", checkpoint=checkpoint, resolved=total_resolved, unresolved=total_unresolved, payload={"results": results})
    emit_progress(f"Gmail directory mappings applied: {total_resolved} resolved, {total_unresolved} unresolved.")
    return True


def run_gmail_linkedin_resolution(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    input_cfg = ledger.get("input", {})
    provider = input_cfg.get("gmail_linkedin_provider") or "off"
    artifacts = ledger.setdefault("artifacts", {})
    queue_records = artifacts.get("gmail_unresolved_linkedin_resolution_queue_csvs") or gmail_queue_records(artifacts)
    if provider == "off" or not queue_records:
        mark_step(ledger, "gmail_linkedin_resolution", "skipped", reason="provider off or no queue")
        return True
    queue_records = ordered_records([record for record in queue_records if isinstance(record, dict) and record.get("queue_csv") and csv_row_count(Path(str(record.get("queue_csv")))) > 0], unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")))
    if not queue_records:
        mark_step(ledger, "gmail_linkedin_resolution", "skipped", reason="all Gmail queue rows resolved by directory")
        return True
    by_slug = artifacts.setdefault("gmail_linkedin_resolutions_by_slug", {})
    artifacts["gmail_linkedin_resolutions_csvs"] = []
    artifacts["gmail_linkedin_resolution_ledgers"] = []
    begin_step(ledger_path, ledger, "gmail_linkedin_resolution", f"Resolving Gmail contacts to LinkedIn for {len(queue_records)} account queue(s).")
    results = []
    for index, record in enumerate(queue_records):
        queue = record.get("queue_csv")
        if not queue:
            continue
        slug = source_slug(record.get("account_email") or record.get("slug") or f"queue-{index}")
        existing = by_slug.get(slug) if isinstance(by_slug.get(slug), dict) else {}
        if existing.get("resolutions_csv"):
            artifacts["gmail_linkedin_resolutions_csvs"].append(existing)
            if existing.get("ledger"):
                artifacts["gmail_linkedin_resolution_ledgers"].append(existing["ledger"])
            results.append(existing)
            continue
        child_ledger = Path(ledger["run_dir"]) / f"gmail-linkedin-resolution.{slug}.ledger.json"
        out_dir = Path(ledger["run_dir"]) / f"gmail-linkedin-resolution-{slug}"
        cmd = py_cmd(
            "packs/ingestion/primitives/resolve_linkedin_queue/resolve_linkedin_queue.py",
            "run",
            "--provider", provider,
            "--input", str(queue),
            "--output-dir", str(out_dir),
            "--ledger", str(child_ledger),
        )
        if input_cfg.get("gmail_linkedin_limit") is not None:
            cmd.extend(["--limit", str(input_cfg["gmail_linkedin_limit"])])
        code, payload, stderr = run_cmd(cmd)
        artifacts.setdefault("gmail_linkedin_resolution_ledgers", []).append(str(child_ledger))
        if "gmail_linkedin_resolution_ledger" not in artifacts:
            artifacts["gmail_linkedin_resolution_ledger"] = str(child_ledger)
        if code == 20 or payload.get("status") == "blocked_approval":
            ledger["blocked"] = {"step_id": "gmail_linkedin_resolution", "child_ledger": str(child_ledger), "child": payload, "account_email": record.get("account_email") if isinstance(record, dict) else ""}
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
        result = dict(record)
        result.update({"payload": payload, "resolutions_csv": payload.get("output"), "ledger": str(child_ledger)})
        results.append(result)
        if payload.get("output"):
            by_slug[slug] = result
            artifacts.setdefault("gmail_linkedin_resolutions_csvs", []).append(result)
            if "gmail_linkedin_resolutions_csv" not in artifacts:
                artifacts["gmail_linkedin_resolutions_csv"] = payload.get("output")
            checkpoint = build_directory_checkpoint(input_cfg, artifacts, extra_sources=[Path(str(payload.get("output")))])
            artifacts["directory_csv"] = checkpoint["directory_csv"]
        if payload.get("prompts_jsonl"):
            artifacts.setdefault("gmail_linkedin_harness_prompts_jsonls", []).append(payload.get("prompts_jsonl"))
            artifacts.setdefault("gmail_linkedin_harness_prompts_jsonl", payload.get("prompts_jsonl"))
        if payload.get("instructions"):
            artifacts.setdefault("gmail_linkedin_harness_instructions", payload.get("instructions"))
    mark_step(ledger, "gmail_linkedin_resolution", "completed", payload={"results": results})
    emit_progress("Gmail LinkedIn resolution completed.")
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
        if not people_records:
            people_csvs = unique_strings(artifacts.get("gmail_final_people_csvs") or artifacts.get("gmail_people_csvs") or artifacts.get("gmail_people_csv"))
            people_records = [{"account_email": "", "people_csv": path, "slug": "all" if len(people_csvs) == 1 else f"account-{index}"} for index, path in enumerate(people_csvs)]
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
    if not raw_resolution_records and artifacts.get("gmail_linkedin_resolutions_csv"):
        raw_resolution_records.append({
            "account_email": "",
            "resolutions_csv": artifacts.get("gmail_linkedin_resolutions_csv"),
            "people_csv": artifacts.get("gmail_people_csv"),
            "slug": "all",
            "source": "provider",
        })
    resolution_records = combine_gmail_resolution_records(raw_resolution_records, Path(ledger["run_dir"]))
    if not resolution_records:
        mark_step(ledger, "gmail_apply_enrich", "skipped", reason="no gmail resolutions")
        return True
    resolution_records = ordered_records(resolution_records, unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")))
    by_slug = artifacts.setdefault("gmail_apply_enrich_by_slug", {})
    artifacts["gmail_resolved_people_csvs"] = []
    artifacts["gmail_enrich_people_ledgers"] = []
    artifacts["gmail_final_people_csvs"] = []
    artifacts["gmail_combined_resolutions_csvs"] = resolution_records
    begin_step(ledger_path, ledger, "gmail_apply_enrich", f"Applying Gmail LinkedIn matches for {len(resolution_records)} account file(s).")
    results = []
    final_people_csvs = []
    for index, record in enumerate(resolution_records):
        slug = source_slug(record.get("account_email") or record.get("slug") or f"account-{index}")
        existing = by_slug.get(slug) if isinstance(by_slug.get(slug), dict) else {}
        if existing.get("final_people_csv"):
            final_people_csvs.append(existing["final_people_csv"])
            artifacts["gmail_final_people_csvs"].append(existing["final_people_csv"])
            if existing.get("people_csv"):
                artifacts["gmail_resolved_people_csvs"].append(existing["people_csv"])
            if existing.get("enrich_ledger"):
                artifacts["gmail_enrich_people_ledgers"].append(existing["enrich_ledger"])
            results.append(existing)
            continue
        apply_cmd = py_cmd(
            "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py",
            "apply-resolutions",
            "--people-csv", str(record["people_csv"]),
            "--resolutions-csv", str(record["resolutions_csv"]),
            "--output-dir", str(DEFAULT_BASE_DIR),
            "--run-id", f"{ledger['run_id']}-gmail-resolved-{slug}",
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
        if int(payload.get("resolved") or 0) > 0:
            emit_progress(f"Enriching {payload.get('resolved')} resolved Gmail LinkedIn profiles for {record.get('account_email') or slug}.")
            child_ledger = Path(ledger["run_dir"]) / f"gmail-enrich-people.{slug}.ledger.json"
            enrich_cmd = py_cmd(
                "packs/ingestion/primitives/enrich_people/enrich_people.py",
                "run",
                "--input", str(resolved_people),
                "--ledger", str(child_ledger),
                "--run-id", f"{ledger['run_id']}-gmail-enrich-{slug}",
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
                mark_step(ledger, "gmail_apply_enrich", "failed", error=stderr or enrich_payload)
                ledger["status"] = "failed"
                save_ledger(ledger_path, ledger)
                emit({"status": "failed", "step_id": "gmail_apply_enrich", "error": stderr or enrich_payload})
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
    artifacts["gmail_final_people_csvs"] = final_people_csvs
    mark_step(ledger, "gmail_apply_enrich", "completed", payload={"results": results})
    emit_progress("Gmail LinkedIn matches applied and enrichment completed.")
    return True


def resolve_messages_review_csv(ledger: dict[str, Any]) -> str:
    input_cfg = ledger.get("input", {}) or {}
    artifacts = ledger.get("artifacts", {}) or {}
    review_csv = artifacts.get("messages_review_csv") or input_cfg.get("messages_review_csv") or ""
    default_review_csv = default_messages_review_csv()
    if not review_csv and input_cfg.get("include_existing_artifacts") and default_review_csv.exists():
        review_csv = str(default_review_csv)
    return str(review_csv or "")


def enrich_people_payload_from_ledger(child_ledger: Path) -> dict[str, Any]:
    child = read_json(child_ledger, {}) or {}
    if child.get("status") != "completed":
        return {}
    return {"status": "completed", "ledger": str(child_ledger), "run_dir": child.get("run_dir"), "artifacts": child.get("artifacts", {})}


def run_messages_enrichment(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    artifacts = ledger.setdefault("artifacts", {})
    review_csv_text = resolve_messages_review_csv(ledger)
    if not review_csv_text:
        mark_step(ledger, "messages_enrich_people", "skipped", reason="no messages research_review.csv")
        return True
    review_csv = Path(review_csv_text)
    artifacts["messages_review_csv"] = str(review_csv)
    run_dir = Path(ledger["run_dir"]) / "messages"
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

    child_ledger = Path(ledger["run_dir"]) / "messages-enrich-people.ledger.json"
    artifacts["messages_enrich_people_ledger"] = str(child_ledger)
    existing_payload = enrich_people_payload_from_ledger(child_ledger)
    if existing_payload:
        enrich_payload = existing_payload
        code = 0
        stderr = ""
    else:
        child_state = read_json(child_ledger, {}) or {}
        if child_ledger.exists() and child_state.get("status") not in {"completed", "failed"}:
            enrich_cmd = py_cmd("packs/ingestion/primitives/enrich_people/enrich_people.py", "continue", "--ledger", str(child_ledger))
        else:
            enrich_cmd = py_cmd(
                "packs/ingestion/primitives/enrich_people/enrich_people.py",
                "run",
                "--input", str(input_people),
                "--ledger", str(child_ledger),
                "--run-id", f"{ledger['run_id']}-messages-enrich",
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
    artifacts["messages_people_csv"] = enriched_people
    artifacts.setdefault("messages_people_csvs", [])
    if enriched_people and enriched_people not in artifacts["messages_people_csvs"]:
        artifacts["messages_people_csvs"].append(enriched_people)
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

    account_order = unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email"))
    gmail_inputs = artifacts.get("gmail_final_people_csvs") or []
    if not gmail_inputs and artifacts.get("gmail_people_records"):
        gmail_inputs = [record.get("people_csv") for record in ordered_records(artifacts["gmail_people_records"], account_order)]
    if not gmail_inputs:
        gmail_inputs = sorted(str(path) for path in artifacts.get("gmail_people_csvs", []) if path)

    explicit_inputs.extend(
        value for key, value in sorted(artifacts.items())
        if key in {"linkedin_people_csv"} and value
    )
    if gmail_inputs:
        explicit_inputs.extend(str(path) for path in gmail_inputs if path)
    elif artifacts.get("gmail_people_csv"):
        explicit_inputs.append(str(artifacts["gmail_people_csv"]))

    messages_people_inputs = artifacts.get("messages_people_csvs") or []
    if not messages_people_inputs and artifacts.get("messages_people_csv"):
        messages_people_inputs = [artifacts.get("messages_people_csv")]
    explicit_inputs.extend(str(path) for path in messages_people_inputs if path)

    messages_review = artifacts.get("messages_review_csv") or input_cfg.get("messages_review_csv")
    default_review_csv = default_messages_review_csv()
    if not messages_review and include_existing and default_review_csv.exists():
        messages_review = str(default_review_csv)
    if messages_review:
        scratch = merge_dir / "source-inputs" / "messages" / "contacts.csv"
        materialized = materialize_approved_messages_review(Path(messages_review), scratch)
        if materialized and materialized.get("contacts_csv"):
            explicit_inputs.append(str(scratch))

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
    merge_dir = Path(ledger["run_dir"]) / "merged"
    cmd = py_cmd(
        "packs/ingestion/primitives/merge_network_sources/merge_network_sources.py",
        "run",
        "--no-discover",
        "--base-dir", ".powerpacks",
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


def run_duckdb(ledger_path: Path, ledger: dict[str, Any]) -> bool:
    begin_step(ledger_path, ledger, "duckdb", "Building local network DuckDB.")
    merge_dir = Path(ledger["run_dir"]) / "merged"
    duckdb_dir = Path(ledger["run_dir"]) / "duckdb"
    cmd = py_cmd(
        "packs/ingestion/primitives/build_network_duckdb/build_network_duckdb.py",
        "--network-dir", str(merge_dir),
        "--output-dir", str(duckdb_dir),
        "--flavor", ledger["run_id"],
        "--force",
    )
    code, payload, stderr = run_cmd(cmd)
    if code != 0:
        mark_step(ledger, "duckdb", "failed", error=stderr or payload)
        ledger["status"] = "failed"
        save_ledger(ledger_path, ledger)
        emit({"status": "failed", "step_id": "duckdb", "error": stderr or payload})
        return False
    mark_step(ledger, "duckdb", "completed", payload=payload)
    ledger.setdefault("artifacts", {}).update({"duckdb": payload.get("duckdb"), "duckdb_manifest": payload.get("manifest")})
    emit_progress("Local network DuckDB is ready.")
    return True


def run_pipeline(ledger_path: Path, *, resume: bool = False) -> int:
    ledger = load_ledger(ledger_path)
    if not ledger.get("input", {}).get("fan_in_only") and ledger.get("steps", {}).get("source_imports", {}).get("status") not in {"completed", "skipped"}:
        if not run_source_import_workers(ledger_path, ledger, resume=resume):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if ledger.get("input", {}).get("only_sources") and not ledger.get("input", {}).get("fan_in_only"):
        if "messages" in set(unique_strings(ledger.get("input", {}).get("only_sources"))) and ledger.get("steps", {}).get("messages_enrich_people", {}).get("status") not in {"completed", "skipped"}:
            if not run_messages_enrichment(ledger_path, ledger):
                return 20 if ledger.get("blocked") else 1
            save_ledger(ledger_path, ledger)
        ledger["status"] = "source_import_completed"
        save_ledger(ledger_path, ledger)
        emit({"status": "source_import_completed", "ledger": str(ledger_path), "run_dir": ledger["run_dir"], "steps": ledger.get("steps", {}), "artifacts": ledger.get("artifacts", {})})
        return 0
    if ledger.get("steps", {}).get("gmail_directory", {}).get("status") not in {"completed", "skipped"}:
        if not run_gmail_directory(ledger_path, ledger):
            return 1
        save_ledger(ledger_path, ledger)
    if ledger.get("steps", {}).get("gmail_linkedin_resolution", {}).get("status") not in {"completed", "skipped"}:
        if not run_gmail_linkedin_resolution(ledger_path, ledger):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if ledger.get("steps", {}).get("gmail_apply_enrich", {}).get("status") not in {"completed", "skipped"}:
        if not run_gmail_apply_and_enrich(ledger_path, ledger):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if ledger.get("steps", {}).get("messages_enrich_people", {}).get("status") not in {"completed", "skipped"}:
        if not run_messages_enrichment(ledger_path, ledger):
            return 20 if ledger.get("blocked") else 1
        save_ledger(ledger_path, ledger)
    if not run_merge(ledger_path, ledger):
        return 1
    save_ledger(ledger_path, ledger)
    if not run_duckdb(ledger_path, ledger):
        return 1
    ledger["status"] = "completed"
    ledger.pop("blocked", None)
    save_ledger(ledger_path, ledger)
    emit({"status": "completed", "ledger": str(ledger_path), "run_dir": ledger["run_dir"], "artifacts": ledger.get("artifacts", {})})
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


def cmd_run(args: argparse.Namespace) -> int:
    args = apply_account_sources(args)
    run_id = args.run_id or f"network-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    run_dir = DEFAULT_BASE_DIR / "network-runs" / run_id
    ledger_path = Path(args.ledger)
    if args.dry_run or args.estimate:
        emit(dry_run_plan(args, ledger_path, run_id, run_dir))
        return 0
    existing = load_ledger(ledger_path) if ledger_path.exists() else {}
    if ledger_path.exists() and not args.force:
        if existing.get("status") == "completed":
            emit({
                "status": "completed",
                "cached": True,
                "ledger": str(ledger_path),
                "run_dir": existing.get("run_dir"),
                "message": "Existing completed import-network run found; no work was run.",
                "artifact_check": check_artifact_paths(existing),
                "artifacts": existing.get("artifacts", {}),
            })
            return 0
        if existing.get("status") not in {"failed"}:
            emit({"status": "active_run_exists", "ledger": str(ledger_path), "message": "Use continue/approve or --force."})
            return 0
    selected_sources = set(unique_strings(getattr(args, "only_source", [])))
    preserve_sources = set() if args.fan_in_only else selected_sources
    preserved = preserved_state_for_source_refresh(existing, preserve_sources) if args.force and (selected_sources or args.fan_in_only) else {}
    ledger = {
        "primitive": "import_network_pipeline",
        "version": 1,
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "run_id": run_id,
        "run_dir": str(run_dir),
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


def dry_run_plan(args: argparse.Namespace, ledger_path: Path, run_id: str, run_dir: Path) -> dict[str, Any]:
    args = apply_account_sources(args)
    if ledger_path.exists():
        ledger = load_ledger(ledger_path)
        steps = ledger.get("steps", {}) or {}
        if ledger.get("status") == "completed":
            would_run = []
        else:
            would_run = [
                step for step in ["linkedin", "gmail_msgvault", "gmail_directory", "gmail_linkedin_resolution", "gmail_apply_enrich", "messages_enrich_people", "merge", "duckdb"]
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
            "run_id": ledger.get("run_id") or run_id,
            "run_dir": ledger.get("run_dir") or str(run_dir),
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
    if not messages_review_csv and getattr(args, "include_existing_artifacts", False) and default_messages_review_csv().exists():
        messages_review_csv = str(default_messages_review_csv())
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
    if args.gmail_account_email or unique_strings(getattr(args, "gmail_account_emails", [])) or resolve_msgvault_db(args):
        would_run.append("gmail_msgvault")
        would_run.append("gmail_directory")
    if getattr(args, "gmail_linkedin_provider", "off") != "off":
        would_run.append("gmail_linkedin_resolution")
    if getattr(args, "gmail_resolutions_csv", ""):
        would_run.append("gmail_apply_enrich")
    if messages_review_csv:
        would_run.append("messages_enrich_people")
    would_run.extend(["merge", "duckdb"])
    return {
        "status": "dry_run",
        "ledger": str(ledger_path),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "existing_status": "missing",
        "would_run_steps": would_run,
        "worker_groups": {"import": source_worker_group(input_cfg, run_id)},
        "gmail_api_estimates": gmail_estimates,
        "gmail_estimate_summary": summarize_gmail_estimates(gmail_estimates) if gmail_estimates else "",
        "estimated_paid_calls": "unknown_without_existing_stage_outputs",
        "message": "No existing import-network ledger was found; running would execute the listed stages until any child approval confirmation.",
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
        "run_dir": ledger.get("run_dir"),
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
    run.add_argument("--run-id")
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
    run.add_argument("--gmail-linkedin-provider", choices=["off", "harness", "parallel"], default="off", help="Prepare/run Gmail email-to-LinkedIn resolution before merge. harness is local prompt prep; parallel is spend-bearing and requires approval.")
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
    run.add_argument("--fan-in-only", action="store_true", help="Skip source import workers and run merge/DuckDB fan-in from existing artifacts")
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
