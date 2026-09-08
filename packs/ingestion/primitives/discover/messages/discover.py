#!/usr/bin/env python3
"""Discover iMessage and WhatsApp contact metadata.

Flow: explicit channel selection -> per-channel extract -> merge by phone
-> stage manifest. A blocked or failed channel stops discovery before merging.
The merged CSV contains only the selected channels' current exports; prior
matcher columns are not inputs. No message bodies, enrichment, or uploads.
Post-import review and enrichment belong to deep_context.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.messages.models import (  # noqa: E402
    MessageChannelBlocked,
    MessageChannelExtracted,
    MessageChannelFailed,
    MessageContactRow,
    MessagesDiscoveryCompleted,
    MessagesDiscoveryNotCompleted,
    MessagesDiscoverySkipped,
    MessagesPrivacy,
)
from packs.ingestion.primitives.common.jsonio import emit, now_iso, write_json  # noqa: E402
from packs.ingestion.primitives.common.paths import (  # noqa: E402
    MESSAGES_OUT_DIR,
    discover_source_dir,
)
from packs.ingestion.primitives.discover.common import (  # noqa: E402
    read_csv_rows,
    write_csv_rows,
)
from packs.ingestion.primitives.discover.messages.merge_contacts import ContactsMerger  # noqa: E402
from packs.ingestion.primitives.discover.messages.channels.message_channel_base import (  # noqa: E402
    MessageChannel,
    failed_child,
)
from packs.ingestion.primitives.discover.messages.channels.i_message_channel import (  # noqa: E402
    IMESSAGE_CONTACTS,
    IMessageChannel,
)
from packs.ingestion.primitives.discover.messages.channels.whats_app_channel import (  # noqa: E402
    DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
    WHATSAPP_CONTACTS,
    WhatsAppChannel,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node  # noqa: E402
from packs.ingestion.schemas.message_contacts import CSV_HEADERS  # noqa: E402


DEFAULT_MESSAGES_OUTPUT_DIR = discover_source_dir("messages")

# The shared messages scratch dir stays sourced from common/paths; the
# merged-contacts output paths live here (the per-channel fixed paths are owned
# by the channel modules under channels/).
MESSAGES_DIR = MESSAGES_OUT_DIR
MERGED_CONTACTS = MESSAGES_DIR / "contacts.csv"
MERGED_CONTACTS_MANIFEST = MESSAGES_DIR / "contacts.csv.manifest.json"


@dataclass(frozen=True)
class ChannelSelection:
    """Which message channels this run covers, parsed once from the CLI flags.

    The ``--include-*`` flags ARE the selection — there is no accounts.json
    fallback and no other source. ``linked`` is derived, not stored: it used to
    be a third key in a mutable dict alongside the two bools it is a function of,
    which is one more thing that can disagree with itself."""

    include_imessage: bool = False
    include_whatsapp: bool = False

    @property
    def linked(self) -> bool:
        """Whether any channel was selected. Neither -> the skipped path."""
        return self.include_imessage or self.include_whatsapp


# --- the store: owns the output dir, the run loop, the merge, the manifest ----

class MessagesDiscovery(Node):
    """Orchestrates a messages discovery run: creates the fixed output directory,
    runs each enabled channel (stopping at the first blocked/failed one), merges
    the per-channel CSVs, and writes the stage manifest. Holds all filesystem
    side effects so the channels stay pure."""

    name = "messages_stage_merge"
    # The two per-channel CSVs, which ARE the channel nodes' declared outputs.
    # required=False: a run may enable only one channel, and a channel that
    # produced nothing legitimately leaves its CSV absent.
    inputs = (
        Artifact(path=str(IMESSAGE_CONTACTS), row_model=MessageContactRow, required=False),
        Artifact(path=str(WHATSAPP_CONTACTS), row_model=MessageContactRow, required=False),
    )
    outputs = (
        Artifact(
            path=str(MERGED_CONTACTS),
            row_model=MessageContactRow,
            writes="full_rewrite",
        ),
    )
    payload = MessagesDiscoveryCompleted
    manifest = str(DEFAULT_MESSAGES_OUTPUT_DIR / "manifest.json")

    def __init__(
        self,
        *,
        out_dir: Path = DEFAULT_MESSAGES_OUTPUT_DIR,
        wacli_max_messages: int = DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
        include_imessage: bool = False,
        include_whatsapp: bool = False,
    ) -> None:
        # Channel selection is EXPLICIT: the --include-* flags ARE the selection
        # (no accounts.json fallback). Neither enabled -> the skipped manifest path.
        # Named `selection`, not `inputs`: `inputs` is now the declared Artifact tuple.
        self.selection = ChannelSelection(
            include_imessage=bool(include_imessage),
            include_whatsapp=bool(include_whatsapp),
        )
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)  # the one place the dir is created
        # This stage's ONE output (the merged, shared message-contacts CSV) as a
        # plain instance attribute, read from the module global once here so a test
        # that patches it still gets a store pointed at its own temp file.
        self.contacts_csv = MERGED_CONTACTS
        self.manifest_json = self.out_dir / "manifest.json"
        self.channels: list[MessageChannel] = []
        if self.selection.include_imessage:
            self.channels.append(IMessageChannel(
                other_enabled=self.selection.include_whatsapp))
        if self.selection.include_whatsapp:
            self.channels.append(WhatsAppChannel(
                other_enabled=self.selection.include_imessage,
                max_messages=wacli_max_messages))

    def bindings(self) -> dict[str, str]:
        """Declared path -> this instance's path, so an explicit ``out_dir`` (or a
        test that patches ``MERGED_CONTACTS``) still validates against the
        declaration. The KEYS come from the declaration itself, never from a
        second read of a module constant — a patched constant would otherwise
        produce keys no declared path matches, and the template would validate
        the unpatched `.powerpacks/` path instead."""
        bound = {
            self.outputs[0].path: str(self.contacts_csv),
            self.manifest: str(self.manifest_json),
        }
        # This store's declared inputs ARE the channels' declared outputs, so each
        # enabled channel supplies its own binding.
        for channel in self.channels:
            bound[channel.outputs[0].path] = str(channel.contacts_csv)
        return bound

    def execute(self) -> MessagesDiscoveryCompleted | MessagesDiscoveryNotCompleted | MessagesDiscoverySkipped:
        """Run the enabled channels (stop at the first blocked/failed child),
        merge, and return the typed payload (the Node template writes it).

        Each channel RETURNS what it produced; nothing is read back off the
        channel objects afterwards."""
        if not self.selection.linked:
            return MessagesDiscoverySkipped(
                reason="messages_not_linked",
                contacts_csv=str(self.contacts_csv),
                updated_at=now_iso(),
            )
        extracted: list[MessageChannelExtracted] = []
        for channel in self.channels:
            child = channel.run()
            if child.status != "completed":
                return self._not_completed(child)
            extracted.append(child)
        failed = self._merge()
        if failed is not None:
            return self._not_completed(failed)
        return self._completed(extracted)

    def _artifacts(self, extracted: list[MessageChannelExtracted]) -> dict[str, Any]:
        """Render the manifest's artifacts map from the channels' returns. Each
        channel names its own keys off its `channel` — `imessage_contacts_csv`,
        `whatsapp_contacts_csv`, `whatsapp_provider`, `whatsapp_pairing_*` — and
        contributes only the ones it actually has."""
        artifacts: dict[str, Any] = {}
        for item in extracted:
            artifacts[f"{item.channel}_contacts_csv"] = item.contacts_csv
            if item.provider:
                artifacts[f"{item.channel}_provider"] = item.provider
            if item.pairing_state:
                artifacts[f"{item.channel}_pairing_state"] = item.pairing_state
                artifacts[f"{item.channel}_pairing_notice"] = item.pairing_notice or ""
        return artifacts

    def _merge(self) -> MessageChannelFailed | None:
        """Union the enabled channels' contacts CSVs by canonical phone into
        MERGED_CONTACTS (via ``ContactsMerger`` in-process). Writes an empty
        merged CSV + manifest when no channel produced an export; returns a failed
        child on a non-``ok`` merge."""
        inputs = [channel.contacts_csv for channel in self.channels if channel.contacts_csv.exists()]
        if not inputs:
            write_csv_rows(MERGED_CONTACTS, CSV_HEADERS, [])
            write_json(MERGED_CONTACTS_MANIFEST, {
                "primitive": "messages/merge_contacts",
                "status": "ok",
                "reason": "no_channel_contact_exports_found",
                "artifacts": {"contacts_csv": str(MERGED_CONTACTS)},
                "counts": {"rows_written": 0, "unique_phones": 0, "cross_channel_phones": 0, "by_source": {}},
            })
            return None
        payload = ContactsMerger().merge(
            inputs=inputs, output=MERGED_CONTACTS, manifest=MERGED_CONTACTS_MANIFEST,
        )
        if payload.get("status") != "ok":
            return failed_child("ensure_contacts", payload, "")
        return None

    def _not_completed(
        self,
        child: MessageChannelBlocked | MessageChannelFailed,
    ) -> MessagesDiscoveryNotCompleted:
        """The not-completed stage payload for a blocked/failed child. Reads the
        child TYPED; its dict form is built once, where the manifest embeds it
        verbatim."""
        return MessagesDiscoveryNotCompleted(
            status=(child.status
                    if child.status in {"blocked_user_action", "blocked_approval"}
                    else "failed"),
            error=child.stage_error(),
            child=child.to_payload(),
            contacts_csv=str(self.contacts_csv),
            updated_at=now_iso(),
        )

    def _completed(self, extracted: list[MessageChannelExtracted]) -> MessagesDiscoveryCompleted:
        """Build the completed stage payload from the channels' returns plus the
        merged CSV (contact count, channels, privacy, pre-full-sync nudge)."""
        artifacts = self._artifacts(extracted)
        artifacts["contacts_csv"] = str(self.contacts_csv)
        child = {
            "primitive": "messages_discovery",
            "status": "selected_steps_completed",
            "message": "Selected message channels were extracted and merged.",
            "channels": {
                "imessage": self.selection.include_imessage,
                "whatsapp": self.selection.include_whatsapp,
            },
            "artifacts": artifacts,
            "privacy": {
                "message_bodies_read": False,
                "provider_research_ran": False,
                "cloud_upload_ran": False,
            },
        }
        _, rows = read_csv_rows(self.contacts_csv)
        # The pairing fields hoist the non-blocking pre-full-sync nudge to the top
        # level so a fast-path run surfaces it without digging into child.artifacts.
        nudge = next((item for item in extracted if item.pairing_state), None)
        return MessagesDiscoveryCompleted(
            contacts_csv=str(self.contacts_csv),
            contacts=len(rows),
            include_imessage=self.selection.include_imessage,
            include_whatsapp=self.selection.include_whatsapp,
            privacy=MessagesPrivacy(),
            child=child,
            updated_at=now_iso(),
            whatsapp_pairing_state=nudge.pairing_state if nudge else None,
            whatsapp_pairing_notice=(nudge.pairing_notice or "") if nudge else None,
        )


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse surface: the single `discover` subcommand with the wacli
    max-messages and the explicit --include-* channel selection (the flags ARE the
    selection; there is no --accounts file)."""
    parser = argparse.ArgumentParser(description="Discover iMessage/WhatsApp contacts")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("discover", help="Discover message contacts")
    run.add_argument("--wacli-max-messages", type=int, default=DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES)
    run.add_argument("--include-imessage", action="store_true")
    run.add_argument("--include-whatsapp", action="store_true")
    return parser


def main() -> int:
    """CLI dispatch: run the store and emit the typed payload's dict form; map
    status to the exit code (20 blocked, 1 failed, else 0)."""
    args = build_parser().parse_args()
    if args.command == "discover":
        payload = MessagesDiscovery(
            wacli_max_messages=args.wacli_max_messages,
            include_imessage=args.include_imessage,
            include_whatsapp=args.include_whatsapp,
        ).run()
        emit(payload.to_payload())
        if payload.status in {"blocked_user_action", "blocked_approval"}:
            return 20
        return 1 if payload.status == "failed" else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
