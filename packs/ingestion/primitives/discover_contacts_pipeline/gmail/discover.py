"""Gmail contact discovery CLI: sync msgvault, aggregate contacts, build queues."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import argparse
import sys


# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover_contacts_pipeline.common import (  # noqa: E402
    GMAIL_INTERACTION_CALCULATION_VERSION,
    emit,
    now_iso,
    ordered_unique,
    py_cmd,
    read_csv_rows,
    read_json,
    run_cmd,
    source_slug,
    write_csv_rows,
    write_json,
    write_stage_manifest,
)
from packs.ingestion.primitives.discover_contacts_pipeline.discovery_config import (  # noqa: E402
    accounts_path as configured_accounts_path,
    load_config,
    output_path,
)
from packs.ingestion.primitives.discover_contacts_pipeline.gmail.models import (  # noqa: E402
    GmailDiscoveryCompleted,
    GmailDiscoveryFailed,
    GmailDiscoveryIncrementalMismatch,
    GmailDiscoverySkipped,
    GmailPrivacy,
)
from packs.ingestion.primitives.discover_contacts_pipeline.gmail.util import (  # noqa: E402
    GMAIL_DISCOVERY_COLUMNS,
    GMAIL_CALCULATION_FULL_RECOUNT,
    GMAIL_CALCULATION_INCREMENTAL_DELTA,
    _as_list,
    _merge_rows,
    gmail_incremental_input_id,
    gmail_discovery_merge_plan,
    gmail_network_import_base_dir,
    inputs,
)
from packs.ingestion.primitives.discover_contacts_pipeline.gmail.sync import (  # noqa: E402
    sync_msgvault_account,
)


def discover(
    *,
    accounts_file: Path | None = None,
    selected_accounts: list[str] | None = None,
    account_email: str | None = None,
    msgvault_db: str | None = None,
    sync_query: str | None = None,
    skip_msgvault_sync: bool = False,
    sync_after: str = "",
    sync_before: str = "",
    fresh: bool = False,
    limit: int = 0,
    no_attachments: bool = False,
) -> dict[str, Any]:
    """Discover Gmail contacts: sync msgvault per selected account, aggregate
    contacts, build the resolution queue, write the stage manifest.

    Keyword-only ON PURPOSE (the `*`): thirteen knobs are unusable positionally.
    STRICT on purpose too — the old `**_` swallowed unknown kwargs silently,
    which let call sites pass options that were never honored; now a typo or a
    phantom option raises. `accounts_file` is the ONE accounts-state param
    (the old accounts_path alias is gone), and all configuration resolves
    through resolve_discovery_inputs — one documented precedence, one place.

    DEFAULTS CONVENTION — the `| None = None` params are override SENTINELS,
    not values: None means "no override given, inherit from the next config
    layer" (accounts.json state, then discovery.config defaults). Params with
    no lower layer carry their real default instead:
      accounts_file=None       -> inherit the configured accounts path
      selected_accounts=None / account_email=None -> inherit linked accounts
      msgvault_db=None (or "") -> inherit the configured/state db path
      sync_query=None          -> inherit the configured query;
      sync_query=""            -> EXPLICITLY clear it (the one distinction
                                  callers actually use — see the orchestrator)
      sync_after/sync_before="" -> plain values: pure run-window overrides
                                  with no config layer beneath them
      skip_msgvault_sync/fresh/limit/no_attachments -> run-mode flags, no
                                  layering, real defaults."""
    # ONE resolution point for configuration (see resolve_discovery_inputs):
    # explicit caller/CLI overrides > accounts.json state > discovery.config
    # defaults. Nothing below this line consults config sources directly.
    resolved = resolve_discovery_inputs(
        accounts_file,
        selected_accounts=selected_accounts,
        account_email=account_email,
        msgvault_db=msgvault_db,
        sync_query=sync_query,
    )
    source_inputs = {
        "selected_accounts": list(resolved.selected_accounts),
        "msgvault_db": resolved.msgvault_db,
        "sync_query": resolved.sync_query,
    }
    contacts_csv = output_path("gmail", "contacts_csv")
    queue_csv = output_path("gmail", "linkedin_resolution_queue_csv")
    manifest_json = output_path("gmail", "manifest_json")
    contacts_csv.parent.mkdir(parents=True, exist_ok=True)

    if not source_inputs["selected_accounts"]:
        payload = GmailDiscoverySkipped(
            reason="no_selected_accounts",
            contacts_csv=str(contacts_csv),
            linkedin_resolution_queue_csv=str(queue_csv),
        )
        return write_stage_manifest(manifest_json, payload)

    # PHASE 1 — per selected account: sync msgvault (unless skipped), then run
    # the gmail_network_import child, which reads the synced store and emits
    # this account's contact rows. A child reports its calculation_mode:
    #   full_recount        -> rows are the account's COMPLETE current truth
    #   incremental_delta   -> rows are ONLY the new/changed contacts since
    #                          the last run and must be APPENDED exactly once
    incoming_outputs: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    child_modes: list[str] = []
    child_output_base = gmail_network_import_base_dir(contacts_csv)
    for email in source_inputs["selected_accounts"]:
        account_artifact_dir = child_output_base / "discover" / "gmail" / source_slug(email)
        if skip_msgvault_sync:
            sync = {
                "status": "skipped",
                "reason": "skip_msgvault_sync",
                "account_email": email,
                "query": source_inputs["sync_query"],
            }
        else:
            sync = sync_msgvault_account(
                email,
                source_inputs["msgvault_db"],
                source_inputs["sync_query"],
                sync_after_override=sync_after,
                sync_before=sync_before,
                fresh=fresh,
                limit=limit,
                no_attachments=no_attachments,
            )
        if sync["status"] == "failed":
            failed = GmailDiscoveryFailed(account_email=email, error=sync)
            return write_stage_manifest(manifest_json, failed)
        cmd = py_cmd(
            "packs/ingestion/primitives/gmail_network_import/gmail_network_import.py",
            "msgvault",
            "--db",
            source_inputs["msgvault_db"],
            "--account-email",
            email,
            "--output-dir",
            str(child_output_base),
        )
        code, child, stderr = run_cmd(cmd)
        child_mode = str(child.get("calculation_mode") or child.get("counts", {}).get("calculation_mode") or GMAIL_CALCULATION_FULL_RECOUNT) if isinstance(child, dict) else GMAIL_CALCULATION_FULL_RECOUNT
        child_modes.append(child_mode)
        child_artifacts = child.get("artifacts") if isinstance(child, dict) and isinstance(child.get("artifacts"), dict) else {}
        child_queue_text = str((child_artifacts or {}).get("linkedin_resolution_queue_csv") or "").strip()
        child_people_text = str((child_artifacts or {}).get("people_csv") or "").strip()
        child_artifact_dir = str(child.get("artifact_dir") or account_artifact_dir) if isinstance(child, dict) else str(account_artifact_dir)
        child_queue = Path(child_queue_text) if child_queue_text else None
        rows_written = 0
        rows: list[dict[str, Any]] = []
        if child_queue and child_queue.is_file():
            _fields, rows = read_csv_rows(child_queue)
            rows_written = len(rows)
        # Replay-dedup key for the append-only path: YES — incremental children
        # only ever APPEND to the existing contacts/queue, so replaying the same
        # child output (rerun, crash-resume) must not append the same rows twice.
        # The id is a content hash of (account, calculation version, rows); ids
        # already recorded in the manifest are skipped in PHASE 3 below.
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
            "artifact_dir": child_artifact_dir,
            "people_csv": child_people_text,
            "linkedin_resolution_queue_csv": child_queue_text,
            "artifacts": child_artifacts,
        })
        if code != 0:
            failed = GmailDiscoveryFailed(account_email=email, error=stderr or child)
            return write_stage_manifest(manifest_json, failed)

    # PHASE 2 — decide how to combine child outputs with what is on disk.
    # full_rewrite: the calculation version or the selected-account set changed,
    #   or any child did a full recount -> rebuild contacts.csv from scratch.
    # incremental_update: EVERY child returned a delta -> keep existing rows and
    #   append only unapplied deltas (dedup via incremental_input_id above).
    existing_manifest = read_json(manifest_json, {}) or {}
    merge_plan = gmail_discovery_merge_plan(existing_manifest, source_inputs["selected_accounts"], child_modes)
    existing: list[dict[str, Any]] = []
    incoming: list[dict[str, Any]] = []
    applied_incremental_inputs = _as_list(existing_manifest.get("applied_incremental_inputs"))
    applied_incremental_input_set = set(applied_incremental_inputs)
    skipped_incremental_inputs: list[str] = []
    incremental_outputs = [output for output in incoming_outputs if output.get("calculation_mode") == GMAIL_CALCULATION_INCREMENTAL_DELTA]
    # Guard: a full rewrite built from delta-only children would DROP every row
    # the deltas do not restate. Fail loudly instead of silently losing contacts.
    if merge_plan["mode"] != "incremental_update" and incremental_outputs:
        mismatch = GmailDiscoveryIncrementalMismatch(
            calculation_version=GMAIL_INTERACTION_CALCULATION_VERSION,
            calculation_mode=merge_plan["mode"],
            selected_accounts=source_inputs["selected_accounts"],
            child_calculation_modes=child_modes,
            children=children,
        ).to_payload()
        write_json(manifest_json, mismatch)
        return mismatch
    # PHASE 3 — assemble the row set. Incremental: existing rows + each child's
    # unapplied delta (skipping already-applied incremental_input_ids). Full
    # rewrite: children's rows only.
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
    # PHASE 4 — merge by primary email (counts summed, newest last_interaction,
    # account lists unioned) and write BOTH stage outputs with the same rows:
    # contacts.csv is the aggregate; linkedin_resolution_queue.csv is the same
    # content republished as the import stage's work queue.
    merged = _merge_rows([*existing, *incoming])
    write_csv_rows(contacts_csv, GMAIL_DISCOVERY_COLUMNS, merged)
    write_csv_rows(queue_csv, GMAIL_DISCOVERY_COLUMNS, merged)
    return write_stage_manifest(manifest_json, GmailDiscoveryCompleted(
        calculation_version=GMAIL_INTERACTION_CALCULATION_VERSION,
        calculation_mode=merge_plan["mode"],
        calculation_reason=merge_plan["reason"],
        child_calculation_modes=child_modes,
        applied_incremental_inputs=applied_incremental_inputs,
        skipped_incremental_inputs=skipped_incremental_inputs,
        contacts_csv=str(contacts_csv),
        linkedin_resolution_queue_csv=str(queue_csv),
        contacts=len(merged),
        selected_accounts=source_inputs["selected_accounts"],
        msgvault_db=source_inputs["msgvault_db"],
        updated_at=now_iso(),
        privacy=GmailPrivacy(gmail_sync_ran=not skip_msgvault_sync),
        children=children,
    ))


# Moved here from sync.py during the audit split: this step DRIVES discover(),
# so leaving it in sync.py made `discover` an unbound name there (latent
# NameError) and would have required a circular import to fix in place.
def run_gmail_msgvault(ledger_path: Path, ledger: dict[str, Any], _worker: dict[str, Any]) -> bool:
    input_cfg = ledger.get("input") or {}
    payload = discover(
        accounts_file=Path(str(input_cfg.get("from_accounts") or ".powerpacks/ingestion/accounts.json")),
        selected_accounts=_as_list(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email") or []),
        msgvault_db=str(input_cfg.get("msgvault_db") or ""),
        sync_query=str(input_cfg.get("gmail_sync_query") or ""),
        skip_msgvault_sync=bool(input_cfg.get("skip_msgvault_sync")),
    )
    ledger.setdefault("artifacts", {})["gmail_contacts_csv"] = payload.get("contacts_csv", "")
    ledger.setdefault("artifacts", {})["gmail_linkedin_resolution_queue_csv"] = payload.get("linkedin_resolution_queue_csv", "")
    return payload.get("status") in {"completed", "skipped"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover Gmail contacts from existing msgvault metadata")
    parser.add_argument("command", choices=["discover"])
    parser.add_argument("--accounts", type=Path, default=None)
    parser.add_argument("--account-email", action="append", default=[], help="Account email to sync (repeatable); default: all linked")
    parser.add_argument("--msgvault-db", default="")
    parser.add_argument("--sync-query", default=None)
    parser.add_argument("--skip-msgvault-sync", action="store_true")
    parser.add_argument("--sync-after", default="", help="Window start YYYY-MM-DD (overrides resume inference)")
    parser.add_argument("--sync-before", default="", help="Window end YYYY-MM-DD")
    parser.add_argument("--fresh", action="store_true", help="Force --noresume so the full window is rescanned")
    parser.add_argument("--limit", type=int, default=0, help="Cap messages per account (testing safety)")
    parser.add_argument("--no-attachments", action="store_true", help="Skip attachment download when msgvault supports it")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = discover(
        accounts_file=args.accounts,
        selected_accounts=args.account_email or None,
        msgvault_db=args.msgvault_db,
        sync_query=args.sync_query,
        skip_msgvault_sync=args.skip_msgvault_sync,
        sync_after=args.sync_after,
        sync_before=args.sync_before,
        fresh=args.fresh,
        limit=args.limit,
        no_attachments=args.no_attachments,
    )
    emit(payload)
    return 1 if payload.get("status") == "failed" else 0


