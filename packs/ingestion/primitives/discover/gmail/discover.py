"""Gmail contact discovery CLI: sync msgvault, aggregate contacts, build queues.

What it does (discover()):
  1. Resolve config ONCE (resolve_discovery_inputs): CLI/caller overrides >
     accounts.json state > discovery.config defaults. No selected accounts ->
     skipped manifest.
  2. Per selected account -> msgvault sync (skippable), then spawn the
     gmail/discover_engine.py child. Sync window = --sync-after, else the resume
     marker from infer_msgvault_sync_after (sync.py); an explicit window adds
     --noresume. Failed sync/child -> failed manifest, stop.
  3. Each child reports calculation_mode: full_recount (its rows ARE the
     account's whole truth) | incremental_delta (only new rows, appended once,
     deduped by incremental_input_id).
  4. gmail_discovery_merge_plan picks full_rewrite (calc-version/account-set
     change, or any full_recount -> rebuild contacts.csv from child rows) vs
     incremental_update (keep existing rows + append unapplied deltas). A
     full_rewrite fed only deltas fails loudly (it would drop rows). Merge rows
     by primary email -> write contacts.csv + linkedin_resolution_queue.csv
     (same rows) + a typed stage manifest.

Changelog:
  2026-07-23 (audit): the per-account child spawn is now the frozen `EngineChild`
    dataclass (account + argv + the FIXED gmail_discover_dir output paths) instead
    of ad-hoc dicts, and the isinstance/defensive-path ladder over the child
    payload is gone: run_cmd always returns a dict and the child is our own
    discover_engine writing known paths, so the queue/people CSVs are read from
    gmail_discover_dir (common/paths.py), not re-parsed out of the payload. The
    msgvault sync import moved to the gmail/msgvault/ package.
  2026-07-23 (audit):
    - discover() became strictly keyword-only: the old `**_` catch-all
      swallowed unknown kwargs silently, so call sites could pass options that
      were never honored; a typo or phantom option now raises TypeError.
    - The old `accounts_path` alias parameter was removed; `accounts_file` is
      the single accounts-state parameter.
    - run_gmail_msgvault moved here from sync.py during the audit split: it
      drives discover(), so leaving it in sync.py made `discover` an unbound
      name there (latent NameError) and fixing it in place would have required
      a circular import.
  2026-07-23 (audit batch 17): the per-account child was renamed —
    `gmail/network_import.py` split into `gmail/msgvault_store.py` (reader)
    and `gmail/discover_engine.py` (the CLI spawned here); the base-dir helper
    is now `discover_engine_base_dir`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import argparse
import sys


# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit, now_iso, read_json, write_json  # noqa: E402
from packs.ingestion.primitives.common.paths import gmail_discover_dir  # noqa: E402
from packs.ingestion.primitives.common.proc import py_cmd, run_cmd  # noqa: E402
from packs.ingestion.primitives.discover.common import (  # noqa: E402
    GMAIL_INTERACTION_CALCULATION_VERSION,
    ordered_unique,
    read_csv_rows,
    write_csv_rows,
    write_stage_manifest,
)
from packs.ingestion.primitives.discover.discovery_config import (  # noqa: E402
    accounts_path as configured_accounts_path,
    load_config,
    output_path,
)
from packs.ingestion.primitives.discover.gmail.models import (  # noqa: E402
    GmailDiscoveryCompleted,
    GmailDiscoveryFailed,
    GmailDiscoveryIncrementalMismatch,
    GmailDiscoverySkipped,
    GmailPrivacy,
)
from packs.ingestion.primitives.discover.gmail.util import (  # noqa: E402
    GMAIL_DISCOVERY_COLUMNS,
    GMAIL_CALCULATION_FULL_RECOUNT,
    GMAIL_CALCULATION_INCREMENTAL_DELTA,
    _as_list,
    _merge_rows,
    gmail_incremental_input_id,
    gmail_discovery_merge_plan,
    discover_engine_base_dir,
    inputs,
    resolve_discovery_inputs,
)
from packs.ingestion.primitives.discover.gmail.msgvault.sync import (  # noqa: E402
    sync_msgvault_account,
)


@dataclass(frozen=True)
class EngineChild:
    """One discover_engine spawn: the account it covers, the argv used, and the
    FIXED output paths it writes (built from gmail_discover_dir before the child
    runs — the child does not choose them, so we never re-parse them out of its
    payload)."""

    account_email: str
    cmd: tuple[str, ...]
    output_base: Path

    @property
    def discover_dir(self) -> Path:
        """The child's fixed per-account output directory."""
        return gmail_discover_dir(self.output_base, self.account_email)

    @property
    def queue_csv(self) -> Path:
        """The child's `linkedin_resolution_queue.csv` (the rows discover reads back)."""
        return self.discover_dir / "linkedin_resolution_queue.csv"

    @property
    def people_csv(self) -> Path:
        """The child's canonical `people.csv`."""
        return self.discover_dir / "people.csv"


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

    Keyword-only ON PURPOSE (the `*`): thirteen knobs are unusable positionally,
    and unknown options raise TypeError. `accounts_file` is the ONE
    accounts-state param, and all configuration resolves through
    resolve_discovery_inputs — one documented precedence (explicit overrides >
    accounts.json state > discovery.config defaults), one place.

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
    # the gmail/discover_engine.py child, which reads the synced store and emits
    # this account's contact rows. A child reports its calculation_mode:
    #   full_recount        -> rows are the account's COMPLETE current truth
    #   incremental_delta   -> rows are ONLY the new/changed contacts since
    #                          the last run and must be APPENDED exactly once
    incoming_outputs: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    child_modes: list[str] = []
    child_output_base = discover_engine_base_dir(contacts_csv)
    for email in source_inputs["selected_accounts"]:
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
        child_spawn = EngineChild(
            account_email=email,
            cmd=tuple(py_cmd(
                "packs/ingestion/primitives/discover/gmail/discover_engine.py",
                "msgvault",
                "--db",
                source_inputs["msgvault_db"],
                "--account-email",
                email,
                "--output-dir",
                str(child_output_base),
            )),
            output_base=child_output_base,
        )
        # run_cmd ALWAYS returns (code, dict, stderr) and the child is our own
        # discover_engine emitting a known payload — so read the payload as a
        # dict with no type-sniffing, and take the queue/people CSVs from the
        # child's FIXED gmail_discover_dir paths rather than re-parsing them out
        # of the payload's artifacts block.
        code, payload, stderr = run_cmd(list(child_spawn.cmd))
        child_mode = str(payload.get("calculation_mode") or payload.get("counts", {}).get("calculation_mode") or GMAIL_CALCULATION_FULL_RECOUNT)
        child_modes.append(child_mode)
        rows: list[dict[str, Any]] = []
        if child_spawn.queue_csv.is_file():
            _fields, rows = read_csv_rows(child_spawn.queue_csv)
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
            "status": payload.get("status", ""),
            "contacts": payload.get("contacts") or payload.get("counts", {}).get("contacts_written", ""),
            "calculation_mode": child_mode,
            "incremental_input_id": incremental_input_id if child_mode == GMAIL_CALCULATION_INCREMENTAL_DELTA else "",
            "rows_read": len(rows),
            "artifact_dir": str(child_spawn.discover_dir),
            "people_csv": str(child_spawn.people_csv),
            "linkedin_resolution_queue_csv": str(child_spawn.queue_csv),
            "artifacts": payload.get("artifacts", {}),
        })
        if code != 0:
            failed = GmailDiscoveryFailed(account_email=email, error=stderr or payload)
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


if __name__ == "__main__":
    raise SystemExit(main())
