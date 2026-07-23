#!/usr/bin/env python3
"""Cross-source `directory.csv` and people.csv materialization helpers.

Owns the shared, cross-source `directory.csv` aggregate contract
(`DIRECTORY_COLUMNS`), its email/phone/name normalization and identity keys,
row merge, and the people.csv → directory commit path. This is import-stage
code: it has no discover-stage consumers — only `imports/common.py`,
`imports/messages/importer.py`, and tests import it.

Changelog:
  2026-07-23 (audit batch 21): relocated discover/directory.py → imports/directory.py.
    It had zero discover-stage callers; all consumers are import-stage. No
    functions were split into a vertical: the gmail-named helpers
    (gmail_account_from_source_key, gmail_directory_source_key) are reached only
    through the cross-source core (normalized_directory_row /
    people_directory_source_key), and directory_rows_from_resolutions/_candidates
    are reached only through the generic build_directory_checkpoint — none are
    single-vertical, so all stay here. directory.csv is a cross-source aggregate;
    its schema is not duplicated per vertical.
  2026-07-23 (audit): union_alias_list replaced overwrite semantics for
    all_emails/all_phones — previously the resolved Gmail address was
    discarded when two rows collapsed onto one public_identifier; aliases
    now accumulate as an order-preserving set union.
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
    LIST_VALUE_COLUMNS,
    PEOPLE_SCHEMA_COLUMNS,
    extract_public_identifier,
    latest_interaction,
    merge_interaction_counts,
    normalize_linkedin_url,
    normalize_people_row,
)

from packs.ingestion.primitives.discover.common import (
    DEFAULT_BASE_DIR,
    DEFAULT_DIRECTORY_CSV,
    now_iso,
    parse_jsonish,
    read_csv_rows,
    unique_strings,
    write_csv_rows,
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
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


def gmail_directory_source_key(account: str, email: str) -> str:
    account_key = (account or "").strip().lower()
    email_key = (email or "").strip().lower()
    return f"gmail:{account_key}:email:{email_key}"


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
                    current[col] = latest_interaction(current.get(col, ""), row.get(col, ""))
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
