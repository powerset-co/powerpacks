"""Gmail contact discovery CLI: sync msgvault, aggregate contacts, build queues.

Shape (discover()):
  Resolve config ONCE (resolve_discovery_inputs: CLI/caller overrides >
  accounts.json state > discovery.config defaults) into source_inputs, then hand
  off to GmailDiscovery, which owns the run: the fixed output dir, the per-account
  channels, the merge plan, and the final typed stage manifest.

  Each selected account is a GmailAccountChannel that owns its fixed per-account
  output dir (gmail_discover_dir) and its sync -> engine-child chain:
    - sync msgvault (skippable): window = --sync-after, else the resume marker
      from infer_msgvault_sync_after (msgvault/sync.py); an explicit window adds
      --noresume. A failed sync short-circuits with GmailDiscoveryFailed.
    - spawn gmail/discover_engine.py, which reads the synced store and writes this
      account's rows to the channel's FIXED queue_csv/people_csv (read back from
      there, never re-parsed out of the payload). The child reports its
      calculation_mode: full_recount (its rows ARE the account's whole truth) |
      incremental_delta (only new rows, appended once, deduped by
      incremental_input_id).
    extract records what it contributed in self.artifacts; run() returns None on
    success or a GmailDiscoveryFailed payload that short-circuits the run.

  GmailDiscovery loops the channels (stopping at the first failure), then
  gmail_discovery_merge_plan picks full_rewrite (calc-version/account-set change,
  or any full_recount -> rebuild contacts.csv from child rows) vs
  incremental_update (keep existing rows + append unapplied deltas, dedup by
  incremental_input_id). A full_rewrite fed only deltas fails loudly (it would
  drop rows). It merges rows by primary email -> writes contacts.csv +
  linkedin_resolution_queue.csv (same rows) + a typed stage manifest (models.py).

Changelog:
  2026-07-23 (oop): the monolithic 4-phase discover() body — the per-account
    sync/child loop over ad-hoc dicts and the frozen EngineChild dataclass — was
    replaced by GmailAccountChannel (one per selected account: owns its output
    dir, sync step, and engine-child spawn; records its contribution in
    self.artifacts) and a GmailDiscovery store (owns the output dir, the channel
    loop, the merge plan, and the manifest). discover() is now a thin wrapper:
    resolve inputs -> GmailDiscovery(...).run(). CLI signature, fixed output
    paths, and the typed manifest payloads (models.py) are unchanged.
  2026-07-23 (audit): the isinstance/defensive-path ladder over the child payload
    is gone: run_cmd always returns a dict and the child is our own discover_engine
    writing known paths, so the queue/people CSVs are read from gmail_discover_dir
    (common/paths.py), not re-parsed out of the payload. The msgvault sync import
    moved to the gmail/msgvault/ package.
  2026-07-23 (audit):
    - discover() became strictly keyword-only: the old `**_` catch-all
      swallowed unknown kwargs silently, so call sites could pass options that
      were never honored; a typo or phantom option now raises TypeError.
    - The old `accounts_path` alias parameter was removed; `accounts_file` is
      the single accounts-state parameter.
  2026-07-23 (audit batch 17): the per-account child was renamed —
    `gmail/network_import.py` split into `gmail/msgvault_store.py` (reader)
    and `gmail/discover_engine.py` (the CLI spawned here); the base-dir helper
    is now `discover_engine_base_dir`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import argparse
import sys
import time


# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit, now_iso, read_json, write_json  # noqa: E402
from packs.ingestion.primitives.common.paths import gmail_discover_dir  # noqa: E402
from packs.ingestion.primitives.common.proc import py_cmd, run_cmd  # noqa: E402
from packs.ingestion.primitives.common.manifests import write_stage_manifest  # noqa: E402
from packs.ingestion.primitives.discover.common import (  # noqa: E402
    GMAIL_INTERACTION_CALCULATION_VERSION,
    read_csv_rows,
    write_csv_rows,
)
from packs.ingestion.primitives.discover.discovery_config import (  # noqa: E402
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
    resolve_discovery_inputs,
)
from packs.ingestion.primitives.discover.gmail.msgvault.sync import (  # noqa: E402
    sync_msgvault_account,
)


class GmailAccountChannel:
    """One selected Gmail account. Owns its FIXED per-account output dir
    (gmail_discover_dir), its msgvault sync step, and its gmail/discover_engine.py
    child spawn. run() syncs (unless skipped), spawns the child, and reads this
    account's rows back from the child's fixed queue_csv — never re-parsed out of
    the payload, since the child is our own discover_engine writing known paths.

    It records what it contributed on self:
      artifacts             the fixed queue/people CSV paths it produced
      mode                  the child's calculation_mode (full_recount |
                            incremental_delta)
      rows                  the queue rows the child wrote (empty when missing)
      incremental_input_id  content-hash dedup key for the append-only path
      record                the per-account entry the store puts in manifest.children
      output                the incoming row the store feeds the merge

    run() returns None on success or a GmailDiscoveryFailed payload (failed sync
    or non-zero child exit) that short-circuits the discovery run."""

    def __init__(
        self,
        *,
        account_email: str,
        output_base: Path,
        msgvault_db: str,
        sync_query: str,
        skip_msgvault_sync: bool = False,
        sync_after: str = "",
        sync_before: str = "",
        fresh: bool = False,
        limit: int = 0,
        no_attachments: bool = False,
    ) -> None:
        self.account_email = account_email
        self.output_base = output_base
        self.msgvault_db = msgvault_db
        self.sync_query = sync_query
        self.skip_msgvault_sync = skip_msgvault_sync
        self.sync_after = sync_after
        self.sync_before = sync_before
        self.fresh = fresh
        self.limit = limit
        self.no_attachments = no_attachments
        # Populated by run(); defaults hold for a channel that never ran.
        self.mode: str = GMAIL_CALCULATION_FULL_RECOUNT
        self.rows: list[dict[str, Any]] = []
        self.incremental_input_id: str = ""
        self.artifacts: dict[str, Any] = {}
        self.record: dict[str, Any] = {}
        self.timing: dict[str, Any] = {}

    # Path accessors are computed from the module-level gmail_discover_dir at call
    # time so the child does not choose its own output — discover() reads these.
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

    @property
    def output(self) -> dict[str, Any]:
        """The incoming row the store merges: account, calc mode, dedup id, rows."""
        return {
            "account_email": self.account_email,
            "calculation_mode": self.mode,
            "incremental_input_id": self.incremental_input_id,
            "rows": self.rows,
        }

    def _sync(self) -> dict[str, Any]:
        """Sync this account's msgvault window (or a skipped stub). An explicit
        --sync-after window overrides the resume marker and forces --noresume;
        see sync_msgvault_account."""
        if self.skip_msgvault_sync:
            return {
                "status": "skipped",
                "reason": "skip_msgvault_sync",
                "account_email": self.account_email,
                "query": self.sync_query,
            }
        return sync_msgvault_account(
            self.account_email,
            self.msgvault_db,
            self.sync_query,
            sync_after_override=self.sync_after,
            sync_before=self.sync_before,
            fresh=self.fresh,
            limit=self.limit,
            no_attachments=self.no_attachments,
        )

    def _child_cmd(self) -> list[str]:
        """argv for the gmail/discover_engine.py child. --output-dir is the base;
        the child derives its own gmail_discover_dir under it (the paths above)."""
        return py_cmd(
            "packs/ingestion/primitives/discover/gmail/discover_engine.py",
            "msgvault",
            "--db",
            self.msgvault_db,
            "--account-email",
            self.account_email,
            "--output-dir",
            str(self.output_base),
        )

    def run(self) -> GmailDiscoveryFailed | None:
        """Sync then spawn the engine child, recording the contribution on self.
        Returns None on success or a GmailDiscoveryFailed payload that stops the run."""
        started = time.monotonic()
        sync = self._sync()
        if sync["status"] == "failed":
            self._finish_timing(started, sync)
            return GmailDiscoveryFailed(account_email=self.account_email, error=sync)
        # run_cmd ALWAYS returns (code, dict, stderr) and the child is our own
        # discover_engine emitting a known payload — so read the payload as a dict
        # with no type-sniffing, and take the queue/people CSVs from the child's
        # FIXED gmail_discover_dir paths rather than re-parsing them out of the
        # payload's artifacts block.
        code, payload, stderr = run_cmd(self._child_cmd())
        self.mode = str(
            payload.get("calculation_mode")
            or payload.get("counts", {}).get("calculation_mode")
            or GMAIL_CALCULATION_FULL_RECOUNT
        )
        if self.queue_csv.is_file():
            _fields, self.rows = read_csv_rows(self.queue_csv)
        # Replay-dedup key for the append-only path (see GmailDiscovery PHASE 3):
        # incremental children only APPEND to the existing contacts/queue, so
        # replaying the same child output (rerun, crash-resume) must not append the
        # same rows twice. The id is a content hash of (account, calc version, rows);
        # ids already recorded in the manifest are skipped when the store assembles.
        self.incremental_input_id = gmail_incremental_input_id(self.account_email, self.rows)
        self.artifacts = {
            "linkedin_resolution_queue_csv": str(self.queue_csv),
            "people_csv": str(self.people_csv),
        }
        self.record = {
            "account_email": self.account_email,
            "sync": sync,
            "code": code,
            "status": payload.get("status", ""),
            "contacts": payload.get("contacts") or payload.get("counts", {}).get("contacts_written", ""),
            "calculation_mode": self.mode,
            "incremental_input_id": self.incremental_input_id if self.mode == GMAIL_CALCULATION_INCREMENTAL_DELTA else "",
            "rows_read": len(self.rows),
            "artifact_dir": str(self.discover_dir),
            "people_csv": self.artifacts["people_csv"],
            "linkedin_resolution_queue_csv": self.artifacts["linkedin_resolution_queue_csv"],
            "artifacts": payload.get("artifacts", {}),
        }
        if code != 0:
            self._finish_timing(started, sync)
            return GmailDiscoveryFailed(account_email=self.account_email, error=stderr or payload)
        self._finish_timing(started, sync)
        return None

    def _finish_timing(self, started: float, sync: dict[str, Any]) -> None:
        """Record this account's monotonic elapsed time and optional sync count."""
        self.timing = {
            "email": self.account_email,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        if sync.get("messages_added") not in (None, ""):
            self.timing["messages_added"] = sync["messages_added"]


class GmailDiscovery:
    """Store/orchestrator for one Gmail discovery run. discover() resolves config
    once and passes source_inputs; this owns everything else: the fixed output dir
    (the one mkdir), the per-account channels, the run loop (stop at the first
    failed channel), the merge plan, and the typed stage manifest. Holds all
    filesystem side effects so the channels only sync + spawn + read their rows.

      full_rewrite       calc version or selected-account set changed, or any
                         child did a full recount -> rebuild contacts.csv from
                         child rows.
      incremental_update EVERY child returned a delta -> keep existing rows and
                         append only unapplied deltas (dedup via incremental_input_id).
    A full rewrite built from delta-only children would DROP every row the deltas
    do not restate, so that combination fails loudly instead of losing contacts."""

    def __init__(
        self,
        *,
        source_inputs: dict[str, Any],
        skip_msgvault_sync: bool = False,
        sync_after: str = "",
        sync_before: str = "",
        fresh: bool = False,
        limit: int = 0,
        no_attachments: bool = False,
    ) -> None:
        self.source_inputs = source_inputs
        self.skip_msgvault_sync = skip_msgvault_sync
        # Read output_path at call time (not import) so tests can patch the module
        # global and the store honors it.
        self.contacts_csv = output_path("gmail", "contacts_csv")
        self.queue_csv = output_path("gmail", "linkedin_resolution_queue_csv")
        self.manifest_json = output_path("gmail", "manifest_json")
        self.contacts_csv.parent.mkdir(parents=True, exist_ok=True)  # the one place the dir is created
        child_output_base = discover_engine_base_dir(self.contacts_csv)
        self.channels: list[GmailAccountChannel] = [
            GmailAccountChannel(
                account_email=email,
                output_base=child_output_base,
                msgvault_db=source_inputs["msgvault_db"],
                sync_query=source_inputs["sync_query"],
                skip_msgvault_sync=skip_msgvault_sync,
                sync_after=sync_after,
                sync_before=sync_before,
                fresh=fresh,
                limit=limit,
                no_attachments=no_attachments,
            )
            for email in source_inputs["selected_accounts"]
        ]

    def run(self) -> dict[str, Any]:
        started_at = now_iso()
        started = time.monotonic()
        if not self.source_inputs["selected_accounts"]:
            return write_stage_manifest(self.manifest_json, GmailDiscoverySkipped(
                started_at=started_at,
                duration_seconds=round(time.monotonic() - started, 3),
                accounts_timing=[],
                reason="no_selected_accounts",
                contacts_csv=str(self.contacts_csv),
                linkedin_resolution_queue_csv=str(self.queue_csv),
            ))

        # PHASE 1 — per selected account: sync msgvault (unless skipped), then run
        # the gmail/discover_engine.py child. Stop at the first failed channel.
        for channel in self.channels:
            failed = channel.run()
            if failed is not None:
                failed.started_at = started_at
                failed.duration_seconds = round(time.monotonic() - started, 3)
                failed.accounts_timing = [item.timing for item in self.channels if item.timing]
                return write_stage_manifest(self.manifest_json, failed)
        children = [channel.record for channel in self.channels]
        child_modes = [channel.mode for channel in self.channels]
        incoming_outputs = [channel.output for channel in self.channels]

        # PHASE 2 — decide how to combine child outputs with what is on disk.
        existing_manifest = read_json(self.manifest_json, {}) or {}
        merge_plan = gmail_discovery_merge_plan(
            existing_manifest, self.source_inputs["selected_accounts"], child_modes)
        existing: list[dict[str, Any]] = []
        incoming: list[dict[str, Any]] = []
        applied_incremental_inputs = _as_list(existing_manifest.get("applied_incremental_inputs"))
        applied_incremental_input_set = set(applied_incremental_inputs)
        skipped_incremental_inputs: list[str] = []
        incremental_outputs = [
            output for output in incoming_outputs
            if output.get("calculation_mode") == GMAIL_CALCULATION_INCREMENTAL_DELTA
        ]
        # Guard: a full rewrite built from delta-only children would DROP every row
        # the deltas do not restate. Fail loudly instead of silently losing contacts.
        if merge_plan["mode"] != "incremental_update" and incremental_outputs:
            mismatch = GmailDiscoveryIncrementalMismatch(
                started_at=started_at,
                duration_seconds=round(time.monotonic() - started, 3),
                accounts_timing=[channel.timing for channel in self.channels],
                calculation_version=GMAIL_INTERACTION_CALCULATION_VERSION,
                calculation_mode=merge_plan["mode"],
                selected_accounts=self.source_inputs["selected_accounts"],
                child_calculation_modes=child_modes,
                children=children,
            ).to_payload()
            write_json(self.manifest_json, mismatch)
            return mismatch

        # PHASE 3 — assemble the row set. Incremental: existing rows + each child's
        # unapplied delta (skipping already-applied incremental_input_ids). Full
        # rewrite: children's rows only.
        if merge_plan["mode"] == "incremental_update" and self.contacts_csv.exists():
            _fields, existing = read_csv_rows(self.contacts_csv)
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
        write_csv_rows(self.contacts_csv, GMAIL_DISCOVERY_COLUMNS, merged)
        write_csv_rows(self.queue_csv, GMAIL_DISCOVERY_COLUMNS, merged)
        return write_stage_manifest(self.manifest_json, GmailDiscoveryCompleted(
            started_at=started_at,
            duration_seconds=round(time.monotonic() - started, 3),
            accounts_timing=[channel.timing for channel in self.channels],
            calculation_version=GMAIL_INTERACTION_CALCULATION_VERSION,
            calculation_mode=merge_plan["mode"],
            calculation_reason=merge_plan["reason"],
            child_calculation_modes=child_modes,
            applied_incremental_inputs=applied_incremental_inputs,
            skipped_incremental_inputs=skipped_incremental_inputs,
            contacts_csv=str(self.contacts_csv),
            linkedin_resolution_queue_csv=str(self.queue_csv),
            contacts=len(merged),
            selected_accounts=self.source_inputs["selected_accounts"],
            msgvault_db=self.source_inputs["msgvault_db"],
            updated_at=now_iso(),
            privacy=GmailPrivacy(gmail_sync_ran=not self.skip_msgvault_sync),
            children=children,
        ))


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
    contacts, build the resolution queue, write the stage manifest. A thin wrapper
    — it resolves config ONCE, then GmailDiscovery(...).run() owns the run.

    Keyword-only ON PURPOSE (the `*`): eleven knobs are unusable positionally,
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
    return GmailDiscovery(
        source_inputs=source_inputs,
        skip_msgvault_sync=skip_msgvault_sync,
        sync_after=sync_after,
        sync_before=sync_before,
        fresh=fresh,
        limit=limit,
        no_attachments=no_attachments,
    ).run()


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
