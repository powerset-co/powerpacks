"""WhatsAppChannel: WhatsAppWacli.run (fetch the pinned wacli binary, auth,
sync, export local metadata) -> normalize.

Owns its fixed output paths — the ``WHATSAPP_*`` module constants, assigned to
instance attributes in ``__init__`` — plus the wacli max-messages and sync
timeout defaults, which are channel-scoped. A missing
QR pairing returns ``blocked_user_action`` (with the QR page path); a completed
run surfaces the non-blocking pre-full-sync re-link nudge on ``self.artifacts``.
Metadata only — no message bodies.

Changelog:
  2026-07-23 (in-process): ``extract()`` now calls ``WhatsAppWacli().run(...)``
    in-process instead of spawning ``whatsapp_wacli.py run``; branches on the
    returned payload's ``status`` (blocked_user_action -> blocked, non-completed
    -> failed). ``run_cmd``/``py_cmd`` are no longer imported, and the outer
    subprocess-timeout constant ``DEFAULT_WACLI_DEPTH_TIMEOUT`` is gone (the wacli
    phases keep their own internal timeouts; there is no outer child process to
    cap). Behavior, fixed output paths, and payload shapes unchanged.
  2026-07-23 (channels split): moved out of messages/discover.py into channels/;
    the WhatsApp-owned ``WHATSAPP_*`` path constants and the wacli
    max-messages / sync / depth timeout defaults moved here with it. The QR page
    path is derived from the shared scratch dir (common/paths' ``MESSAGES_OUT_DIR``).
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
from packs.ingestion.primitives.discover.messages.whatsapp_wacli import (  # noqa: E402
    WhatsAppWacli,
)
from packs.ingestion.primitives.discover.messages.channels.message_channel_base import (  # noqa: E402
    MessageChannel,
    blocked_child,
    failed_child,
)


DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES = 0
# First full backfill scales with history size (~3-year default window):
# ~30 minutes on small accounts, a few hours on large ones. 3h hard cap. Passed
# through as the wacli sync-phase timeout.
DEFAULT_WACLI_SYNC_TIMEOUT = 10800

# Fixed per-stage output paths owned by the WhatsApp channel (stable path ->
# idempotent reruns). The channel assigns these to instance attributes in __init__.
WHATSAPP_CONTACTS = MESSAGES_OUT_DIR / "whatsapp.contacts.csv"
WHATSAPP_RAW_JSONL = MESSAGES_OUT_DIR / "whatsapp.contacts.raw.jsonl"
WHATSAPP_MANIFEST = MESSAGES_OUT_DIR / "whatsapp.contacts.csv.manifest.json"
WHATSAPP_PROGRESS_JSONL = MESSAGES_OUT_DIR / "whatsapp.contacts.csv.manifest.json.progress.jsonl"
WHATSAPP_NORMALIZED_JSONL = MESSAGES_OUT_DIR / "whatsapp.contacts.normalized.jsonl"
WHATSAPP_NORMALIZED_MANIFEST = MESSAGES_OUT_DIR / "whatsapp.contacts.normalized.jsonl.manifest.json"


class WhatsAppChannel(MessageChannel):
    name = "whatsapp"

    def __init__(
        self,
        *,
        accounts_path: Path,
        other_enabled: bool,
        max_messages: int = DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
    ) -> None:
        super().__init__(accounts_path=accounts_path, other_enabled=other_enabled)
        self.max_messages = max_messages
        self.contacts_csv = WHATSAPP_CONTACTS
        self.normalized_jsonl = WHATSAPP_NORMALIZED_JSONL
        self.normalized_manifest = WHATSAPP_NORMALIZED_MANIFEST

    def extract(self) -> dict[str, Any] | None:
        payload = WhatsAppWacli().run(
            output_csv=WHATSAPP_CONTACTS,
            output_jsonl=WHATSAPP_RAW_JSONL,
            manifest=WHATSAPP_MANIFEST,
            progress_jsonl=WHATSAPP_PROGRESS_JSONL,
            max_messages=self.max_messages,
            max_group_participants=30,
            sync_timeout=DEFAULT_WACLI_SYNC_TIMEOUT,
        )
        if payload.get("status") == "blocked_user_action":
            return blocked_child(
                message=str(payload.get("message") or "WhatsApp needs a QR scan."),
                accounts_path=self.accounts_path,
                detail=payload,
                whatsapp_provider="wacli",
                qr_page=str(payload.get("qr_page") or MESSAGES_OUT_DIR / "wacli-login-qr.html"),
                include_imessage=self.other_enabled,
                include_whatsapp=True,
            )
        if payload.get("status") != "completed":
            return failed_child("extract_whatsapp", payload, "")
        self.artifacts["whatsapp_contacts_csv"] = str(WHATSAPP_CONTACTS)
        self.artifacts["whatsapp_provider"] = "wacli"
        # Surface the non-blocking "re-link for deeper history" nudge to the skill
        # when the WhatsApp session predates full history sync.
        pairing = payload.get("pairing") if isinstance(payload.get("pairing"), dict) else {}
        if pairing.get("state") == "pre_full_sync":
            self.artifacts["whatsapp_pairing_state"] = "pre_full_sync"
            self.artifacts["whatsapp_pairing_notice"] = str(pairing.get("hint") or "")
        return None
