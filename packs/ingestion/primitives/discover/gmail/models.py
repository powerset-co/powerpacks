"""Typed stage-manifest payloads and row models for gmail discovery — the ONLY
shapes gmail/discover.py may emit. New fields are added here, never invented inline.

Changelog:
  2026-09-23 (typed rows): added `GmailExtractPayload`, the typed read of what
    `GmailExtractor.run_msgvault` returns — discover parses the extractor payload
    once there instead of probing the dict.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.gmail.util import GMAIL_DISCOVERY_COLUMNS  # noqa: E402
from packs.ingestion.primitives.pipeline.contract import StageManifest, row_model_for  # noqa: E402

# The discovery queue row contract uses the writer's column order.
GmailContactRow = row_model_for("GmailContactRow", GMAIL_DISCOVERY_COLUMNS)


class GmailPrivacy(BaseModel):
    message_bodies_read: bool = False
    gmail_sync_ran: bool = False
    parallel_called: bool = False
    rapidapi_called: bool = False


@dataclass(frozen=True)
class GmailExtractPayload:
    """What `GmailExtractor.run_msgvault` returns (and the error mirror discover
    substitutes for a ValueError), parsed once — `from_payload` is the ONE reader
    of its shape, so discover reads attributes instead of probing the dict. `raw`
    keeps the original payload for the manifest's `children` entry and a failed
    payload (both persisted verbatim)."""

    status: str = ""
    calculation_mode: str = ""
    contacts_written: Any = ""
    artifacts: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Any) -> "GmailExtractPayload":
        raw = payload if isinstance(payload, dict) else {}
        counts = raw.get("counts")
        counts = counts if isinstance(counts, dict) else {}
        artifacts = raw.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        return cls(
            status=str(raw.get("status") or ""),
            calculation_mode=str(raw.get("calculation_mode") or ""),
            contacts_written=counts.get("contacts_written", ""),
            artifacts=dict(artifacts),
            raw=dict(raw),
        )


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
