"""Typed stage-manifest payloads for gmail discovery — the ONLY shapes
gmail/discover.py may emit. New fields are added here, never invented inline.

Changelog:
  2026-07-23 (audit):
    - Payloads discover.py previously assembled as inline dicts became these
      typed dataclasses.
  2026-07-23 (account-email selection): the selected_accounts field on
    GmailDiscoveryIncrementalMismatch and GmailDiscoveryCompleted was renamed
    account_emails, matching the single --account-email selection surface.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.manifests import StagePayload  # noqa: E402


@dataclass
class GmailPrivacy:
    message_bodies_read: bool = False
    gmail_sync_ran: bool = False
    parallel_called: bool = False
    rapidapi_called: bool = False


@dataclass
class GmailDiscoverySkipped(StagePayload):
    started_at: str = ""
    duration_seconds: float = 0.0
    accounts_timing: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    contacts_csv: str = ""
    linkedin_resolution_queue_csv: str = ""
    status: str = "skipped"
    source: str = "gmail"


@dataclass
class GmailDiscoveryFailed(StagePayload):
    started_at: str = ""
    duration_seconds: float = 0.0
    accounts_timing: list[dict[str, Any]] = field(default_factory=list)
    account_email: str = ""
    error: Any = None
    status: str = "failed"
    source: str = "gmail"


@dataclass
class GmailDiscoveryIncrementalMismatch(StagePayload):
    """A full rewrite built from delta-only children would drop rows — loud failure."""
    started_at: str = ""
    duration_seconds: float = 0.0
    accounts_timing: list[dict[str, Any]] = field(default_factory=list)
    calculation_version: str = ""
    calculation_mode: str = ""
    calculation_reason: str = "full_rewrite_requires_full_recount_children"
    account_emails: list[str] = field(default_factory=list)
    child_calculation_modes: list[str] = field(default_factory=list)
    children: list[dict[str, Any]] = field(default_factory=list)
    status: str = "failed"
    source: str = "gmail"


@dataclass
class GmailDiscoveryCompleted(StagePayload):
    started_at: str = ""
    duration_seconds: float = 0.0
    accounts_timing: list[dict[str, Any]] = field(default_factory=list)
    calculation_version: str = ""
    calculation_mode: str = ""
    calculation_reason: str = ""
    child_calculation_modes: list[str] = field(default_factory=list)
    applied_incremental_inputs: list[str] = field(default_factory=list)
    skipped_incremental_inputs: list[str] = field(default_factory=list)
    contacts_csv: str = ""
    linkedin_resolution_queue_csv: str = ""
    contacts: int = 0
    account_emails: list[str] = field(default_factory=list)
    msgvault_db: str = ""
    updated_at: str = ""
    privacy: GmailPrivacy = field(default_factory=GmailPrivacy)
    children: list[dict[str, Any]] = field(default_factory=list)
    status: str = "completed"
    source: str = "gmail"
