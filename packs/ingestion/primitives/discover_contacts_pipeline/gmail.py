#!/usr/bin/env python3
"""Discover Gmail contacts from existing msgvault metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_MSGVAULT_DB,
        emit,
        now_iso,
        ordered_unique,
        parse_jsonish,
        py_cmd,
        read_csv_rows,
        read_json,
        run_cmd,
        source_slug,
        write_csv_rows,
        write_json,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.discovery_config import (
        accounts_path as configured_accounts_path,
        load_config,
        output_path,
        source_config,
        state_value,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_MSGVAULT_DB,
        emit,
        now_iso,
        ordered_unique,
        parse_jsonish,
        py_cmd,
        read_csv_rows,
        read_json,
        run_cmd,
        source_slug,
        write_csv_rows,
        write_json,
    )
    from packs.ingestion.primitives.discover_contacts_pipeline.discovery_config import (
        accounts_path as configured_accounts_path,
        load_config,
        output_path,
        source_config,
        state_value,
    )

GMAIL_DISCOVERY_COLUMNS = [
    "handle",
    "id",
    "account_emails",
    "source_ids",
    "display_name",
    "full_name",
    "primary_email",
    "company_guess",
    "primary_email_type",
    "total_messages",
    "thread_count",
    "last_interaction",
    "source",
    "source_channels",
]
DEFAULT_GMAIL_ESTIMATE_MAX_PAGES = 4
GMAIL_INTERACTION_CALCULATION_VERSION = "msgvault-interactions-v2"
GMAIL_CALCULATION_FULL_RECOUNT = "full_recount"
GMAIL_CALCULATION_INCREMENTAL_DELTA = "incremental_delta"


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return ordered_unique(value)
    text = str(value or "").strip()
    return [text] if text else []


def _json_list(value: Any) -> list[str]:
    parsed = parse_jsonish(value, [])
    return _as_list(parsed) if isinstance(parsed, list) else _as_list(value)


def _int_value(value: Any) -> int:
    try:
        return int(float(str(value or "0")))
    except ValueError:
        return 0


def _merge_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    keyed: dict[str, dict[str, Any]] = {}
    for row in rows:
        email = str(row.get("primary_email") or row.get("handle") or "").strip().lower()
        if not email:
            continue
        existing = keyed.get(email)
        if existing is None:
            item = {field: str(row.get(field) or "") for field in GMAIL_DISCOVERY_COLUMNS}
            item["handle"] = email
            item["primary_email"] = email
            item["account_emails"] = json.dumps(_json_list(row.get("account_emails")), ensure_ascii=False)
            item["source_ids"] = json.dumps(_json_list(row.get("source_ids")), ensure_ascii=False)
            keyed[email] = item
            continue
        for field in ("display_name", "full_name", "company_guess", "primary_email_type", "source", "source_channels"):
            if row.get(field) and not existing.get(field):
                existing[field] = str(row[field])
        for field in ("total_messages", "thread_count"):
            existing[field] = str(_int_value(existing.get(field)) + _int_value(row.get(field)))
        if str(row.get("last_interaction") or "") > str(existing.get("last_interaction") or ""):
            existing["last_interaction"] = str(row.get("last_interaction") or "")
        existing["account_emails"] = json.dumps(
            ordered_unique(_json_list(existing.get("account_emails")) + _json_list(row.get("account_emails"))),
            ensure_ascii=False,
        )
        existing["source_ids"] = json.dumps(
            ordered_unique(_json_list(existing.get("source_ids")) + _json_list(row.get("source_ids"))),
            ensure_ascii=False,
        )
    return [{field: str(row.get(field) or "") for field in GMAIL_DISCOVERY_COLUMNS} for _, row in sorted(keyed.items())]


def gmail_incremental_input_id(account_email: str, rows: list[dict[str, Any]]) -> str:
    """Return a stable manifest key for an incremental child output.

    Incremental rows are additive, so replaying the same child output must not
    be merged twice. This key is derived from the account and normalized child
    CSV rows already produced by the command; it does not create any directories
    or require a separate batch concept.
    """
    normalized_rows = [
        {field: str(row.get(field) or "") for field in GMAIL_DISCOVERY_COLUMNS}
        for row in rows
    ]
    payload = {
        "account_email": str(account_email or "").strip().lower(),
        "calculation_version": GMAIL_INTERACTION_CALCULATION_VERSION,
        "rows": sorted(normalized_rows, key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False)),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _same_selected_accounts(left: Any, right: list[str]) -> bool:
    return sorted(_as_list(left)) == sorted(_as_list(right))


def gmail_discovery_merge_plan(existing_manifest: dict[str, Any], selected_accounts: list[str], child_modes: list[str]) -> dict[str, str]:
    if existing_manifest.get("calculation_version") != GMAIL_INTERACTION_CALCULATION_VERSION:
        return {"mode": "full_rewrite", "reason": "calculation_version_changed"}
    if not _same_selected_accounts(existing_manifest.get("selected_accounts"), selected_accounts):
        return {"mode": "full_rewrite", "reason": "selected_accounts_changed"}
    if child_modes and all(mode == GMAIL_CALCULATION_INCREMENTAL_DELTA for mode in child_modes):
        return {"mode": "incremental_update", "reason": "children_returned_incremental_deltas"}
    return {"mode": "full_rewrite", "reason": "children_returned_full_recounts"}


def inputs(accounts: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    gmail_cfg = source_config("gmail")
    input_cfg = gmail_cfg["inputs"]
    selected = state_value(accounts, input_cfg["selected_accounts_state_key"], [])
    msgvault_db = state_value(accounts, input_cfg["msgvault_db_state_key"], "") or input_cfg.get("msgvault_db_default") or str(DEFAULT_MSGVAULT_DB)
    return {
        "selected_accounts": _as_list(selected),
        "msgvault_db": str(Path(str(msgvault_db)).expanduser()),
        "sync_query": str(input_cfg.get("sync_query") or "").strip(),
    }


def parse_msgvault_sync_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if len(text) >= 10 and text[:10].count("-") == 2:
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


def sync_msgvault_account(email: str, db: str, query: str) -> dict[str, Any]:
    inferred = infer_msgvault_sync_after(db, email)
    sync_after = inferred.get("sync_after", "")
    sync_after_source = inferred.get("source", "")
    if not shutil.which("msgvault"):
        return {
            "status": "skipped",
            "reason": "msgvault_command_not_found",
            "account_email": email,
            "sync_after": sync_after,
            "sync_after_source": sync_after_source,
            "query": query,
        }
    cmd = ["msgvault"]
    db_home = Path(db).expanduser().parent
    default_home = Path(DEFAULT_MSGVAULT_DB).expanduser().parent
    if db_home != default_home:
        cmd.extend(["--home", str(db_home)])
    cmd.extend(["sync-full", email])
    if sync_after:
        cmd.extend(["--after", sync_after])
    if query:
        cmd.extend(["--query", query])
    code, payload, stderr = run_cmd(cmd)
    return {
        "status": "completed" if code == 0 else "failed",
        "account_email": email,
        "code": code,
        "messages_added": payload.get("messages_added") if isinstance(payload, dict) else "",
        "error": stderr or payload if code != 0 else "",
        "sync_after": sync_after,
        "sync_after_source": sync_after_source,
        "query": query,
    }


def normalize_label_names(labels: Any) -> list[str]:
    if isinstance(labels, str):
        labels = [labels]
    if not isinstance(labels, list):
        return []
    return ordered_unique([str(label).strip() for label in labels if str(label or "").strip()])


def gmail_sync_query(input_cfg: dict[str, Any]) -> str:
    explicit = str(input_cfg.get("gmail_sync_query") or "").strip()
    if explicit:
        return explicit
    return str(source_config("gmail")["inputs"].get("sync_query") or "").strip()


def gmail_sync_after(input_cfg: dict[str, Any]) -> str:
    return parse_msgvault_sync_date(input_cfg.get("gmail_sync_after"))


def gmail_excluded_labels(input_cfg: dict[str, Any]) -> list[str]:
    if input_cfg.get("include_category_mail"):
        return []
    labels = input_cfg.get("gmail_exclude_labels")
    if labels:
        return normalize_label_names(labels)
    return ["CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_FORUMS", "CATEGORY_UPDATES"]


def estimate_gmail_accounts_via_api(_input_cfg: dict[str, Any], _emails: list[str]) -> list[dict[str, Any]]:
    return []


def summarize_gmail_estimates(estimates: list[dict[str, Any]]) -> dict[str, Any]:
    return {"accounts": len(estimates), "estimated_messages": 0}


def run_gmail_directory(_ledger_path: Path, _ledger: dict[str, Any]) -> bool:
    return True


def run_gmail_linkedin_resolution(_ledger_path: Path, _ledger: dict[str, Any]) -> bool:
    return True


def run_gmail_apply_and_enrich(_ledger_path: Path, _ledger: dict[str, Any]) -> bool:
    return True


def run_gmail_msgvault(ledger_path: Path, ledger: dict[str, Any], _worker: dict[str, Any]) -> bool:
    payload = discover(accounts_file=Path(str((ledger.get("input") or {}).get("from_accounts") or ".powerpacks/ingestion/accounts.json")))
    ledger.setdefault("artifacts", {})["gmail_contacts_csv"] = payload.get("contacts_csv", "")
    ledger.setdefault("artifacts", {})["gmail_linkedin_resolution_queue_csv"] = payload.get("linkedin_resolution_queue_csv", "")
    return payload.get("status") in {"completed", "skipped"}


def discover(*, accounts_file: Path | None = None, accounts_path: Path | None = None, **_: Any) -> dict[str, Any]:
    cfg = load_config()
    accounts_file = accounts_file or accounts_path or configured_accounts_path()
    account_state = read_json(accounts_file, {}) or {}
    source_inputs = inputs(account_state, cfg)
    contacts_csv = output_path("gmail", "contacts_csv")
    queue_csv = output_path("gmail", "linkedin_resolution_queue_csv")
    manifest_json = output_path("gmail", "manifest_json")
    contacts_csv.parent.mkdir(parents=True, exist_ok=True)

    if not source_inputs["selected_accounts"]:
        payload = {
            "status": "skipped",
            "source": "gmail",
            "reason": "no_selected_accounts",
            "contacts_csv": str(contacts_csv),
            "linkedin_resolution_queue_csv": str(queue_csv),
        }
        write_json(manifest_json, payload)
        return payload

    incoming_outputs: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    child_modes: list[str] = []
    raw_root = contacts_csv.parent / "raw"
    for email in source_inputs["selected_accounts"]:
        account_raw_dir = raw_root / source_slug(email)
        sync = sync_msgvault_account(email, source_inputs["msgvault_db"], source_inputs["sync_query"])
        if sync["status"] == "failed":
            payload = {"status": "failed", "source": "gmail", "account_email": email, "error": sync}
            write_json(manifest_json, payload)
            return payload
        cmd = py_cmd(
            "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py",
            "msgvault",
            "--db",
            source_inputs["msgvault_db"],
            "--account-email",
            email,
            "--output-dir",
            str(account_raw_dir),
        )
        code, child, stderr = run_cmd(cmd)
        child_mode = str(child.get("calculation_mode") or child.get("counts", {}).get("calculation_mode") or GMAIL_CALCULATION_FULL_RECOUNT) if isinstance(child, dict) else GMAIL_CALCULATION_FULL_RECOUNT
        child_modes.append(child_mode)
        child_artifacts = child.get("artifacts") if isinstance(child, dict) else {}
        child_queue_text = str((child_artifacts or {}).get("linkedin_resolution_queue_csv") or "").strip()
        child_queue = Path(child_queue_text) if child_queue_text else None
        rows_written = 0
        rows: list[dict[str, Any]] = []
        if child_queue and child_queue.is_file():
            _fields, rows = read_csv_rows(child_queue)
            rows_written = len(rows)
        incremental_input_id = gmail_incremental_input_id(email, rows)
        incoming_outputs.append({
            "account_email": email,
            "calculation_mode": child_mode,
            "incremental_input_id": incremental_input_id,
            "rows": rows,
        })
        children.append({
            "account_email": email,
            "sync": sync,
            "code": code,
            "status": child.get("status") if isinstance(child, dict) else "",
            "contacts": child.get("contacts") or child.get("counts", {}).get("contacts_written", "") if isinstance(child, dict) else "",
            "calculation_mode": child_mode,
            "incremental_input_id": incremental_input_id if child_mode == GMAIL_CALCULATION_INCREMENTAL_DELTA else "",
            "rows_read": rows_written,
            "raw_dir": str(account_raw_dir),
        })
        if code != 0:
            payload = {"status": "failed", "source": "gmail", "account_email": email, "error": stderr or child}
            write_json(manifest_json, payload)
            return payload

    existing_manifest = read_json(manifest_json, {}) or {}
    merge_plan = gmail_discovery_merge_plan(existing_manifest, source_inputs["selected_accounts"], child_modes)
    existing: list[dict[str, Any]] = []
    incoming: list[dict[str, Any]] = []
    applied_incremental_inputs = _as_list(existing_manifest.get("applied_incremental_inputs"))
    # Backward-compatible read for manifests written by earlier PR drafts.
    if not applied_incremental_inputs:
        applied_incremental_inputs = _as_list(existing_manifest.get("applied_incremental_batches"))
    applied_incremental_input_set = set(applied_incremental_inputs)
    skipped_incremental_inputs: list[str] = []
    incremental_outputs = [output for output in incoming_outputs if output.get("calculation_mode") == GMAIL_CALCULATION_INCREMENTAL_DELTA]
    if merge_plan["mode"] != "incremental_update" and incremental_outputs:
        payload = {
            "status": "failed",
            "source": "gmail",
            "calculation_version": GMAIL_INTERACTION_CALCULATION_VERSION,
            "calculation_mode": merge_plan["mode"],
            "calculation_reason": "full_rewrite_requires_full_recount_children",
            "selected_accounts": source_inputs["selected_accounts"],
            "child_calculation_modes": child_modes,
            "children": children,
        }
        write_json(manifest_json, payload)
        return payload
    if merge_plan["mode"] == "incremental_update" and contacts_csv.exists():
        _fields, existing = read_csv_rows(contacts_csv)
        for output in incoming_outputs:
            input_id = str(output.get("incremental_input_id") or "")
            if input_id and input_id in applied_incremental_input_set:
                skipped_incremental_inputs.append(input_id)
                continue
            incoming.extend(output.get("rows") or [])
            if input_id:
                applied_incremental_inputs.append(input_id)
                applied_incremental_input_set.add(input_id)
    else:
        for output in incoming_outputs:
            incoming.extend(output.get("rows") or [])
    merged = _merge_rows([*existing, *incoming])
    write_csv_rows(contacts_csv, GMAIL_DISCOVERY_COLUMNS, merged)
    write_csv_rows(queue_csv, GMAIL_DISCOVERY_COLUMNS, merged)
    payload = {
        "status": "completed",
        "source": "gmail",
        "calculation_version": GMAIL_INTERACTION_CALCULATION_VERSION,
        "calculation_mode": merge_plan["mode"],
        "calculation_reason": merge_plan["reason"],
        "child_calculation_modes": child_modes,
        "applied_incremental_inputs": applied_incremental_inputs,
        "skipped_incremental_inputs": skipped_incremental_inputs,
        "contacts_csv": str(contacts_csv),
        "linkedin_resolution_queue_csv": str(queue_csv),
        "contacts": len(merged),
        "selected_accounts": source_inputs["selected_accounts"],
        "msgvault_db": source_inputs["msgvault_db"],
        "updated_at": now_iso(),
        "privacy": {
            "message_bodies_read": False,
            "gmail_sync_ran": True,
            "parallel_called": False,
            "rapidapi_called": False
        },
        "children": children,
    }
    write_json(manifest_json, payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover Gmail contacts from existing msgvault metadata")
    parser.add_argument("command", choices=["discover"])
    parser.add_argument("--accounts", type=Path, default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    emit(discover(accounts_file=args.accounts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
