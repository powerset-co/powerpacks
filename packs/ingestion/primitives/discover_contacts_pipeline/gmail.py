#!/usr/bin/env python3
"""Discover Gmail contacts from existing msgvault metadata."""

from __future__ import annotations

import argparse
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
        "command": cmd,
        "code": code,
        "payload": payload,
        "stderr": stderr,
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

    incoming: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    raw_root = contacts_csv.parent / "raw"
    for email in source_inputs["selected_accounts"]:
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
            str(raw_root),
        )
        code, child, stderr = run_cmd(cmd)
        children.append({"account_email": email, "sync": sync, "code": code, "payload": child, "stderr": stderr})
        if code != 0:
            payload = {"status": "failed", "source": "gmail", "account_email": email, "error": stderr or child}
            write_json(manifest_json, payload)
            return payload
        child_queue = Path(str((child.get("artifacts") or {}).get("linkedin_resolution_queue_csv") or ""))
        if child_queue.exists():
            _fields, rows = read_csv_rows(child_queue)
            incoming.extend(rows)

    existing: list[dict[str, Any]] = []
    if contacts_csv.exists():
        _fields, existing = read_csv_rows(contacts_csv)
    merged = _merge_rows([*existing, *incoming])
    write_csv_rows(contacts_csv, GMAIL_DISCOVERY_COLUMNS, merged)
    write_csv_rows(queue_csv, GMAIL_DISCOVERY_COLUMNS, merged)
    payload = {
        "status": "completed",
        "source": "gmail",
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
