"""Typed stage-manifest payloads and row models for gmail discovery — the ONLY
shapes gmail/discover.py may emit. New fields are added here, never invented inline.

Changelog:
  2026-07-26 (contacts.csv deleted): DROPPED `contacts_csv` from
    GmailDiscoveryCompleted / GmailDiscoverySkipped with the file itself — it was
    byte-identical to `linkedin_resolution_queue_csv` and existed only for
    `imports/status.py` to count, which now reads the queue's row count out of the
    manifest's per-node stats.
  2026-07-25 (declared contract): the payloads are pydantic `StageManifest`
    models (`pipeline/contract.py`) instead of `StagePayload` dataclasses — same
    field names, same defaults, same None-dropping in `to_payload()`. Added
    `GmailAccountExtracted` (the per-account node's payload; it has no
    manifest.json of its own and is embedded in the stage manifest's `children`)
    and `GmailContactRow`, the row model both gmail discovery CSVs declare.
  2026-07-24 (incremental deleted): DELETED GmailDiscoveryIncrementalMismatch and
    GmailDiscoveryCompleted's applied_incremental_inputs /
    skipped_incremental_inputs fields, along with the append-only merge path that
    was their only writer. See discover.py's Changelog.
  2026-07-23 (audit):
    - Payloads discover.py previously assembled as inline dicts became these
      typed dataclasses.
  2026-07-23 (account-email selection): the selected_accounts field on
    GmailDiscoveryCompleted was renamed account_emails, matching the single
    --account-email selection surface.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.gmail.util import GMAIL_DISCOVERY_COLUMNS  # noqa: E402
from packs.ingestion.primitives.pipeline.contract import StageManifest, row_model_for  # noqa: E402

# Both gmail discovery CSVs (contacts.csv and its byte-identical twin
# linkedin_resolution_queue.csv) carry these columns, generated from the one
# column constant so a declaration cannot drift from the writer.
GmailContactRow = row_model_for("GmailContactRow", GMAIL_DISCOVERY_COLUMNS)


class GmailPrivacy(BaseModel):
    message_bodies_read: bool = False
    gmail_sync_ran: bool = False
    parallel_called: bool = False
    rapidapi_called: bool = False


class GmailDiscoverySkipped(StageManifest):
    started_at: str = ""
    duration_seconds: float = 0.0
    accounts_timing: list[dict[str, Any]] = Field(default_factory=list)
    reason: str = ""
    linkedin_resolution_queue_csv: str = ""
    status: str = "skipped"
    source: str = "gmail"


class GmailDiscoveryFailed(StageManifest):
    started_at: str = ""
    duration_seconds: float = 0.0
    accounts_timing: list[dict[str, Any]] = Field(default_factory=list)
    account_email: str = ""
    error: Any = None
    status: str = "failed"
    source: str = "gmail"


class GmailAccountExtracted(StageManifest):
    """One account's contribution. The per-account node reports into the STAGE
    manifest's `children` list (it has no manifest.json of its own), so these are
    the record fields the store already published there."""

    account_email: str = ""
    calculation_mode: str = ""
    rows_read: int = 0
    artifact_dir: str = ""
    people_csv: str = ""
    linkedin_resolution_queue_csv: str = ""
    status: str = "completed"


class GmailDiscoveryCompleted(StageManifest):
    started_at: str = ""
    duration_seconds: float = 0.0
    accounts_timing: list[dict[str, Any]] = Field(default_factory=list)
    calculation_version: str = ""
    calculation_mode: str = ""
    calculation_reason: str = ""
    child_calculation_modes: list[str] = Field(default_factory=list)
    linkedin_resolution_queue_csv: str = ""
    contacts: int = 0
    account_emails: list[str] = Field(default_factory=list)
    msgvault_db: str = ""
    updated_at: str = ""
    privacy: GmailPrivacy = Field(default_factory=GmailPrivacy)
    children: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "completed"
    source: str = "gmail"
