"""WhatsAppChannel: the declared `messages_whatsapp_extract` node.

``WhatsAppExtractor.run`` fetches the pinned wacli binary, authenticates, syncs,
deepens recent history, then exports local metadata. This channel owns its fixed
output paths — the ``WHATSAPP_*`` module constants, assigned to instance
attributes in ``__init__`` — plus the wacli max-messages and sync timeout
defaults, which are channel-scoped. The extractor's payload is parsed ONCE into
``WhatsAppExtractResult`` (defined beside the extractor that produces it), so
this channel only branches on typed fields: a missing QR pairing returns
``blocked_user_action`` (with the QR page path); a completed run returns the
non-blocking pre-full-sync re-link nudge on its payload. Metadata only — no
message bodies.

Declared contract:
  reads   ``.powerpacks/messages/wacli/wacli.db`` — the wacli GO BINARY's own
          SQLite store: ``external=True`` (no node in the graph produces it) and
          ``required=False`` (absent until the first successful pairing + sync,
          which is exactly the blocked path this node reports). That is the ONE
          input: wacli's contact store is the name authority.
  writes  ``whatsapp.contacts.csv`` — the whole file, all 19 columns, sole writer.

NOT declared, deliberately: ``whatsapp.contacts.raw.jsonl``,
``whatsapp.contacts.csv.manifest.json`` and its progress JSONL. The manifest and
progress file are the leaf extractor's own. The raw JSONL has ZERO readers
repo-wide (grep-verified) — a dead output the declared graph ignores rather than
models.

Note (not fixed here): ``extract_whatsapp.py``'s own CLI defaults write
``wacli.contacts.csv``, NOT the ``whatsapp.contacts.csv`` this channel passes. A
hand-run of that CLI therefore leaves an orphan copy in
``.powerpacks/messages/`` that nothing reads.

Changelog:
  2026-07-30 (steps return results / parse at the boundary): ``extract()`` became
    ``execute()`` and returns ``MessageChannelExtracted`` carrying
    ``provider="wacli"`` and the pairing nudge, instead of returning ``None``
    after writing four ``whatsapp_*`` keys into a ``self.artifacts`` dict the
    store read back. The extractor payload is unwrapped once through
    ``WhatsAppExtractResult.from_payload`` — the ``isinstance(payload.get(
    "pairing"), dict)`` guard and the ``str(payload.get(...) or ...)`` chains are
    gone from this step, and the ``pre_full_sync`` test now reads as the one
    policy decision it is. Same manifest keys and values.
  2026-07-26 (feedback edge removed): DROPPED the ``name_fallback_csv`` input. It
    was the merged ``.powerpacks/messages/contacts.csv`` this channel FEEDS — the
    graph's WhatsApp cycle — and it was redundant: it only fills names wacli did
    not supply, the downstream merge already unions names across channels, and on
    real local data it supplied 0 of the 90 named WhatsApp contacts.
    ``extract_whatsapp`` no longer defaults to it (see its Changelog), so this
    channel reads the wacli store alone.
  2026-07-25 (declared contract): now a ``(MessageChannel, Node)`` — declares the
    node ``messages_whatsapp_extract`` with its wacli-store and name-fallback
    inputs and its contacts.csv output, and ``extract()`` returns the typed
    channel payloads. The extractor call reads the fixed paths off ``self``
    instead of the module globals (same values: ``__init__`` resolves them from
    those globals).
  2026-07-25 (normalize deleted): dropped ``WHATSAPP_NORMALIZED_JSONL`` /
    ``WHATSAPP_NORMALIZED_MANIFEST`` with the dead normalize step.
  2026-07-23 (explicit-selection): dropped the ``accounts_path`` constructor
    parameter — the QR-scan ``blocked_child`` no longer threads it (the continue
    command is rebuilt from the include flags alone). Behavior otherwise unchanged.
  2026-07-23 (extractor split): ``extract()`` now calls
    ``WhatsAppExtractor().run(...)`` (imported from ``extract_whatsapp``) instead
    of ``WhatsAppWacli().run(...)`` — the WhatsApp discovery orchestrator was
    renamed and moved out of ``whatsapp_wacli.py`` into ``extract_whatsapp.py``
    (parallel to ``extract_imessage``). Behavior, branches, fixed output paths,
    and payload shapes unchanged.
  2026-07-23 (in-process): ``extract()`` calls the extractor class ``run(...)``
    in-process instead of spawning a file; branches on the returned payload's
    ``status`` (blocked_user_action -> blocked, non-completed -> failed).
    ``run_cmd``/``py_cmd`` are no longer imported, and the outer
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

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.paths import MESSAGES_OUT_DIR  # noqa: E402
from packs.ingestion.primitives.discover.messages.extract_whatsapp import (  # noqa: E402
    WhatsAppExtractor,
    WhatsAppExtractResult,
)
from packs.ingestion.primitives.discover.messages.models import (  # noqa: E402
    MessageChannelBlocked,
    MessageChannelExtracted,
    MessageChannelFailed,
    MessageContactRow,
)
from packs.ingestion.primitives.discover.messages.channels.message_channel_base import (  # noqa: E402
    MessageChannel,
    blocked_child,
    failed_child,
)
from packs.ingestion.primitives.discover.messages.whatsapp_wacli import DEFAULT_STORE  # noqa: E402
from packs.ingestion.primitives.pipeline.contract import Artifact, Node  # noqa: E402


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

# The wacli GO BINARY's own SQLite store — the external input this node reads
# through the client in whatsapp_wacli.py (see open_wacli_db).
WACLI_DB = DEFAULT_STORE / "wacli.db"


class WhatsAppChannel(MessageChannel, Node):
    channel = "whatsapp"

    name = "messages_whatsapp_extract"
    inputs = (Artifact(path=str(WACLI_DB), external=True, required=False),)
    outputs = (
        Artifact(path=str(WHATSAPP_CONTACTS), row_model=MessageContactRow, writes="full_rewrite"),
    )
    payload = MessageChannelExtracted
    # "" — this node has no manifest.json of its own; it reports into the
    # MessagesDiscovery stage manifest through the payload it RETURNS.
    manifest = ""

    def __init__(
        self,
        *,
        other_enabled: bool,
        max_messages: int = DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
    ) -> None:
        super().__init__(other_enabled=other_enabled)
        self.max_messages = max_messages
        self.contacts_csv = WHATSAPP_CONTACTS
        self.raw_jsonl = WHATSAPP_RAW_JSONL
        self.extract_manifest = WHATSAPP_MANIFEST
        self.progress_jsonl = WHATSAPP_PROGRESS_JSONL

    def bindings(self) -> dict[str, str]:
        """Declared path -> this instance's path. The key comes from the
        DECLARATION, never a second read of the module constant, so a test that
        patches ``WHATSAPP_CONTACTS`` still produces a key the template matches."""
        return {self.outputs[0].path: str(self.contacts_csv)}

    def execute(self) -> MessageChannelExtracted | MessageChannelBlocked | MessageChannelFailed:
        result = WhatsAppExtractResult.from_payload(WhatsAppExtractor().run(
            output_csv=self.contacts_csv,
            output_jsonl=self.raw_jsonl,
            manifest=self.extract_manifest,
            progress_jsonl=self.progress_jsonl,
            max_messages=self.max_messages,
            max_group_participants=30,
            sync_timeout=DEFAULT_WACLI_SYNC_TIMEOUT,
        ))
        if result.status == "blocked_user_action":
            return blocked_child(
                message=result.message or "WhatsApp needs a QR scan.",
                detail=result.raw,
                whatsapp_provider="wacli",
                qr_page=result.qr_page or str(MESSAGES_OUT_DIR / "wacli-login-qr.html"),
                include_imessage=self.other_enabled,
                include_whatsapp=True,
            )
        if result.status != "completed":
            return failed_child("extract_whatsapp", result.raw, "")
        # The one pairing DECISION this channel makes: only a session that
        # predates full history sync earns the non-blocking "re-link for deeper
        # history" nudge. Every other pairing state contributes nothing.
        nudge = result.pairing_state == "pre_full_sync"
        return MessageChannelExtracted(
            channel=self.channel,
            contacts_csv=str(self.contacts_csv),
            provider="wacli",
            pairing_state=result.pairing_state if nudge else None,
            pairing_notice=result.pairing_hint if nudge else None,
        )
