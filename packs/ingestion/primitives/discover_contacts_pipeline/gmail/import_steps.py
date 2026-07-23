"""Gmail import step functions for the LIVE import chain.

Contains ONLY `run_gmail_directory` and `run_gmail_apply_and_enrich` (which
applies STORED resolutions — no Parallel calls, no RapidAPI hydration;
deep-context owns resolution/enrichment, and `bin/deep-context migrate-legacy`
adopts the stored era into overrides/review.csv), plus `save_ledger` and
`materialize_gmail_merged_people_csv`, with their transitive helpers. Loaded via
`import_contacts_pipeline.common.load_gmail_import_steps`.

Changelog:
  2026-07-23 (audit):
    - Extracted from the retired pre-split orchestrator (now deleted).
    - Narrowed to the surviving steps when the legacy resolve/enrich flags
      were removed.
    - union_alias_list stopped alias loss: the resolved Gmail address used to
      be discarded when two rows collapsed onto one public_identifier; alias
      lists now set-union instead.
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.schemas.people_schema import (  # noqa: E402
    LIST_VALUE_COLUMNS,
    PEOPLE_SCHEMA_COLUMNS,
    extract_public_identifier,
    merge_interaction_counts,
    normalize_linkedin_url,
    normalize_people_row,
)
from packs.shared.csv_io import CsvIO  # noqa: E402


DEFAULT_BASE_DIR = Path(".powerpacks/network-import")


DEFAULT_DISCOVER_DIR = DEFAULT_BASE_DIR / "discover"


DEFAULT_DIRECTORY_CSV = DEFAULT_BASE_DIR / "directory.csv"


DEFAULT_CHILD_TIMEOUT_SECONDS = int(os.environ.get("POWERPACKS_IMPORT_NETWORK_CHILD_TIMEOUT_SECONDS", str(6 * 60 * 60)))


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


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def artifact_dir_from_ledger(ledger: dict[str, Any]) -> Path:
    return Path(str(ledger.get("artifact_dir") or ledger.get("run_dir") or DEFAULT_DISCOVER_DIR))


def emit_progress(message: str) -> None:
    print(f"[discover-contacts] {message}", file=sys.stderr, flush=True)


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
    here rather than overwrite each other when rows collapse onto one
    public_identifier. The matching primary_email/primary_phone values are
    folded in so a single-email row that only populated primary_* still
    contributes its address to the union.
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


def ordered_records(records: list[dict[str, Any]], account_order: list[str] | None = None) -> list[dict[str, Any]]:
    order = {email: index for index, email in enumerate(account_order or []) if email}
    return sorted(
        records,
        key=lambda record: (
            order.get(str(record.get("account_email") or ""), len(order)),
            str(record.get("account_email") or record.get("slug") or record.get("people_csv") or record.get("queue_csv") or ""),
        ),
    )


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


def py_cmd(script: str, *args: str) -> list[str]:
    return [sys.executable, script, *args]


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


