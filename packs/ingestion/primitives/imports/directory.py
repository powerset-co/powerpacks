#!/usr/bin/env python3
"""Shared directory persistence and pure people-row merge helpers.

Deep Context persists reviewed identities here; the shared fan-in reads them.
Source imports reuse metadata unions without matching or updating the directory.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import sys  # noqa: F401

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.schemas.people_schema import (  # noqa: E402
    extract_public_identifier,
    normalize_linkedin_url,
    parse_jsonish,
)

from packs.ingestion.primitives.common.contact_fields import (  # noqa: E402
    normalize_name_key,
    normalize_phone,
)
from packs.ingestion.primitives.discover.common import read_csv_rows, write_csv_rows  # noqa: E402
from packs.ingestion.primitives.pipeline.contract import row_model_for  # noqa: E402

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
# The declared row shape of `directory.csv`, generated FROM DIRECTORY_COLUMNS so
# field order stays the on-disk header order and the column list keeps one home.
# `_priority` is deliberately absent: normalized_directory_row carries it as an
# in-memory ranking hint and merge_directory_rows drops it before writing.
DirectoryRow = row_model_for("DirectoryRow", DIRECTORY_COLUMNS)

# The row SLICE each source's writer owns, as declared by `Artifact.owns_rows_where`
# (declaration only — the graph checker compares these strings, never evaluates
# them). directory.csv is a cross-source aggregate: every writer writes every
# column, of its own source's rows only, so columns are the wrong ownership axis.
DEEP_CONTEXT_DIRECTORY_ROWS = "source == 'deep_context_review'"


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


def directory_source_priority(source: str, url_col: str) -> int:
    if source == "deep_context_review":
        return 100
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

    Distinct work emails that resolve to the same LinkedIn person accumulate
    here rather than overwriting each other. The matching
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
