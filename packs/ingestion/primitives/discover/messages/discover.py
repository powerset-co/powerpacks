#!/usr/bin/env python3
"""Discover iMessage and WhatsApp contact metadata.

This module owns only local metadata discovery. Review, LinkedIn profile
materialization, and enrichment live in imports/messages/importer.py.

Shape (discover()):
  Resolve channels (messages_discovery_inputs from accounts.json, or explicit
  --include-imessage/--include-whatsapp), then hand off to MessagesDiscovery,
  which owns the run: the fixed output dir, the enabled channels, the merge, and
  the final stage manifest.

  Each source is a MessageChannel that owns its own output paths and its
  extract -> normalize subprocess chain:
    - IMessageChannel: extract_imessage.py check (Full Disk Access gate) ->
      extract chat.db + AddressBook metadata -> imessage.contacts.csv -> normalize.
    - WhatsAppChannel: whatsapp_wacli.py run (fetch pinned wacli, auth, sync,
      export local metadata) -> whatsapp.contacts.csv -> normalize. Missing QR ->
      blocked_user_action; surfaces the pre-full-sync re-link nudge.
  extract()/normalize() return None on success or a blocked/failed child payload
  that short-circuits the run. MessagesDiscovery then merges the enabled per-channel
  CSVs by canonical phone -> .powerpacks/messages/contacts.csv, copies it to
  discover/messages/contacts.csv, and writes a typed manifest (contact count,
  channels, privacy=bodies-never-read, WhatsApp pre-full-sync nudge).
  Metadata only: no bodies, no research, no upload.

Changelog:
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
    DEFAULT_BASE_DIR,
    MESSAGES_OUT_DIR,
)
from packs.ingestion.primitives.common.manifests import write_stage_manifest  # noqa: E402
from packs.ingestion.primitives.common.proc import py_cmd, run_cmd  # noqa: E402
from packs.ingestion.primitives.discover.common import (  # noqa: E402
    account_config,
    channel_is_linked,
    read_accounts,
    read_csv_rows,
    write_csv_rows,
)
from packs.ingestion.schemas.message_contacts import CSV_HEADERS  # noqa: E402


DEFAULT_MESSAGES_OUTPUT_DIR = DEFAULT_BASE_DIR / "discover" / "messages"
DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES = 0
# First full backfill scales with history size (~3-year default window):
# ~30 minutes on small accounts, a few hours on large ones. 3h hard cap.
DEFAULT_WACLI_SYNC_TIMEOUT = 10800

# Fixed per-stage output paths (the durable contract: each stage overwrites in
# place at a stable path, so reruns are idempotent). Channels own these; they are
# module-level so import consumers and the manifest contract can reference them.
MESSAGES_DIR = MESSAGES_OUT_DIR
IMESSAGE_CONTACTS = MESSAGES_DIR / "imessage.contacts.csv"
IMESSAGE_RAW_JSONL = MESSAGES_DIR / "imessage.contacts.raw.jsonl"
IMESSAGE_MANIFEST = MESSAGES_DIR / "imessage.manifest.json"
IMESSAGE_NORMALIZED_JSONL = MESSAGES_DIR / "imessage.contacts.normalized.jsonl"
IMESSAGE_NORMALIZED_MANIFEST = MESSAGES_DIR / "imessage.contacts.normalized.jsonl.manifest.json"
WHATSAPP_CONTACTS = MESSAGES_DIR / "whatsapp.contacts.csv"
WHATSAPP_RAW_JSONL = MESSAGES_DIR / "whatsapp.contacts.raw.jsonl"
WHATSAPP_MANIFEST = MESSAGES_DIR / "whatsapp.contacts.csv.manifest.json"
WHATSAPP_PROGRESS_JSONL = MESSAGES_DIR / "whatsapp.contacts.csv.manifest.json.progress.jsonl"
WHATSAPP_NORMALIZED_JSONL = MESSAGES_DIR / "whatsapp.contacts.normalized.jsonl"
WHATSAPP_NORMALIZED_MANIFEST = MESSAGES_DIR / "whatsapp.contacts.normalized.jsonl.manifest.json"
MERGED_CONTACTS = MESSAGES_DIR / "contacts.csv"
MERGED_CONTACTS_MANIFEST = MESSAGES_DIR / "contacts.csv.manifest.json"


def messages_discovery_inputs(accounts_path: Path) -> dict[str, Any]:
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


# --- child payloads (a channel step returns None on success, else one of these) ---

def blocked_child(
    *,
    message: str,
    accounts_path: Path,
    detail: Any = None,
    whatsapp_provider: str = "",
    qr_page: str = "",
    include_imessage: bool = False,
    include_whatsapp: bool = False,
) -> dict[str, Any]:
    command = (
        "uv run --project . python "
        "packs/ingestion/primitives/discover/messages/discover.py discover "
        f"--accounts {accounts_path}"
    )
    if include_imessage:
        command += " --include-imessage"
    if include_whatsapp:
        command += " --include-whatsapp"
    payload = {
        "primitive": "messages_discovery",
        "status": "blocked_user_action",
        "message": message,
        "detail": detail,
        "whatsapp_provider": whatsapp_provider,
        "qr_page": qr_page,
        "continue_command": command,
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


def failed_child(step_id: str, payload: dict[str, Any], stderr: str) -> dict[str, Any]:
    detail = payload.get("error") or payload.get("message") or payload or stderr or "child command failed"
    return {
        "primitive": "messages_discovery",
        "status": "failed",
        "step_id": step_id,
        "error": detail,
    }


# --- channels: each source owns its output paths + extract -> normalize -------

class MessageChannel:
    """One message source (iMessage or WhatsApp). Owns its output paths and its
    extract -> normalize subprocess chain, and records what it contributed in
    ``artifacts``. extract()/normalize()/run() return None on success or a
    blocked/failed child payload that short-circuits the discovery run."""

    name = ""

    def __init__(self, *, accounts_path: Path, other_enabled: bool) -> None:
        self.accounts_path = accounts_path
        # Whether the OTHER channel is enabled — only used to rebuild an accurate
        # `--include-*` continue command when this channel blocks.
        self.other_enabled = other_enabled
        self.artifacts: dict[str, Any] = {}

    # Path accessors read the module-level fixed paths at call time so tests can
    # patch the module constant and have the channel honor it.
    @property
    def contacts_csv(self) -> Path:
        raise NotImplementedError

    @property
    def normalized_jsonl(self) -> Path:
        raise NotImplementedError

    @property
    def normalized_manifest(self) -> Path:
        raise NotImplementedError

    def extract(self) -> dict[str, Any] | None:
        raise NotImplementedError

    def normalize(self) -> dict[str, Any] | None:
        """Normalize this channel's contacts CSV into JSONL. No-op when the JSONL
        is already at least as new as the CSV; writes an empty JSONL + manifest
        when the CSV is missing (a channel that produced no contacts)."""
        input_csv, output_jsonl, manifest = self.contacts_csv, self.normalized_jsonl, self.normalized_manifest
        if output_jsonl.exists() and (
            not input_csv.exists()
            or output_jsonl.stat().st_mtime_ns >= input_csv.stat().st_mtime_ns
        ):
            return None
        if not input_csv.exists():
            output_jsonl.parent.mkdir(parents=True, exist_ok=True)
            output_jsonl.write_text("", encoding="utf-8")
            write_json(manifest, {
                "primitive": "messages/normalize_contacts",
                "status": "ok",
                "reason": f"missing_input:{input_csv}",
                "output": str(output_jsonl),
                "counts": {"rows_written": 0},
            })
            return None
        code, payload, stderr = run_cmd(py_cmd(
            "packs/ingestion/primitives/discover/messages/normalize_contacts.py",
            "normalize",
            "--input", str(input_csv),
            "--out-jsonl", str(output_jsonl),
            "--manifest", str(manifest),
        ))
        if code != 0:
            return failed_child(f"normalize_{self.name}", payload, stderr)
        return None

    def run(self) -> dict[str, Any] | None:
        blocked = self.extract()
        if blocked is not None:
            return blocked
        return self.normalize()


class IMessageChannel(MessageChannel):
    name = "imessage"

    @property
    def contacts_csv(self) -> Path:
        return IMESSAGE_CONTACTS

    @property
    def normalized_jsonl(self) -> Path:
        return IMESSAGE_NORMALIZED_JSONL

    @property
    def normalized_manifest(self) -> Path:
        return IMESSAGE_NORMALIZED_MANIFEST

    def extract(self) -> dict[str, Any] | None:
        code, payload, stderr = run_cmd(py_cmd(
            "packs/ingestion/primitives/discover/messages/extract_imessage.py",
            "check", "--strict",
        ))
        if code != 0:
            return blocked_child(
                message="Enable macOS Full Disk Access / Contacts access for this terminal, then continue.",
                accounts_path=self.accounts_path,
                detail=payload or stderr[-1000:],
                include_imessage=True,
                include_whatsapp=self.other_enabled,
            )
        code, payload, stderr = run_cmd(py_cmd(
            "packs/ingestion/primitives/discover/messages/extract_imessage.py",
            "extract",
            "--output-csv", str(IMESSAGE_CONTACTS),
            "--output-jsonl", str(IMESSAGE_RAW_JSONL),
            "--manifest", str(IMESSAGE_MANIFEST),
        ), timeout=600)
        if code != 0:
            return failed_child("extract_imessage", payload, stderr)
        self.artifacts["imessage_contacts_csv"] = str(IMESSAGE_CONTACTS)
        return None


class WhatsAppChannel(MessageChannel):
    name = "whatsapp"

    def __init__(
        self,
        *,
        accounts_path: Path,
        other_enabled: bool,
        max_messages: int = DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
        sync_mode: str = "auto",
    ) -> None:
        super().__init__(accounts_path=accounts_path, other_enabled=other_enabled)
        self.max_messages = max_messages
        self.sync_mode = sync_mode

    @property
    def contacts_csv(self) -> Path:
        return WHATSAPP_CONTACTS

    @property
    def normalized_jsonl(self) -> Path:
        return WHATSAPP_NORMALIZED_JSONL

    @property
    def normalized_manifest(self) -> Path:
        return WHATSAPP_NORMALIZED_MANIFEST

    def extract(self) -> dict[str, Any] | None:
        code, payload, stderr = run_cmd(py_cmd(
            "packs/ingestion/primitives/discover/messages/whatsapp_wacli.py",
            "run",
            "--output-csv", str(WHATSAPP_CONTACTS),
            "--output-jsonl", str(WHATSAPP_RAW_JSONL),
            "--manifest", str(WHATSAPP_MANIFEST),
            "--progress-jsonl", str(WHATSAPP_PROGRESS_JSONL),
            "--max-messages", str(self.max_messages),
            "--sync-mode", self.sync_mode,
            "--max-group-participants", "30",
            "--sync-timeout", str(DEFAULT_WACLI_SYNC_TIMEOUT),
        ), timeout=DEFAULT_WACLI_SYNC_TIMEOUT + 900)
        if code != 0 and payload.get("status") == "blocked_user_action":
            return blocked_child(
                message=str(payload.get("message") or "WhatsApp needs a QR scan."),
                accounts_path=self.accounts_path,
                detail=payload,
                whatsapp_provider="wacli",
                qr_page=str(payload.get("qr_page") or MESSAGES_DIR / "wacli-login-qr.html"),
                include_imessage=self.other_enabled,
                include_whatsapp=True,
            )
        if code != 0:
            return failed_child("extract_whatsapp", payload, stderr)
        self.artifacts["whatsapp_contacts_csv"] = str(WHATSAPP_CONTACTS)
        self.artifacts["whatsapp_provider"] = "wacli"
        # Surface the non-blocking "re-link for deeper history" nudge to the skill
        # when the WhatsApp session predates full history sync.
        pairing = payload.get("pairing") if isinstance(payload.get("pairing"), dict) else {}
        if pairing.get("state") == "pre_full_sync":
            self.artifacts["whatsapp_pairing_state"] = "pre_full_sync"
            self.artifacts["whatsapp_pairing_notice"] = str(pairing.get("hint") or "")
        return None


# --- the store: owns the output dir, the run loop, the merge, the manifest ----

class MessagesDiscovery:
    """Orchestrates a messages discovery run: creates the fixed output directory,
    runs each enabled channel's extract -> normalize (stopping at the first
    blocked/failed step), merges the per-channel CSVs, and writes the stage
    manifest. Holds all filesystem side effects so the channels stay pure."""

    def __init__(
        self,
        *,
        inputs: dict[str, Any],
        accounts_path: Path,
        out_dir: Path | None = None,
        wacli_max_messages: int = DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
        wacli_sync_mode: str = "auto",
    ) -> None:
        self.inputs = inputs
        # Read the module constant at call time (not as a default arg) so tests
        # can patch DEFAULT_MESSAGES_OUTPUT_DIR and the store honors it.
        self.out_dir = out_dir if out_dir is not None else DEFAULT_MESSAGES_OUTPUT_DIR
        self.out_dir.mkdir(parents=True, exist_ok=True)  # the one place the dir is created
        self.contacts_csv = self.out_dir / "contacts.csv"
        self.manifest_json = self.out_dir / "manifest.json"
        self.channels: list[MessageChannel] = []
        if inputs["include_imessage"]:
            self.channels.append(IMessageChannel(
                accounts_path=accounts_path, other_enabled=inputs["include_whatsapp"]))
        if inputs["include_whatsapp"]:
            self.channels.append(WhatsAppChannel(
                accounts_path=accounts_path, other_enabled=inputs["include_imessage"],
                max_messages=wacli_max_messages, sync_mode=wacli_sync_mode))

    def run(self) -> dict[str, Any]:
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
        merged: dict[str, Any] = {}
        for channel in self.channels:
            merged.update(channel.artifacts)
        return merged

    def _merge(self) -> dict[str, Any] | None:
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
        command = py_cmd("packs/ingestion/primitives/discover/messages/merge_contacts.py", "merge")
        for input_csv in inputs:
            command.extend(["--input", str(input_csv)])
        command.extend(["--output", str(MERGED_CONTACTS), "--manifest", str(MERGED_CONTACTS_MANIFEST)])
        code, payload, stderr = run_cmd(command)
        if code != 0:
            return failed_child("ensure_contacts", payload, stderr)
        return None

    def _not_completed(self, child: dict[str, Any]) -> dict[str, Any]:
        status = str(child.get("status") or "failed")
        return write_stage_manifest(self.manifest_json, MessagesDiscoveryNotCompleted(
            status=status if status in {"blocked_user_action", "blocked_approval"} else "failed",
            error=child.get("error") or child.get("message") or child,
            child=child,
            contacts_csv=str(self.contacts_csv),
            updated_at=now_iso(),
        ))

    def _completed(self) -> dict[str, Any]:
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


def discover(
    *,
    accounts_file: Path = DEFAULT_ACCOUNTS,
    wacli_max_messages: int = DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
    wacli_sync_mode: str = "auto",
    include_imessage: bool | None = None,
    include_whatsapp: bool | None = None,
) -> dict[str, Any]:
    inputs = messages_discovery_inputs(accounts_file)
    if include_imessage is not None or include_whatsapp is not None:
        inputs = {
            "linked": bool(include_imessage or include_whatsapp),
            "include_imessage": bool(include_imessage),
            "include_whatsapp": bool(include_whatsapp),
        }
    return MessagesDiscovery(
        inputs=inputs,
        accounts_path=accounts_file,
        wacli_max_messages=wacli_max_messages,
        wacli_sync_mode=wacli_sync_mode,
    ).run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover iMessage/WhatsApp contacts")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("discover", help="Discover message contacts")
    run.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    run.add_argument("--wacli-max-messages", type=int, default=DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES)
    run.add_argument("--wacli-sync-mode", choices=("auto", "full", "incremental"), default="auto",
                     help="auto: full WhatsApp backfill on first run, incremental after; "
                          "full: force a full re-backfill; incremental: only new messages")
    run.add_argument("--include-imessage", action="store_true", default=None)
    run.add_argument("--include-whatsapp", action="store_true", default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "discover":
        payload = discover(
            accounts_file=args.accounts,
            wacli_max_messages=args.wacli_max_messages,
            wacli_sync_mode=args.wacli_sync_mode,
            include_imessage=args.include_imessage,
            include_whatsapp=args.include_whatsapp,
        )
        emit(payload)
        if payload.get("status") in {"blocked_user_action", "blocked_approval"}:
            return 20
        return 1 if payload.get("status") == "failed" else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
