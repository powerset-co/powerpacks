#!/usr/bin/env python3
"""Discover iMessage and WhatsApp contact metadata.

This module owns only local metadata discovery. Review, LinkedIn profile
materialization, and enrichment live in imports/messages/importer.py.

Shape:
  MessagesDiscovery(include_imessage=..., include_whatsapp=...) is the whole
  thing: channel selection is EXPLICIT — the --include-* flags ARE the selection,
  with no accounts.json fallback. Neither enabled -> the skipped/messages_not_linked
  manifest path (mirrors gmail's empty-selection -> skipped). The constructor
  creates the fixed output dir and builds the enabled channels; .run() (the Node
  template: validate declared inputs -> execute() -> validate declared outputs ->
  manifest) extracts each channel, merges, and writes the stage manifest. main()
  constructs it and calls run() — no wrapper function.

  Each source is a MessageChannel node (channels/) that owns its own output paths
  and its in-process call into the leaf extractor class:
    - IMessageChannel (channels/i_message_channel.py): extract_imessage.py check
      (Full Disk Access gate) -> extract chat.db + AddressBook metadata ->
      imessage.contacts.csv.
    - WhatsAppChannel (channels/whats_app_channel.py): WhatsAppExtractor.run
      (extract_whatsapp.py, composing the whatsapp_wacli client: fetch pinned
      wacli, auth, sync, deepen, export local metadata) ->
      whatsapp.contacts.csv. Missing QR -> blocked_user_action; surfaces the
      pre-full-sync re-link nudge.
  A channel's run() returns its payload body; anything but ``completed``
  short-circuits the discovery run. MessagesDiscovery then merges the enabled
  per-channel CSVs by canonical phone -> .powerpacks/messages/contacts.csv,
  copies it to discover/messages/contacts.csv, and writes a typed manifest
  (contact count, channels, privacy=bodies-never-read, WhatsApp pre-full-sync
  nudge). Metadata only: no bodies, no research, no upload.

Flow:
  --include-* selection -> per-channel extract (stop at the first blocked/failed)
  -> merge by canonical phone -> copy to the staged discover dir -> one manifest

Known behaviors this stage DECLARES rather than fixes:
  - ``_merge`` feeds the merger only the per-channel CSVs, never the prior merged
    ``contacts.csv``. So every rerun BLANKS the 8 import-matcher columns, even
    though ``merge_contacts._better_match`` exists precisely to rank and preserve
    them. Adding the prior merged file to the merger's inputs would make the
    round trip lossless. Out of scope here.
  - A FAILED iMessage extract truncates a previously-good
    ``imessage.contacts.csv``: ``IMessageExtractor.extract`` writes empty
    artifacts on its failure path before returning the failure manifest. Noted,
    not fixed here.
  - ``--include-imessage`` alone silently DROPS WhatsApp rows from the merged
    output: ``_merge`` passes only the enabled channels' CSVs and the merger
    rewrites the whole file from them. The merged CSV is the union of the
    channels selected on THIS run, not of everything ever discovered.

Changelog:
  2026-07-25 (declared contract): ``MessagesDiscovery`` is a
    ``pipeline/contract.py:Node`` (``messages_stage_merge``), as are the two
    channels. It DECLARES the two per-channel CSVs it reads and the two CSVs it
    writes, ``run()`` is the inherited template, and the manifest payloads moved
    from ``StagePayload`` dataclasses to pydantic (models.py). The shared
    ``.powerpacks/messages/contacts.csv`` is declared with ``owns_columns`` =
    the 11 columns whose VALUES this stage computes; the import matcher owns the
    8 ``match_*`` columns and ``skip`` is owned by NEITHER (see
    DISCOVERY_OWNED_COLUMNS in models.py). The constructor's ``self.inputs``
    selection dict was renamed ``self.selection`` — ``inputs`` is now the
    declared Artifact tuple.
  2026-07-25 (normalize deleted): the per-channel normalize step and
    ``normalize_contacts.py`` are gone; its ``*.contacts.normalized.jsonl`` was
    byte-identical to the extractors' raw JSONL and had zero readers.
  2026-07-23 (explicit-selection): channel selection is now EXPLICIT --include-*
    only, mirroring gmail's --account-email model. Dropped the accounts_file
    parameter, the --accounts CLI argument, and the messages_discovery_inputs
    function (the accounts.json linkage read via channel_is_linked — now verified
    dead since nothing writes the messages channel status to accounts.json and
    $import-messages always passes --include-*). ``linked`` is just
    ``include_imessage or include_whatsapp``; channels are constructed without
    accounts_path, and the blocked/QR continue command drops --accounts. The
    now-unused account_config/channel_is_linked/read_accounts/DEFAULT_ACCOUNTS
    imports were removed; channel_is_linked was deleted from discover/common.py.
  2026-07-23 (in-process): MessagesDiscovery._merge now calls
    ``ContactsMerger().merge(...)`` in-process instead of spawning
    merge_contacts.py; the channels likewise call their leaf primitive classes
    directly. No self-owned Python file is spawned as a subprocess anymore.
    ``run_cmd``/``py_cmd`` are no longer imported here. Fixed output paths,
    manifests, and the CLI are unchanged.
  2026-07-23 (terse): folded the resolve()/discover() wrapper functions into
    MessagesDiscovery — the constructor now resolves channels itself, so callers
    just construct and run(). out_dir is a plain default arg; tests pass it
    explicitly.
  2026-07-23 (channels split): MessageChannel + blocked_child/failed_child moved
    to channels/message_channel_base.py, and IMessageChannel/WhatsAppChannel with
    their owned IMESSAGE_*/WHATSAPP_* path constants (and the wacli
    max-messages/sync/depth defaults) moved to channels/i_message_channel.py /
    channels/whats_app_channel.py — channels own their paths.
  2026-07-23 (oop): the per-channel extract free functions and the mutated
    `artifacts` dict + `child is None` chain were replaced by MessageChannel
    classes and a MessagesDiscovery store that owns the output dir, run loop,
    merge, and manifest.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.discover.messages.models import (  # noqa: E402
    DISCOVERY_OWNED_COLUMNS,
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
        # The SHARED file: this stage and the import matcher both write it.
        # `writes="upsert"` + `owns_columns` is what makes that legal — a
        # `full_rewrite` beside any other writer is a two-writer conflict.
        # Caveat, declared honestly: the merger physically rewrites the whole
        # file. It is an upsert only in the sense that it recomputes ITS columns
        # and carries the matcher's through `_better_match`; because `_merge`
        # does not feed it the prior merged file, the matcher's columns are in
        # practice blanked on every rerun (see the module docstring).
        Artifact(
            path=str(MERGED_CONTACTS),
            row_model=MessageContactRow,
            writes="upsert",
            owns_columns=DISCOVERY_OWNED_COLUMNS,
        ),
        # The staged copy under the discover dir. Sole writer, whole file. Its
        # one reader is `imports/status.py`, which counts its rows for the
        # "which sources are present" report.
        Artifact(
            path=str(DEFAULT_MESSAGES_OUTPUT_DIR / "contacts.csv"),
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
        self.selection = {
            "linked": bool(include_imessage or include_whatsapp),
            "include_imessage": bool(include_imessage),
            "include_whatsapp": bool(include_whatsapp),
        }
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)  # the one place the dir is created
        self.contacts_csv = self.out_dir / "contacts.csv"
        self.manifest_json = self.out_dir / "manifest.json"
        self.channels: list[MessageChannel] = []
        if self.selection["include_imessage"]:
            self.channels.append(IMessageChannel(
                other_enabled=self.selection["include_whatsapp"]))
        if self.selection["include_whatsapp"]:
            self.channels.append(WhatsAppChannel(
                other_enabled=self.selection["include_imessage"],
                max_messages=wacli_max_messages))

    def bindings(self) -> dict[str, str]:
        """Declared path -> this instance's path, so an explicit ``out_dir`` (or a
        test that patches ``MERGED_CONTACTS``) still validates against the
        declaration. The KEYS come from the declaration itself, never from a
        second read of a module constant — a patched constant would otherwise
        produce keys no declared path matches, and the template would validate
        the unpatched `.powerpacks/` path instead."""
        merged_declared, staged_declared = (item.path for item in self.outputs)
        bound = {
            merged_declared: str(MERGED_CONTACTS),
            staged_declared: str(self.contacts_csv),
            self.manifest: str(self.manifest_json),
        }
        # This store's declared inputs ARE the channels' declared outputs, so each
        # enabled channel supplies its own binding.
        for channel in self.channels:
            bound[channel.outputs[0].path] = str(channel.contacts_csv)
        return bound

    def execute(self) -> MessagesDiscoveryCompleted | MessagesDiscoveryNotCompleted | MessagesDiscoverySkipped:
        """Run the enabled channels (stop at the first blocked/failed child),
        merge, and return the typed payload (the Node template writes it)."""
        if not self.selection["linked"]:
            return MessagesDiscoverySkipped(
                reason="messages_not_linked",
                contacts_csv=str(self.contacts_csv),
                updated_at=now_iso(),
            )
        for channel in self.channels:
            child = channel.run()
            if child.get("status") != "completed":
                return self._not_completed(child)
        failed = self._merge()
        if failed is not None:
            return self._not_completed(failed.to_payload())
        return self._completed()

    def _artifacts(self) -> dict[str, Any]:
        """Union the per-channel artifact dicts into one map for the manifest."""
        merged: dict[str, Any] = {}
        for channel in self.channels:
            merged.update(channel.artifacts)
        return merged

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

    def _not_completed(self, child: dict[str, Any]) -> MessagesDiscoveryNotCompleted:
        """The not-completed stage payload for a blocked/failed child."""
        status = str(child.get("status") or "failed")
        return MessagesDiscoveryNotCompleted(
            status=status if status in {"blocked_user_action", "blocked_approval"} else "failed",
            error=child.get("error") or child.get("message") or child,
            child=child,
            contacts_csv=str(self.contacts_csv),
            updated_at=now_iso(),
        )

    def _completed(self) -> MessagesDiscoveryCompleted:
        """Copy the merged CSV to the fixed output dir and build the completed
        stage payload (contact count, channels, privacy, pre-full-sync nudge)."""
        artifacts = self._artifacts()
        artifacts["contacts_csv"] = str(MERGED_CONTACTS)
        child = {
            "primitive": "messages_discovery",
            "status": "selected_steps_completed",
            "message": "Selected message channels were extracted and merged.",
            "channels": {
                "imessage": self.selection["include_imessage"],
                "whatsapp": self.selection["include_whatsapp"],
            },
            "artifacts": artifacts,
            "privacy": {
                "message_bodies_read": False,
                "provider_research_ran": False,
                "cloud_upload_ran": False,
            },
        }
        if MERGED_CONTACTS.exists():
            shutil.copyfile(MERGED_CONTACTS, self.contacts_csv)
        else:
            write_csv_rows(self.contacts_csv, CSV_HEADERS, [])
        _, rows = read_csv_rows(self.contacts_csv)
        # The pairing fields hoist the non-blocking pre-full-sync nudge to the top
        # level so a fast-path run surfaces it without digging into child.artifacts.
        return MessagesDiscoveryCompleted(
            contacts_csv=str(self.contacts_csv),
            contacts=len(rows),
            include_imessage=self.selection["include_imessage"],
            include_whatsapp=self.selection["include_whatsapp"],
            privacy=MessagesPrivacy(),
            child=child,
            updated_at=now_iso(),
            whatsapp_pairing_state=artifacts.get("whatsapp_pairing_state") or None,
            whatsapp_pairing_notice=(artifacts.get("whatsapp_pairing_notice", "")
                                     if artifacts.get("whatsapp_pairing_state") else None),
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
    """CLI dispatch: run discover() and emit the payload; map status to the exit
    code (20 blocked, 1 failed, else 0)."""
    args = build_parser().parse_args()
    if args.command == "discover":
        payload = MessagesDiscovery(
            wacli_max_messages=args.wacli_max_messages,
            include_imessage=args.include_imessage,
            include_whatsapp=args.include_whatsapp,
        ).run()
        emit(payload)
        if payload.get("status") in {"blocked_user_action", "blocked_approval"}:
            return 20
        return 1 if payload.get("status") == "failed" else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
