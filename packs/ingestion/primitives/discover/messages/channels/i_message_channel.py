"""IMessageChannel: the declared `messages_imessage_extract` node.

Owns its fixed output paths — the ``IMESSAGE_*`` module constants, assigned to
instance attributes in ``__init__``. ``execute()`` calls
``IMessageExtractor().check(strict=True)`` in-process (the macOS Full Disk Access
/ Contacts gate; a non-``ok`` status returns ``blocked_user_action``) then
``.extract(...)``, writing ``imessage.contacts.csv`` + raw jsonl + manifest, and
returns the CSV it produced. Metadata only — never selects message body columns.

Declared contract:
  reads   ``~/Library/Messages/chat.db`` — external (macOS owns it) and
          ``required=False`` ON PURPOSE. Under a Full Disk Access denial the file
          still EXISTS and ``os.access`` still returns true; TCC refuses at open.
          A ``required`` input would therefore not catch the real failure, and
          would replace a ``blocked_user_action`` payload that tells the user
          which System Settings pane to open with a bare ``not_ready``. The gate
          is ``IMessageExtractor.check(strict=True)``, not the declaration.
          The AddressBook databases are NOT declared: that input is a GLOB over
          ``Sources/*/AddressBook-v22.abcddb``, and an Artifact path is one file.
  writes  ``imessage.contacts.csv`` — the whole file, all 19 columns, sole writer.

NOT declared, deliberately: ``imessage.contacts.raw.jsonl`` and
``imessage.manifest.json``. The manifest is the leaf extractor's own. The raw
JSONL has ZERO readers repo-wide (grep-verified) — it is a dead output, so the
declared graph simply ignores it rather than modelling its deadness. Deleting it
means dropping ``--output-jsonl`` from a documented CLI, which is its own cut.

Changelog:
  2026-07-30 (steps return results): ``extract()`` became ``execute()`` and
    returns ``MessageChannelExtracted(channel, contacts_csv)`` instead of
    returning ``None`` and stashing ``imessage_contacts_csv`` in a
    ``self.artifacts`` dict for the store to read back. Same manifest key, same
    value.
  2026-07-25 (declared contract): now a ``(MessageChannel, Node)`` — declares the
    node ``messages_imessage_extract`` with its chat.db input and contacts.csv
    output, and ``extract()`` returns the typed channel payloads. The extractor
    call reads the fixed paths off ``self`` instead of the module globals (same
    values: ``__init__`` resolves them from those globals).
  2026-07-25 (normalize deleted): dropped ``IMESSAGE_NORMALIZED_JSONL`` /
    ``IMESSAGE_NORMALIZED_MANIFEST`` with the dead normalize step.
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

# Repo-root bootstrap so `packs.*` imports work in module AND script mode.
_REPO_ROOT = Path(__file__).resolve().parents[6]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.paths import MESSAGES_OUT_DIR  # noqa: E402
from packs.ingestion.primitives.discover.messages.extract_imessage import (  # noqa: E402
    DEFAULT_CHAT_DB,
    IMessageExtractor,
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
from packs.ingestion.primitives.pipeline.contract import Artifact, Node  # noqa: E402


# Fixed per-stage output paths owned by the iMessage channel (the durable
# contract: a stable path -> idempotent reruns). The channel assigns these to
# instance attributes in __init__; the shared scratch dir is common/paths'.
IMESSAGE_CONTACTS = MESSAGES_OUT_DIR / "imessage.contacts.csv"
IMESSAGE_RAW_JSONL = MESSAGES_OUT_DIR / "imessage.contacts.raw.jsonl"
IMESSAGE_MANIFEST = MESSAGES_OUT_DIR / "imessage.manifest.json"


class IMessageChannel(MessageChannel, Node):
    channel = "imessage"

    name = "messages_imessage_extract"
    inputs = (Artifact(path=str(DEFAULT_CHAT_DB), external=True, required=False),)
    outputs = (
        Artifact(path=str(IMESSAGE_CONTACTS), row_model=MessageContactRow, writes="full_rewrite"),
    )
    payload = MessageChannelExtracted
    # "" — this node has no manifest.json of its own; it reports into the
    # MessagesDiscovery stage manifest through the payload it RETURNS.
    manifest = ""

    def __init__(self, *, other_enabled: bool) -> None:
        super().__init__(other_enabled=other_enabled)
        self.contacts_csv = IMESSAGE_CONTACTS
        self.raw_jsonl = IMESSAGE_RAW_JSONL
        self.extract_manifest = IMESSAGE_MANIFEST

    def bindings(self) -> dict[str, str]:
        """Declared path -> this instance's path. The key comes from the
        DECLARATION, never a second read of the module constant, so a test that
        patches ``IMESSAGE_CONTACTS`` still produces a key the template matches."""
        return {self.outputs[0].path: str(self.contacts_csv)}

    def execute(self) -> MessageChannelExtracted | MessageChannelBlocked | MessageChannelFailed:
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
            output_csv=self.contacts_csv,
            output_jsonl=self.raw_jsonl,
            manifest=self.extract_manifest,
        )
        if result.get("status") != "completed":
            return failed_child("extract_imessage", result, "")
        return MessageChannelExtracted(channel=self.channel, contacts_csv=str(self.contacts_csv))
