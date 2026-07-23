"""Gmail discovery utilities: tolerant parsers, row merge, incremental plan."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import json
import sys


# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover_contacts_pipeline.common import (  # noqa: E402
    DEFAULT_BASE_DIR,
    DEFAULT_MSGVAULT_DB,
    GMAIL_INTERACTION_CALCULATION_VERSION,
    ordered_unique,
    parse_jsonish,
)
from packs.ingestion.primitives.discover_contacts_pipeline.discovery_config import (  # noqa: E402
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


def gmail_network_import_base_dir(contacts_csv: Path) -> Path:
    """Return the base dir expected by gmail_network_import.py --output-dir."""
    gmail_dir = contacts_csv.parent
    if gmail_dir.name == "gmail" and gmail_dir.parent.name == "discover":
        return gmail_dir.parent.parent
    return DEFAULT_BASE_DIR


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


