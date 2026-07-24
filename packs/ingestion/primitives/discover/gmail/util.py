"""Gmail discovery utilities: tolerant parsers, row merge, incremental plan.

Changelog:
  2026-07-24 (merge policy): gmail_discovery_merge_plan takes the caller's
    observed output state as keyword-only args (`output_rows`,
    `full_rerun_requested`) and checks two new branches FIRST — `empty_output`
    (no populated contacts.csv to append to) and `full_rerun_requested`
    (--fresh). The function stays pure; the caller owns the filesystem read.
  2026-07-23 (rename): `discover_engine_base_dir` renamed `extract_gmail_base_dir`
    (the extractor it points at was renamed discover_engine.py -> extract_gmail.py).
  2026-07-23 (audit):
    - Helpers split out of the former single-file gmail.py.
  2026-07-23 (audit batch 17): network_import_base_dir renamed to
    discover_engine_base_dir (the child it feeds was renamed
    network_import.py -> discover_engine.py).
  2026-07-23 (account-email selection): resolve_discovery_inputs no longer reads
    accounts.json — the account selection IS the caller's account_emails list
    (empty means no accounts selected). Dropped the inputs()/accounts.json state
    reader, GmailDiscoveryInputs.accounts_file, and the selected_accounts field
    (renamed account_emails). Precedence is now explicit override >
    discovery.config default for msgvault_db and sync_query. _same_selected_accounts
    -> _same_account_emails and the merge-plan reason selected_accounts_changed ->
    account_emails_changed.
"""

from __future__ import annotations

from dataclasses import dataclass

from pathlib import Path
from typing import Any
import hashlib
import json
import sys


# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR, DEFAULT_MSGVAULT_DB  # noqa: E402
from packs.ingestion.primitives.discover.common import (  # noqa: E402
    GMAIL_INTERACTION_CALCULATION_VERSION,
    ordered_unique,
)
from packs.ingestion.schemas.people_schema import parse_jsonish  # noqa: E402
from packs.ingestion.primitives.discover.discovery_config import (  # noqa: E402
    source_config,
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


def _same_account_emails(left: Any, right: list[str]) -> bool:
    return sorted(_as_list(left)) == sorted(_as_list(right))


def gmail_discovery_merge_plan(
    existing_manifest: dict[str, Any],
    account_emails: list[str],
    child_modes: list[str],
    *,
    output_rows: int,
    full_rerun_requested: bool = False,
) -> dict[str, str]:
    """Decide how child outputs combine with the stage output already on disk.

    Pure — every input is passed in, so the caller owns all filesystem reads.
    `output_rows` is the row count of the existing contacts.csv (0 when the file
    is missing or header-only); `full_rerun_requested` is the caller's explicit
    rescan request (`--fresh`).

    Order, first match wins:
      empty_output          nothing on disk to append to, so there is no
                            baseline to preserve -> rebuild from the children.
                            This is also what keeps the append path from
                            replaying a delta onto an absent baseline and
                            double-counting it (_merge_rows SUMS the counts).
      full_rerun_requested  the caller asked for a full rescan; honor it over
                            any incremental opportunity.
      calculation_version_changed / account_emails_changed
                            the rows on disk were computed under different rules
                            or for a different account set -> they cannot be
                            appended to.
      children_returned_incremental_deltas
                            EVERY child returned new-only rows -> keep the
                            existing rows and append the unapplied deltas.
      children_returned_full_recounts
                            otherwise; a child's rows restate its whole truth,
                            so the children alone are the new output.
    """
    if output_rows <= 0:
        return {"mode": "full_rewrite", "reason": "empty_output"}
    if full_rerun_requested:
        return {"mode": "full_rewrite", "reason": "full_rerun_requested"}
    if existing_manifest.get("calculation_version") != GMAIL_INTERACTION_CALCULATION_VERSION:
        return {"mode": "full_rewrite", "reason": "calculation_version_changed"}
    if not _same_account_emails(existing_manifest.get("account_emails"), account_emails):
        return {"mode": "full_rewrite", "reason": "account_emails_changed"}
    if child_modes and all(mode == GMAIL_CALCULATION_INCREMENTAL_DELTA for mode in child_modes):
        return {"mode": "incremental_update", "reason": "children_returned_incremental_deltas"}
    return {"mode": "full_rewrite", "reason": "children_returned_full_recounts"}


def extract_gmail_base_dir(contacts_csv: Path) -> Path:
    """Return the base dir expected by gmail/extract_gmail.py --output-dir."""
    gmail_dir = contacts_csv.parent
    if gmail_dir.name == "gmail" and gmail_dir.parent.name == "discover":
        return gmail_dir.parent.parent
    return DEFAULT_BASE_DIR


@dataclass(frozen=True)
class GmailDiscoveryInputs:
    """THE resolved gmail-discovery configuration — GmailDiscovery reads this and
    nothing else. Built only by resolve_discovery_inputs, which owns the one
    precedence rule for the whole vertical. account_emails IS the selection
    (empty tuple = no accounts selected); there is no accounts.json fallback."""

    account_emails: tuple[str, ...]
    msgvault_db: str
    sync_query: str


def resolve_discovery_inputs(
    *,
    account_emails: list[str] | None = None,
    msgvault_db: str | None = None,
    sync_query: str | None = None,
) -> GmailDiscoveryInputs:
    """The ONE configuration resolution point for gmail discovery.

    Precedence, highest first:
      1. explicit caller/CLI override (the keyword args here)
      2. discovery.config.json defaults (msgvault db default, sync query)
    The account_emails list IS the selection — there is no accounts.json fallback,
    so an empty/None list resolves to no accounts selected. Only msgvault_db and
    sync_query have a config-default layer beneath the explicit override; callers
    never merge config themselves — they pass overrides and read the frozen result."""
    input_cfg = source_config("gmail")["inputs"]
    resolved_accounts = ordered_unique(account_emails or [])
    config_db = str(input_cfg.get("msgvault_db_default") or DEFAULT_MSGVAULT_DB)
    resolved_db = str(Path(str(msgvault_db) if msgvault_db else config_db).expanduser())
    resolved_query = (str(sync_query or "").strip() if sync_query is not None
                      else str(input_cfg.get("sync_query") or "").strip())
    return GmailDiscoveryInputs(
        account_emails=tuple(resolved_accounts),
        msgvault_db=resolved_db,
        sync_query=resolved_query,
    )
