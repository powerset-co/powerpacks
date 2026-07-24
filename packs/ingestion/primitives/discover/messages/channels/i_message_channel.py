"""IMessageChannel: iMessage extract (Full Disk Access gated) -> normalize.

Owns its fixed output paths — the ``IMESSAGE_*`` module constants, assigned to
instance attributes in ``__init__``. ``extract()`` calls
``IMessageExtractor().check(strict=True)`` in-process (the macOS Full Disk Access
/ Contacts gate; a non-``ok`` status returns ``blocked_user_action``) then
``.extract(...)``, writing ``imessage.contacts.csv`` + raw jsonl + manifest; the
inherited ``normalize()`` turns the CSV into canonical JSONL. Metadata only —
never selects message body columns.

Changelog:
  2026-07-23 (explicit-selection): dropped the ``accounts_path`` constructor
    parameter — the Full Disk Access ``blocked_child`` no longer threads it (the
    continue command is rebuilt from the include flags alone). Behavior otherwise
    unchanged.
  2026-07-23 (in-process): ``extract()`` now calls the ``IMessageExtractor`` class
    in-process (``check`` then ``extract``) instead of spawning
    ``extract_imessage.py`` as a subprocess; branches on the returned payload's
    ``status`` (non-``ok`` check -> blocked, non-``completed`` extract -> failed).
    ``run_cmd``/``py_cmd`` are no longer imported here. Behavior, fixed output
    paths, and payload shapes unchanged.
  2026-07-23 (channels split): moved out of messages/discover.py into channels/;
    the iMessage-owned ``IMESSAGE_*`` path constants moved here with it. Shared
    ``MESSAGES_DIR`` stays sourced from common/paths (``MESSAGES_OUT_DIR``).
    Behavior and fixed output paths unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.paths import MESSAGES_OUT_DIR  # noqa: E402
from packs.ingestion.primitives.discover.messages.extract_imessage import (  # noqa: E402
    IMessageExtractor,
)
from packs.ingestion.primitives.discover.messages.channels.message_channel_base import (  # noqa: E402
    MessageChannel,
    blocked_child,
    failed_child,
)


# Fixed per-stage output paths owned by the iMessage channel (the durable
# contract: a stable path -> idempotent reruns). The channel assigns these to
# instance attributes in __init__; the shared scratch dir is common/paths'.
IMESSAGE_CONTACTS = MESSAGES_OUT_DIR / "imessage.contacts.csv"
IMESSAGE_RAW_JSONL = MESSAGES_OUT_DIR / "imessage.contacts.raw.jsonl"
IMESSAGE_MANIFEST = MESSAGES_OUT_DIR / "imessage.manifest.json"
IMESSAGE_NORMALIZED_JSONL = MESSAGES_OUT_DIR / "imessage.contacts.normalized.jsonl"
IMESSAGE_NORMALIZED_MANIFEST = MESSAGES_OUT_DIR / "imessage.contacts.normalized.jsonl.manifest.json"


class IMessageChannel(MessageChannel):
    name = "imessage"

    def __init__(self, *, other_enabled: bool) -> None:
        super().__init__(other_enabled=other_enabled)
        self.contacts_csv = IMESSAGE_CONTACTS
        self.normalized_jsonl = IMESSAGE_NORMALIZED_JSONL
        self.normalized_manifest = IMESSAGE_NORMALIZED_MANIFEST

    def extract(self) -> dict[str, Any] | None:
        extractor = IMessageExtractor()
        check = extractor.check(strict=True)
        if check.get("status") != "ok":
            return blocked_child(
                message="Enable macOS Full Disk Access / Contacts access for this terminal, then continue.",
                detail=check,
                include_imessage=True,
                include_whatsapp=self.other_enabled,
            )
        result = extractor.extract(
            output_csv=IMESSAGE_CONTACTS,
            output_jsonl=IMESSAGE_RAW_JSONL,
            manifest=IMESSAGE_MANIFEST,
        )
        if result.get("status") != "completed":
            return failed_child("extract_imessage", result, "")
        self.artifacts["imessage_contacts_csv"] = str(IMESSAGE_CONTACTS)
        return None
