#!/usr/bin/env python3
"""Discover iMessage and WhatsApp contact metadata.

This module owns only local metadata discovery. Review, LinkedIn profile
materialization, and enrichment live in import_contacts_pipeline/messages.py.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        account_config,
        channel_is_linked,
        emit,
        now_iso,
        py_cmd,
        read_accounts,
        read_csv_rows,
        run_cmd,
        write_csv_rows,
        write_json,
        write_stage_manifest,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.primitives.discover_contacts_pipeline.common import (
        DEFAULT_BASE_DIR,
        account_config,
        channel_is_linked,
        emit,
        now_iso,
        py_cmd,
        read_accounts,
        read_csv_rows,
        run_cmd,
        write_csv_rows,
        write_json,
        write_stage_manifest,
    )


DEFAULT_ACCOUNTS = Path(".powerpacks/ingestion/accounts.json")
DEFAULT_MESSAGES_OUTPUT_DIR = DEFAULT_BASE_DIR / "discover" / "messages"
DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES = 0
# First full backfill scales with history size (~3-year default window):
# ~30 minutes on small accounts, a few hours on large ones. 3h hard cap.
DEFAULT_WACLI_SYNC_TIMEOUT = 10800
# Explicit full imports then run a resumable, sequential shallow-chat depth
# stage. Give that bounded child work up to two additional hours before the
# discovery wrapper stops; completed per-chat results remain resumable.
DEFAULT_WACLI_DEPTH_TIMEOUT = 7200

MESSAGES_DIR = Path(".powerpacks/messages")
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

CONTACT_CSV_HEADERS = [
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
    "skip",
    "match_status",
    "matched_person_id",
    "matched_name",
    "matched_linkedin_url",
    "match_confidence",
    "match_method",
    "match_reason",
]


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


def _blocked_child(
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
        "packs/ingestion/primitives/discover_contacts_pipeline/messages.py discover "
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


def _failed_child(step_id: str, payload: dict[str, Any], stderr: str) -> dict[str, Any]:
    detail = payload.get("error") or payload.get("message") or payload or stderr or "child command failed"
    return {
        "primitive": "messages_discovery",
        "status": "failed",
        "step_id": step_id,
        "error": detail,
    }


def _extract_imessage(
    artifacts: dict[str, Any],
    accounts_path: Path,
    include_whatsapp: bool,
) -> dict[str, Any] | None:
    check_command = py_cmd(
        "packs/ingestion/primitives/extract_imessage_contacts/extract_imessage_contacts.py",
        "check",
        "--strict",
    )
    code, payload, stderr = run_cmd(check_command)
    if code != 0:
        return _blocked_child(
            message="Enable macOS Full Disk Access / Contacts access for this terminal, then continue.",
            accounts_path=accounts_path,
            detail=payload or stderr[-1000:],
            include_imessage=True,
            include_whatsapp=include_whatsapp,
        )

    extract_command = py_cmd(
        "packs/ingestion/primitives/extract_imessage_contacts/extract_imessage_contacts.py",
        "extract",
        "--output-csv",
        str(IMESSAGE_CONTACTS),
        "--output-jsonl",
        str(IMESSAGE_RAW_JSONL),
        "--manifest",
        str(IMESSAGE_MANIFEST),
    )
    code, payload, stderr = run_cmd(extract_command, timeout=600)
    if code != 0:
        return _failed_child("extract_imessage", payload, stderr)
    artifacts["imessage_contacts_csv"] = str(IMESSAGE_CONTACTS)
    return None


def _extract_whatsapp(
    artifacts: dict[str, Any],
    accounts_path: Path,
    max_messages: int,
    include_imessage: bool,
    sync_mode: str = "auto",
) -> dict[str, Any] | None:
    command = py_cmd(
        "packs/ingestion/primitives/import_whatsapp_wacli/import_whatsapp_wacli.py",
        "run",
        "--output-csv",
        str(WHATSAPP_CONTACTS),
        "--output-jsonl",
        str(WHATSAPP_RAW_JSONL),
        "--manifest",
        str(WHATSAPP_MANIFEST),
        "--progress-jsonl",
        str(WHATSAPP_PROGRESS_JSONL),
        "--max-messages",
        str(max_messages),
        "--sync-mode",
        sync_mode,
        "--max-group-participants",
        "30",
        "--sync-timeout",
        str(DEFAULT_WACLI_SYNC_TIMEOUT),
    )
    code, payload, stderr = run_cmd(
        command,
        timeout=DEFAULT_WACLI_SYNC_TIMEOUT + DEFAULT_WACLI_DEPTH_TIMEOUT + 900,
    )
    if code != 0 and payload.get("status") == "blocked_user_action":
        return _blocked_child(
            message=str(payload.get("message") or "WhatsApp needs a QR scan."),
            accounts_path=accounts_path,
            detail=payload,
            whatsapp_provider="wacli",
            qr_page=str(payload.get("qr_page") or MESSAGES_DIR / "wacli-login-qr.html"),
            include_imessage=include_imessage,
            include_whatsapp=True,
        )
    if code != 0:
        return _failed_child("extract_whatsapp", payload, stderr)
    artifacts["whatsapp_contacts_csv"] = str(WHATSAPP_CONTACTS)
    artifacts["whatsapp_provider"] = "wacli"
    # Surface the non-blocking "re-link for deeper history" nudge to the skill when
    # the WhatsApp session predates full history sync.
    pairing = payload.get("pairing") if isinstance(payload.get("pairing"), dict) else {}
    if pairing.get("state") == "pre_full_sync":
        artifacts["whatsapp_pairing_state"] = "pre_full_sync"
        artifacts["whatsapp_pairing_notice"] = str(pairing.get("hint") or "")
    return None


def _normalize_channel(
    *,
    step_id: str,
    input_csv: Path,
    output_jsonl: Path,
    manifest: Path,
) -> dict[str, Any] | None:
    if output_jsonl.exists() and (
        not input_csv.exists()
        or output_jsonl.stat().st_mtime_ns >= input_csv.stat().st_mtime_ns
    ):
        return None
    if not input_csv.exists():
        output_jsonl.parent.mkdir(parents=True, exist_ok=True)
        output_jsonl.write_text("", encoding="utf-8")
        summary = {
            "primitive": "normalize_message_contacts",
            "status": "ok",
            "reason": f"missing_input:{input_csv}",
            "output": str(output_jsonl),
            "counts": {"rows_written": 0},
        }
        write_json(manifest, summary)
        return None
    command = py_cmd(
        "packs/ingestion/primitives/normalize_message_contacts/normalize_message_contacts.py",
        "normalize",
        "--input",
        str(input_csv),
        "--out-jsonl",
        str(output_jsonl),
        "--manifest",
        str(manifest),
    )
    code, payload, stderr = run_cmd(command)
    if code != 0:
        return _failed_child(step_id, payload, stderr)
    return None


def _merge_contacts(
    artifacts: dict[str, Any],
    *,
    include_imessage: bool,
    include_whatsapp: bool,
) -> dict[str, Any] | None:
    inputs = [
        path
        for enabled, path in (
            (include_imessage, IMESSAGE_CONTACTS),
            (include_whatsapp, WHATSAPP_CONTACTS),
        )
        if enabled and path.exists()
    ]
    if not inputs:
        write_csv_rows(MERGED_CONTACTS, CONTACT_CSV_HEADERS, [])
        summary = {
            "primitive": "merge_message_contacts",
            "status": "ok",
            "reason": "no_channel_contact_exports_found",
            "artifacts": {"contacts_csv": str(MERGED_CONTACTS)},
            "counts": {"rows_written": 0, "unique_phones": 0, "cross_channel_phones": 0, "by_source": {}},
        }
        write_json(MERGED_CONTACTS_MANIFEST, summary)
        artifacts["contacts_csv"] = str(MERGED_CONTACTS)
        return None

    command = py_cmd(
        "packs/ingestion/primitives/merge_message_contacts/merge_message_contacts.py",
        "merge",
    )
    for input_csv in inputs:
        command.extend(["--input", str(input_csv)])
    command.extend(["--output", str(MERGED_CONTACTS), "--manifest", str(MERGED_CONTACTS_MANIFEST)])
    code, payload, stderr = run_cmd(command)
    if code != 0:
        return _failed_child("ensure_contacts", payload, stderr)
    artifacts["contacts_csv"] = str(MERGED_CONTACTS)
    return None


def _completed_child(artifacts: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "primitive": "messages_discovery",
        "status": "selected_steps_completed",
        "message": "Selected message channels were extracted and merged.",
        "channels": {
            "imessage": inputs["include_imessage"],
            "whatsapp": inputs["include_whatsapp"],
        },
        "artifacts": artifacts,
        "privacy": {
            "message_bodies_read": False,
            "provider_research_ran": False,
            "cloud_upload_ran": False,
        },
    }


def discover(
    *,
    accounts_path: Path = DEFAULT_ACCOUNTS,
    wacli_max_messages: int = DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
    wacli_sync_mode: str = "auto",
    include_imessage: bool | None = None,
    include_whatsapp: bool | None = None,
) -> dict[str, Any]:
    inputs = messages_discovery_inputs(accounts_path)
    if include_imessage is not None or include_whatsapp is not None:
        inputs = {
            "linked": bool(include_imessage or include_whatsapp),
            "include_imessage": bool(include_imessage),
            "include_whatsapp": bool(include_whatsapp),
        }
    artifacts: dict[str, Any] = {}
    contacts_csv = DEFAULT_MESSAGES_OUTPUT_DIR / "contacts.csv"
    manifest_json = DEFAULT_MESSAGES_OUTPUT_DIR / "manifest.json"
    DEFAULT_MESSAGES_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not inputs["linked"]:
        payload = {
            "status": "skipped",
            "source": "messages",
            "reason": "messages_not_linked",
            "contacts_csv": str(contacts_csv),
            "updated_at": now_iso(),
        }
        write_stage_manifest(manifest_json, payload)
        return payload

    child: dict[str, Any] | None = None
    if inputs["include_imessage"]:
        child = _extract_imessage(
            artifacts,
            accounts_path,
            inputs["include_whatsapp"],
        )
        if child is None:
            child = _normalize_channel(
                step_id="normalize_imessage",
                input_csv=IMESSAGE_CONTACTS,
                output_jsonl=IMESSAGE_NORMALIZED_JSONL,
                manifest=IMESSAGE_NORMALIZED_MANIFEST,
            )
    if child is None and inputs["include_whatsapp"]:
        child = _extract_whatsapp(
            artifacts,
            accounts_path,
            wacli_max_messages,
            inputs["include_imessage"],
            sync_mode=wacli_sync_mode,
        )
        if child is None:
            child = _normalize_channel(
                step_id="normalize_whatsapp",
                input_csv=WHATSAPP_CONTACTS,
                output_jsonl=WHATSAPP_NORMALIZED_JSONL,
                manifest=WHATSAPP_NORMALIZED_MANIFEST,
            )
    if child is None:
        child = _merge_contacts(
            artifacts,
            include_imessage=inputs["include_imessage"],
            include_whatsapp=inputs["include_whatsapp"],
        )

    if child is not None:
        status = str(child.get("status") or "failed")
        result = {
            "status": status if status in {"blocked_user_action", "blocked_approval"} else "failed",
            "source": "messages",
            "error": child.get("error") or child.get("message") or child,
            "child": child,
            "contacts_csv": str(contacts_csv),
            "updated_at": now_iso(),
        }
        write_stage_manifest(manifest_json, result)
        return result

    child = _completed_child(artifacts, inputs)
    if MERGED_CONTACTS.exists():
        shutil.copyfile(MERGED_CONTACTS, contacts_csv)
    else:
        write_csv_rows(contacts_csv, CONTACT_CSV_HEADERS, [])
    _, rows = read_csv_rows(contacts_csv)
    result = {
        "status": "completed",
        "source": "messages",
        "contacts_csv": str(contacts_csv),
        "contacts": len(rows),
        "include_imessage": inputs["include_imessage"],
        "include_whatsapp": inputs["include_whatsapp"],
        "privacy": {
            "message_bodies_read": False,
            "powerset_sync_ran": False,
            "llm_review_ran": False,
            "deep_research_ran": False,
            "upload_ran": False,
        },
        "child": child,
        "updated_at": now_iso(),
    }
    # Hoist the non-blocking pre-full-sync nudge to the top level so a fast-path
    # run surfaces it without digging into child.artifacts (where it was buried).
    if artifacts.get("whatsapp_pairing_state"):
        result["whatsapp_pairing_state"] = artifacts["whatsapp_pairing_state"]
        result["whatsapp_pairing_notice"] = artifacts.get("whatsapp_pairing_notice", "")
    result = write_stage_manifest(manifest_json, result)
    return result


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
            accounts_path=args.accounts,
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
