"""The message-contact ROW model and the typed payloads for messages discovery.

Three things:

  MessageContactRow  the 19-column `contacts.csv` row model, generated FROM
                     `schemas/message_contacts.py`'s `CSV_HEADERS` so the column
                     list still has exactly one home. `contacts.csv` has TWO
                     writers (this stage and the import matcher) and
                     `graph.check_graph` compares row models by IDENTITY, so both
                     writers must import THIS object, not an equal copy.
  channel payloads   what one MessageChannel node returns from `execute()`:
                     `MessageChannelExtracted` on success, or the
                     `MessageChannelBlocked` / `MessageChannelFailed` shapes that
                     short-circuit the store's run loop. These are the pydantic
                     form of the dicts `blocked_child` / `failed_child` used to
                     build by hand; the optional fields are `| None` so
                     `exclude_none` drops exactly the keys the old
                     `value not in (None, "")` filter dropped.
  stage payloads     what the `MessagesDiscovery` store writes into
                     `discover/messages/manifest.json`.

Changelog:
  2026-07-25 (declared contract): ported from `StagePayload` dataclasses to the
    pydantic `StageManifest` of `pipeline/contract.py`, added `MessageContactRow`
    and the three channel payloads. Field names, defaults, and declaration order
    are unchanged, so the manifest JSON is unchanged (`write_json` sorts keys
    anyway); the only behavioral difference is `extra="forbid"`, which is the
    point.
"""

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

# The columns of `.powerpacks/messages/contacts.csv` whose VALUES this stage
# computes — which is what `Artifact.owns_columns` means. Not "the columns it
# emits": the extractors emit all 19 (see `extract_imessage.contact_to_csv_row`
# and its WhatsApp twin, which both write literal `""` for the match block), and
# `merge_contacts` reads and re-emits the match block through `_better_match`,
# whose whole job is to RANK and PRESERVE match values it did not produce. That
# is a pass-through, not ownership.
#
# The complementary 8 — match_status, matched_person_id, matched_name,
# matched_linkedin_url, match_confidence, match_method, match_reason — are the
# import matcher's (imports/messages/match_local_candidates.py), declared on its
# own node.
#
# `skip` is owned by NEITHER, and that is a finding, not an oversight. Grepped
# repo-wide: every producer writes it empty (`extract_imessage`/`extract_whatsapp`
# write `""`, `merge_contacts` ORs it across channels and writes `"yes"` only if
# some input already said so) and nothing in the pipeline ever sets it true. On
# real local data all 873 merged rows carry `""`. Its only READER is
# `imports/messages/util.py`, which floors a contact out of the import when it is
# truthy. So `skip` is a USER-owned column — hand-edited in the CSV to exclude a
# contact — that discovery deliberately carries through. Claiming it here would
# assert this stage may overwrite a user's decision.
DISCOVERY_OWNED_COLUMNS = (
    "phone",
    "name",
    "source",
    "is_in_group_chats",
    "group_names",
    "message_count",
    "imessage_message_count",
    "whatsapp_message_count",
    "last_message",
    "imessage_last_message",
    "whatsapp_last_message",
)


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
    """A channel extracted cleanly. Not persisted anywhere: the channel nodes
    declare `manifest = ""` (they report into the store's stage manifest, via
    `self.artifacts`), so this is only the store's run-loop signal. Its
    predecessor was a bare `None`."""

    status: str = "completed"
    channel: str = ""
    contacts_csv: str = ""


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


class MessageChannelFailed(StageManifest):
    """A channel's extract step (or the store's merge) failed."""

    primitive: str = "messages_discovery"
    status: str = "failed"
    step_id: str = ""
    error: Any = None


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
