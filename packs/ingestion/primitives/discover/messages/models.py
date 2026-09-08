"""Message-contact row schema and typed discovery results."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.pipeline.contract import (  # noqa: E402
    StageManifest,
    row_model_for,
)
from packs.ingestion.schemas.message_contacts import CSV_HEADERS  # noqa: E402


MessageContactRow = row_model_for("MessageContactRow", CSV_HEADERS)

class MessagesPrivacy(BaseModel):
    """The privacy assertions every messages-discovery manifest carries: this
    stage reads contact METADATA only — never message bodies — and never
    researches, reviews, or uploads."""

    message_bodies_read: bool = False
    powerset_sync_ran: bool = False
    llm_review_ran: bool = False
    deep_research_ran: bool = False
    upload_ran: bool = False


# --- channel payloads (one MessageChannel node's execute() result) ------------

class MessageChannelExtracted(StageManifest):
    """A channel extracted cleanly, and what it contributed.

    Not persisted anywhere: the channel nodes declare `manifest = ""`, so this is
    the store's run-loop signal AND the channel's report of what it produced. The
    store renders the stage manifest's `artifacts` map from these returns
    (`<channel>_contacts_csv`, `<channel>_provider`, `<channel>_pairing_*`) —
    the channels no longer write those keys into a dict the store reads back.

    `provider` is the backing client a channel went through (WhatsApp: `wacli`);
    the `pairing_*` pair is the non-blocking "re-link for deeper history" nudge,
    set only when the channel decides the nudge applies."""

    status: str = "completed"
    channel: str = ""
    contacts_csv: str = ""
    provider: str | None = None
    pairing_state: str | None = None
    pairing_notice: str | None = None


class MessageChannelBlocked(StageManifest):
    """A channel needs a user action (macOS Full Disk Access, a WhatsApp QR
    scan). `whatsapp_provider` / `qr_page` / `detail` are `| None` so they vanish
    from the payload when unset, matching the dict builder this replaced."""

    primitive: str = "messages_discovery"
    status: str = "blocked_user_action"
    message: str = ""
    detail: Any = None
    whatsapp_provider: str | None = None
    qr_page: str | None = None
    continue_command: str = ""

    def stage_error(self) -> Any:
        """The `error` the stage payload reports for this child. A blocked child
        carries no `error` of its own, so its message is the error text; a child
        with neither falls back to its whole payload."""
        return self.message or self.to_payload()


class MessageChannelFailed(StageManifest):
    """A channel's extract step (or the store's merge) failed."""

    primitive: str = "messages_discovery"
    status: str = "failed"
    step_id: str = ""
    error: Any = None

    def stage_error(self) -> Any:
        """The `error` the stage payload reports for this child (its own error
        text, or its whole payload when it has none)."""
        return self.error or self.to_payload()


# --- stage payloads (the MessagesDiscovery store's manifest) ------------------

class MessagesDiscoverySkipped(StageManifest):
    reason: str = ""
    contacts_csv: str = ""
    updated_at: str = ""
    status: str = "skipped"
    source: str = "messages"


class MessagesDiscoveryNotCompleted(StageManifest):
    """A child step failed or blocked (user action / approval)."""
    error: Any = None
    child: Any = None
    contacts_csv: str = ""
    updated_at: str = ""
    status: str = "failed"
    source: str = "messages"


class MessagesDiscoveryCompleted(StageManifest):
    contacts_csv: str = ""
    contacts: int = 0
    include_imessage: bool = False
    include_whatsapp: bool = False
    privacy: MessagesPrivacy = MessagesPrivacy()
    child: Any = None
    updated_at: str = ""
    # Non-blocking pre-full-sync nudge, surfaced at top level when present.
    whatsapp_pairing_state: str | None = None
    whatsapp_pairing_notice: str | None = None
    status: str = "completed"
    source: str = "messages"
