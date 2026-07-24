#!/usr/bin/env python3
"""Discover iMessage and WhatsApp contact metadata.

This module owns only local metadata discovery. Review, LinkedIn profile
materialization, and enrichment live in imports/messages/importer.py.

Shape:
  MessagesDiscovery(accounts_file=..., include_imessage=..., include_whatsapp=...)
  is the whole thing: the constructor resolves which channels are enabled (from
  accounts.json, or the explicit --include-* override), creates the fixed output
  dir, and builds the channels; .run() extracts each channel, merges, and writes
  the stage manifest. main() constructs it and calls run() — no wrapper function.

  Each source is a MessageChannel (channels/) that owns its own output paths and
  its extract -> normalize chain (both in-process calls into the leaf primitive
  classes):
    - IMessageChannel (channels/i_message_channel.py): extract_imessage.py check
      (Full Disk Access gate) -> extract chat.db + AddressBook metadata ->
      imessage.contacts.csv -> normalize.
    - WhatsAppChannel (channels/whats_app_channel.py): WhatsAppExtractor.run
      (extract_whatsapp.py, composing the whatsapp_wacli client: fetch pinned
      wacli, auth, sync, deepen, export local metadata) ->
      whatsapp.contacts.csv -> normalize. Missing QR -> blocked_user_action;
      surfaces the pre-full-sync re-link nudge.
  extract()/normalize() return None on success or a blocked/failed child payload
  (blocked_child/failed_child, channels/message_channel_base.py) that
  short-circuits the run. MessagesDiscovery then merges the enabled per-channel
  CSVs by canonical phone -> .powerpacks/messages/contacts.csv, copies it to
  discover/messages/contacts.csv, and writes a typed manifest (contact count,
  channels, privacy=bodies-never-read, WhatsApp pre-full-sync nudge).
  Metadata only: no bodies, no research, no upload.

Changelog:
  2026-07-23 (in-process): MessagesDiscovery._merge now calls
    ``ContactsMerger().merge(...)`` in-process instead of spawning
    merge_contacts.py; the channels likewise call their leaf primitive classes
    directly (extract_imessage/extract_whatsapp/normalize_contacts). No self-owned
    Python file is spawned as a subprocess anymore. ``run_cmd``/``py_cmd`` are no
    longer imported here. Fixed output paths, manifests, and the CLI are unchanged.
  2026-07-23 (terse): folded the resolve()/discover() wrapper functions into
    MessagesDiscovery — the constructor now resolves channels from accounts_file
    (+ explicit --include-* override) itself, so callers just construct and run().
    out_dir is a plain default arg (was a None-sentinel read at call time for
    test patching); tests pass out_dir explicitly.
  2026-07-23 (channels split): MessageChannel + blocked_child/failed_child moved
    to channels/message_channel_base.py, and IMessageChannel/WhatsAppChannel with
    their owned IMESSAGE_*/WHATSAPP_* path constants (and the wacli
    max-messages/sync/depth defaults) moved to channels/i_message_channel.py /
    channels/whats_app_channel.py — channels own their paths. discover() split
    into resolve() (inputs + MessagesDiscovery construction) + run(); discover()
    is now the frozen wrapper = resolve(...).run(). This module keeps inputs
    resolution, the MessagesDiscovery store, and discover/resolve/build_parser/
    main. CLI signature, fixed output paths, and typed manifests unchanged.
  2026-07-23 (audit): discover()'s accounts parameter was renamed from
    accounts_path to accounts_file; internal helpers still take accounts_path,
    bridged by a local alias inside discover().
  2026-07-23 (oop): the per-channel extract/normalize free functions and the
    mutated `artifacts` dict + `child is None` chain were replaced by
    MessageChannel (IMessageChannel/WhatsAppChannel) classes and a MessagesDiscovery
    store that owns the output dir, run loop, merge, and manifest. Fixed output
    paths and the discover()/CLI signatures are unchanged.
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
    MessagesDiscoveryCompleted,
    MessagesDiscoveryNotCompleted,
    MessagesDiscoverySkipped,
    MessagesPrivacy,
)
from packs.ingestion.primitives.common.jsonio import emit, now_iso, write_json  # noqa: E402
from packs.ingestion.primitives.common.paths import (  # noqa: E402
    DEFAULT_ACCOUNTS,
    MESSAGES_OUT_DIR,
    discover_source_dir,
)
from packs.ingestion.primitives.common.manifests import write_stage_manifest  # noqa: E402
from packs.ingestion.primitives.discover.common import (  # noqa: E402
    account_config,
    channel_is_linked,
    read_accounts,
    read_csv_rows,
    write_csv_rows,
)
from packs.ingestion.primitives.discover.messages.merge_contacts import ContactsMerger  # noqa: E402
from packs.ingestion.primitives.discover.messages.channels.message_channel_base import (  # noqa: E402
    MessageChannel,
    failed_child,
)
from packs.ingestion.primitives.discover.messages.channels.i_message_channel import (  # noqa: E402
    IMessageChannel,
)
from packs.ingestion.primitives.discover.messages.channels.whats_app_channel import (  # noqa: E402
    DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
    WhatsAppChannel,
)
from packs.ingestion.schemas.message_contacts import CSV_HEADERS  # noqa: E402


DEFAULT_MESSAGES_OUTPUT_DIR = discover_source_dir("messages")

# The shared messages scratch dir stays sourced from common/paths; the
# merged-contacts output paths live here (the per-channel fixed paths are owned
# by the channel modules under channels/).
MESSAGES_DIR = MESSAGES_OUT_DIR
MERGED_CONTACTS = MESSAGES_DIR / "contacts.csv"
MERGED_CONTACTS_MANIFEST = MESSAGES_DIR / "contacts.csv.manifest.json"


def messages_discovery_inputs(accounts_path: Path) -> dict[str, Any]:
    """Resolve which message channels are enabled from accounts.json: messages
    must be linked; iMessage is on unless explicitly skipped; WhatsApp is on when
    linked or already authenticated."""
    accounts = read_accounts(accounts_path)
    cfg = account_config(accounts, "messages")
    if not channel_is_linked(accounts, "messages"):
        return {"linked": False, "include_imessage": False, "include_whatsapp": False}
    imessage_cfg = cfg.get("imessage") if isinstance(cfg.get("imessage"), dict) else {}
    whatsapp_cfg = cfg.get("whatsapp") if isinstance(cfg.get("whatsapp"), dict) else {}
    include_imessage = str(imessage_cfg.get("status") or "").strip().lower() != "skipped"
    include_whatsapp = (
        str(whatsapp_cfg.get("status") or "").strip().lower() == "linked"
        or whatsapp_cfg.get("authenticated") is True
    )
    return {
        "linked": bool(include_imessage or include_whatsapp),
        "include_imessage": include_imessage,
        "include_whatsapp": include_whatsapp,
    }


# --- the store: owns the output dir, the run loop, the merge, the manifest ----

class MessagesDiscovery:
    """Orchestrates a messages discovery run: creates the fixed output directory,
    runs each enabled channel's extract -> normalize (stopping at the first
    blocked/failed step), merges the per-channel CSVs, and writes the stage
    manifest. Holds all filesystem side effects so the channels stay pure."""

    def __init__(
        self,
        *,
        accounts_file: Path = DEFAULT_ACCOUNTS,
        out_dir: Path = DEFAULT_MESSAGES_OUTPUT_DIR,
        wacli_max_messages: int = DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
        include_imessage: bool | None = None,
        include_whatsapp: bool | None = None,
    ) -> None:
        # Explicit --include-* flags override the accounts.json-derived set
        # entirely; otherwise the enabled channels come from what's linked.
        if include_imessage is not None or include_whatsapp is not None:
            self.inputs = {
                "linked": bool(include_imessage or include_whatsapp),
                "include_imessage": bool(include_imessage),
                "include_whatsapp": bool(include_whatsapp),
            }
        else:
            self.inputs = messages_discovery_inputs(accounts_file)
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)  # the one place the dir is created
        self.contacts_csv = self.out_dir / "contacts.csv"
        self.manifest_json = self.out_dir / "manifest.json"
        self.channels: list[MessageChannel] = []
        if self.inputs["include_imessage"]:
            self.channels.append(IMessageChannel(
                accounts_path=accounts_file, other_enabled=self.inputs["include_whatsapp"]))
        if self.inputs["include_whatsapp"]:
            self.channels.append(WhatsAppChannel(
                accounts_path=accounts_file, other_enabled=self.inputs["include_imessage"],
                max_messages=wacli_max_messages))

    def run(self) -> dict[str, Any]:
        """Run the enabled channels (stop at the first blocked/failed child),
        merge, and write the stage manifest; return the manifest payload."""
        if not self.inputs["linked"]:
            return write_stage_manifest(self.manifest_json, MessagesDiscoverySkipped(
                reason="messages_not_linked",
                contacts_csv=str(self.contacts_csv),
                updated_at=now_iso(),
            ))
        for channel in self.channels:
            child = channel.run()
            if child is not None:
                return self._not_completed(child)
        child = self._merge()
        if child is not None:
            return self._not_completed(child)
        return self._completed()

    def _artifacts(self) -> dict[str, Any]:
        """Union the per-channel artifact dicts into one map for the manifest."""
        merged: dict[str, Any] = {}
        for channel in self.channels:
            merged.update(channel.artifacts)
        return merged

    def _merge(self) -> dict[str, Any] | None:
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

    def _not_completed(self, child: dict[str, Any]) -> dict[str, Any]:
        """Write the not-completed stage manifest for a blocked/failed child."""
        status = str(child.get("status") or "failed")
        return write_stage_manifest(self.manifest_json, MessagesDiscoveryNotCompleted(
            status=status if status in {"blocked_user_action", "blocked_approval"} else "failed",
            error=child.get("error") or child.get("message") or child,
            child=child,
            contacts_csv=str(self.contacts_csv),
            updated_at=now_iso(),
        ))

    def _completed(self) -> dict[str, Any]:
        """Copy the merged CSV to the fixed output dir and write the completed
        stage manifest (contact count, channels, privacy, pre-full-sync nudge)."""
        artifacts = self._artifacts()
        artifacts["contacts_csv"] = str(MERGED_CONTACTS)
        child = {
            "primitive": "messages_discovery",
            "status": "selected_steps_completed",
            "message": "Selected message channels were extracted and merged.",
            "channels": {
                "imessage": self.inputs["include_imessage"],
                "whatsapp": self.inputs["include_whatsapp"],
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
        return write_stage_manifest(self.manifest_json, MessagesDiscoveryCompleted(
            contacts_csv=str(self.contacts_csv),
            contacts=len(rows),
            include_imessage=self.inputs["include_imessage"],
            include_whatsapp=self.inputs["include_whatsapp"],
            privacy=MessagesPrivacy(),
            child=child,
            updated_at=now_iso(),
            whatsapp_pairing_state=artifacts.get("whatsapp_pairing_state") or None,
            whatsapp_pairing_notice=(artifacts.get("whatsapp_pairing_notice", "")
                                     if artifacts.get("whatsapp_pairing_state") else None),
        ))


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse surface: the single `discover` subcommand with the
    accounts path, wacli max-messages, and the explicit --include-* overrides."""
    parser = argparse.ArgumentParser(description="Discover iMessage/WhatsApp contacts")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("discover", help="Discover message contacts")
    run.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    run.add_argument("--wacli-max-messages", type=int, default=DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES)
    run.add_argument("--include-imessage", action="store_true", default=None)
    run.add_argument("--include-whatsapp", action="store_true", default=None)
    return parser


def main() -> int:
    """CLI dispatch: run discover() and emit the payload; map status to the exit
    code (20 blocked, 1 failed, else 0)."""
    args = build_parser().parse_args()
    if args.command == "discover":
        payload = MessagesDiscovery(
            accounts_file=args.accounts,
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
