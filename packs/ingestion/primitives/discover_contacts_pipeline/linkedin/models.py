"""Typed stage-manifest payloads for linkedin discovery."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover_contacts_pipeline.common import StagePayload  # noqa: E402


@dataclass
class LinkedinPrivacy:
    rapidapi_called: bool = False
    parallel_called: bool = False
    upload_ran: bool = False


@dataclass
class LinkedinDiscoverySkipped(StagePayload):
    reason: str = ""
    connections_csv: str = ""
    contacts_csv: str = ""
    status: str = "skipped"
    source: str = "linkedin_csv"


@dataclass
class LinkedinDiscoveryCompleted(StagePayload):
    source_csv: str = ""
    contacts_csv: str = ""
    contacts: int = 0
    source_user: str = ""
    updated_at: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    privacy: LinkedinPrivacy = field(default_factory=LinkedinPrivacy)
    status: str = "completed"
    source: str = "linkedin_csv"
