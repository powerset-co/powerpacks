"""Gmail discovery utilities: tolerant parsers, row merge, merge plan.

Changelog:
  2026-09-23 (typed rows): the prior manifest is parsed once into
    `GmailManifestResume` (the ONE reader of its resume fields), so
    `gmail_discovery_merge_plan` compares attributes instead of probing the dict;
    `resolve_discovery_inputs` reads the typed `SourceConfig` that
    `discovery_config.source_config` now returns. `_merge_rows` takes the declared
    row model (the caller validates the on-disk queue rows into it) and reads
    `to_row()`, so it no longer probes a row dict either.
  2026-09-23 (simplification audit): the de-dup calls now use
    `common.jsonio.unique_strings`, the single home for order-preserving de-dup;
    the byte-identical local `ordered_unique` copy was deleted.
  2026-07-24 (incremental deleted): the append-only delta machinery is gone.
    `aggregate_contacts` takes no date floor, so extract_gmail re-derives the
    whole archive's totals on every run and permanently declares full_recount
    (#334); no producer ever emitted `incremental_delta`, and the owner has
    decided against building real incrementality. DELETED
    GMAIL_CALCULATION_INCREMENTAL_DELTA, gmail_incremental_input_id (the
    replay-dedup content hash) and the `children_returned_incremental_deltas`
    branch, which also retired gmail_discovery_merge_plan's `child_modes`
    parameter. Every branch now returns full_rewrite; the `mode` field is kept
    constant so the stage manifest's calculation_mode/calculation_reason keys are
    unchanged. #334's `empty_output` / `full_rerun_requested` branches stay.
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

from dataclasses import dataclass, field

from pathlib import Path
from typing import Any, Iterable
import json
import sys


# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import unique_strings  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR, DEFAULT_MSGVAULT_DB  # noqa: E402
from packs.ingestion.primitives.discover.common import GMAIL_INTERACTION_CALCULATION_VERSION  # noqa: E402
from packs.ingestion.primitives.pipeline.contract import RowModel  # noqa: E402
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


# The one calculation mode extract_gmail can honestly declare: aggregate_contacts
# takes no date floor, so its rows always restate the account's whole truth.
GMAIL_CALCULATION_FULL_RECOUNT = "full_recount"


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return unique_strings(value)
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


def _merge_rows(rows: Iterable[RowModel]) -> list[dict[str, str]]:
    """Merge the children's queue rows by primary email into the stage queue rows.

    `rows` are the declared `models.GmailContactRow` instances the caller validated
    the on-disk queue rows into (typing that class by name here would be an import
    cycle, so the parameter is the generic `RowModel`). Reads `to_row()`, so every
    declared column is present and no field is probed by name. Returns CSV rows."""
    keyed: dict[str, dict[str, str]] = {}
    for row in rows:
        fields = row.to_row()
        email = str(fields["primary_email"] or fields["handle"] or "").strip().lower()
        if not email:
            continue
        existing = keyed[email] if email in keyed else None
        if existing is None:
            item = dict(fields)
            item["handle"] = email
            item["primary_email"] = email
            item["account_emails"] = json.dumps(_json_list(fields["account_emails"]), ensure_ascii=False)
            item["source_ids"] = json.dumps(_json_list(fields["source_ids"]), ensure_ascii=False)
            keyed[email] = item
            continue
        for column in ("display_name", "full_name", "company_guess", "primary_email_type", "source", "source_channels"):
            if fields[column] and not existing[column]:
                existing[column] = str(fields[column])
        for column in ("total_messages", "thread_count"):
            existing[column] = str(_int_value(existing[column]) + _int_value(fields[column]))
        if str(fields["last_interaction"] or "") > str(existing["last_interaction"] or ""):
            existing["last_interaction"] = str(fields["last_interaction"] or "")
        existing["account_emails"] = json.dumps(
            unique_strings(_json_list(existing["account_emails"]) + _json_list(fields["account_emails"])),
            ensure_ascii=False,
        )
        existing["source_ids"] = json.dumps(
            unique_strings(_json_list(existing["source_ids"]) + _json_list(fields["source_ids"])),
            ensure_ascii=False,
        )
    return [{column: str(row[column] or "") for column in GMAIL_DISCOVERY_COLUMNS} for _, row in sorted(keyed.items())]


def _same_account_emails(left: Any, right: list[str]) -> bool:
    return sorted(_as_list(left)) == sorted(_as_list(right))


@dataclass(frozen=True)
class GmailManifestResume:
    """A prior gmail manifest (the stage manifest for the merge plan, or a
    per-account manifest for the extractor), parsed once — `from_document` is the
    ONE reader of its resume fields. A field the document does not carry defaults
    empty; a document that is not a dict reads as an all-empty resume (first run)."""

    calculation_version: str = ""
    account_emails: list[str] = field(default_factory=list)
    created_at: str = ""

    @classmethod
    def from_document(cls, document: Any) -> "GmailManifestResume":
        document = document if isinstance(document, dict) else {}
        account_emails = document.get("account_emails")
        return cls(
            calculation_version=str(document.get("calculation_version") or ""),
            account_emails=[str(item) for item in (account_emails if isinstance(account_emails, list) else [])],
            created_at=str(document.get("created_at") or ""),
        )


def gmail_discovery_merge_plan(
    existing_manifest: GmailManifestResume,
    account_emails: list[str],
    *,
    output_rows: int,
    full_rerun_requested: bool = False,
) -> dict[str, str]:
    """Explain how the child outputs became the stage output.

    Pure — every input is passed in, so the caller owns all filesystem reads and
    the parse of the prior manifest (`GmailManifestResume.from_document`).
    `output_rows` is the row count of the existing contacts.csv (0 when the file
    is missing or header-only); `full_rerun_requested` is the caller's explicit
    rescan request (`--fresh`).

    `mode` is always `full_rewrite`: a child's rows restate its account's whole
    truth (extract_gmail re-derives from the entire archive — see
    GMAIL_CALCULATION_FULL_RECOUNT), so the children alone are always the new
    output and nothing on disk is preserved. Only the diagnostic `reason` varies,
    first match wins:
      empty_output                    contacts.csv is missing or header-only.
      full_rerun_requested            the caller passed --fresh.
      calculation_version_changed     the rows on disk were computed under
                                      different interaction-counting rules.
      account_emails_changed          they were computed for a different account
                                      set.
      children_returned_full_recounts the ordinary case.
    """
    if output_rows <= 0:
        return {"mode": "full_rewrite", "reason": "empty_output"}
    if full_rerun_requested:
        return {"mode": "full_rewrite", "reason": "full_rerun_requested"}
    if existing_manifest.calculation_version != GMAIL_INTERACTION_CALCULATION_VERSION:
        return {"mode": "full_rewrite", "reason": "calculation_version_changed"}
    if not _same_account_emails(existing_manifest.account_emails, account_emails):
        return {"mode": "full_rewrite", "reason": "account_emails_changed"}
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
    source = source_config("gmail")
    resolved_accounts = unique_strings(account_emails or [])
    config_db = str(source.optional_value("inputs", "msgvault_db_default") or DEFAULT_MSGVAULT_DB)
    resolved_db = str(Path(str(msgvault_db) if msgvault_db else config_db).expanduser())
    resolved_query = (str(sync_query or "").strip() if sync_query is not None
                      else source.optional_value("inputs", "sync_query").strip())
    return GmailDiscoveryInputs(
        account_emails=tuple(resolved_accounts),
        msgvault_db=resolved_db,
        sync_query=resolved_query,
    )
