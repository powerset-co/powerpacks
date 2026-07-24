"""Typed stage-manifest payloads for messages discovery."""

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
class MessagesPrivacy:
    message_bodies_read: bool = False
    powerset_sync_ran: bool = False
    llm_review_ran: bool = False
    deep_research_ran: bool = False
    upload_ran: bool = False


@dataclass
class MessagesDiscoverySkipped(StagePayload):
    reason: str = ""
    contacts_csv: str = ""
    updated_at: str = ""
    status: str = "skipped"
    source: str = "messages"


@dataclass
class MessagesDiscoveryNotCompleted(StagePayload):
    """A child step failed or blocked (user action / approval)."""
    error: Any = None
    child: Any = None
    contacts_csv: str = ""
    updated_at: str = ""
    status: str = "failed"
    source: str = "messages"


@dataclass
class MessagesDiscoveryCompleted(StagePayload):
    contacts_csv: str = ""
    contacts: int = 0
    include_imessage: bool = False
    include_whatsapp: bool = False
    privacy: MessagesPrivacy = field(default_factory=MessagesPrivacy)
    child: Any = None
    updated_at: str = ""
    # Non-blocking pre-full-sync nudge, surfaced at top level when present.
    whatsapp_pairing_state: str | None = None
    whatsapp_pairing_notice: str | None = None
    status: str = "completed"
    source: str = "messages"
