"""Allowlisted manifest readers and privacy-safe workflow projection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .contracts import (
    EFFORTS,
    closed_enum,
    count_bucket,
    duration_bucket,
    nonnegative_number,
    normalized_model,
)

ManifestSpec = tuple[str, str]

WORKFLOW_MANIFESTS: dict[str, tuple[ManifestSpec, ...]] = {
    "setup": (
        ("linkedin_discovery", ".powerpacks/network-import/discover/linkedin/manifest.json"),
        ("linkedin_contacts_index", ".powerpacks/network-import/index/contacts/manifest.json"),
        ("people_merge", ".powerpacks/network-import/merged/manifest.json"),
        ("search_index", ".powerpacks/search-index/manifest.json"),
        ("linkedin_modal", ".powerpacks/runs/setup-linkedin-modal/status.json"),
        ("gmail_modal", ".powerpacks/runs/setup-gmail-modal/status.json"),
    ),
    "import-gmail": (
        ("gmail_discovery", ".powerpacks/network-import/discover/gmail/manifest.json"),
        ("gmail_import", ".powerpacks/network-import/import/gmail/manifest.json"),
        ("people_merge", ".powerpacks/network-import/merged/manifest.json"),
    ),
    "import-messages": (
        ("messages_discovery", ".powerpacks/network-import/discover/messages/manifest.json"),
        ("imessage_extract", ".powerpacks/messages/imessage.manifest.json"),
        ("whatsapp_extract", ".powerpacks/messages/whatsapp.contacts.csv.manifest.json"),
        ("message_history_depth", ".powerpacks/messages/history-depth/manifest.json"),
        ("message_match", ".powerpacks/messages/contacts.csv.match.manifest.json"),
        ("messages_import", ".powerpacks/network-import/import/messages/manifest.json"),
        ("people_merge", ".powerpacks/network-import/merged/manifest.json"),
    ),
    "deep-context": (
        ("context_collection", ".powerpacks/deep-context/raw/manifest.json"),
        ("fact_synthesis", ".powerpacks/deep-context/facts/manifest.json"),
        ("dossier_composition", ".powerpacks/deep-context/dossiers/manifest.json"),
        ("duplicate_clustering", ".powerpacks/deep-context/dossiers/merge_manifest.json"),
        ("dossier_validation", ".powerpacks/deep-context/dossiers/validation.json"),
        ("parent_build", ".powerpacks/deep-context/parents/manifest.json"),
        ("linkedin_reconcile", ".powerpacks/deep-context/reconcile/manifest.json"),
        ("human_review", ".powerpacks/deep-context/review/manifest.json"),
        ("deep_research", ".powerpacks/deep-context/reconcile/deep-research/manifest.json"),
        ("profile_prefetch", ".powerpacks/deep-context/profile-prefetch/manifest.json"),
        ("people_merge", ".powerpacks/network-import/merged/manifest.json"),
        ("search_index", ".powerpacks/search-index/manifest.json"),
    ),
}

COUNT_KEYS = {
    "accounts": "accounts", "account_count": "accounts",
    "calls": "calls", "call_count": "calls", "api_calls": "calls", "llm_calls": "calls",
    "candidates": "candidates", "candidate_count": "candidates",
    "contacts": "contacts", "contact_count": "contacts",
    "errors": "errors", "error_count": "errors", "failed": "errors", "failed_count": "errors",
    "input_rows": "input_rows",
    "matched": "matched", "matched_count": "matched",
    "messages": "messages", "message_count": "messages",
    "output_rows": "output_rows",
    "people": "people", "people_count": "people",
    "processed": "processed", "processed_count": "processed",
    "retries": "retries", "retry_count": "retries",
    "rows": "rows",
    "skipped": "skipped", "skipped_count": "skipped",
    "unmatched": "unmatched", "unmatched_count": "unmatched",
}

STATUS_MAP = {
    "blocked": "blocked", "blocked_approval": "blocked", "blocked_user_action": "blocked",
    "completed": "completed", "done": "completed", "indexed": "completed",
    "ok": "completed", "ready": "completed", "success": "completed",
    "selected_steps_completed": "completed", "reused": "completed",
    "noop": "completed", "ran": "completed", "research_complete": "completed",
    "approved": "completed", "empty": "completed", "skipped": "completed",
    "error": "failed", "failed": "failed", "invalid_budget": "failed",
    "in_progress": "in_progress", "pending": "in_progress", "running": "in_progress",
    "submitted": "in_progress",
    "needs_approval": "blocked", "needs_user_action": "blocked", "not_ready": "blocked",
    "awaiting_user": "blocked",
    "partial": "partial", "warn": "partial", "warning": "partial",
    "dry_run": "partial", "completed_with_errors": "partial", "observed": "partial",
}

APPROVAL_MAP = {
    "import_confirmation": "expected_approval",
    "spend": "expected_approval",
    "spend_approval": "expected_approval",
    "oauth": "oauth",
    "full_disk_access": "os_permission",
    "qr_login": "oauth",
}


def _walk_values(payload: Any, accepted_keys: set[str]) -> Iterable[tuple[str, Any]]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in accepted_keys:
                yield key, value
            if isinstance(value, (dict, list)):
                yield from _walk_values(value, accepted_keys)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, (dict, list)):
                yield from _walk_values(item, accepted_keys)


def first_number(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for _key, value in _walk_values(payload, set(keys)):
        number = nonnegative_number(value)
        if number is not None:
            return number
    return None


def first_string(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for _key, value in _walk_values(payload, set(keys)):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalized_status(payload: dict[str, Any]) -> str:
    raw = payload.get("status")
    if not isinstance(raw, str):
        raw = first_string(payload, ("status",))
    normalized = STATUS_MAP.get(str(raw or "").strip().lower(), "unknown")
    if normalized == "failed":
        nested_statuses = {
            str(value).strip().lower()
            for _key, value in _walk_values(payload, {"status"})
            if isinstance(value, str)
        }
        if nested_statuses & {"blocked", "blocked_approval", "blocked_user_action", "needs_approval"}:
            return "blocked"
    return normalized


def _counts(payload: dict[str, Any]) -> list[dict[str, str]]:
    maxima: dict[str, float] = {}
    for raw_key, value in _walk_values(payload, set(COUNT_KEYS)):
        number = nonnegative_number(value)
        if number is None:
            continue
        metric = COUNT_KEYS[raw_key]
        maxima[metric] = max(maxima.get(metric, 0), number)
    return [
        {"metric": metric, "bucket": count_bucket(value)}
        for metric, value in sorted(maxima.items())
    ]


def _approval(stage: str, payload: dict[str, Any], status: str) -> str:
    raw = payload.get("approval_type") or payload.get("gate")
    if isinstance(raw, str):
        return APPROVAL_MAP.get(raw.strip().lower(), "other_expected_action")
    if status != "blocked":
        return "none"
    if stage == "imessage_extract":
        return "os_permission"
    if stage in {"whatsapp_extract", "message_history_depth"}:
        return "oauth"
    whatsapp_provider = first_string(payload, ("whatsapp_provider",))
    if whatsapp_provider:
        return "oauth"
    continue_command = first_string(payload, ("continue_command",)) or ""
    if "--include-imessage" in continue_command:
        return "os_permission"
    return "other_expected_action"


def _cache_outcome(payload: dict[str, Any]) -> str:
    for key in ("cache_outcome", "run_mode", "mode", "action"):
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower().replace("_", "-")
        if normalized in {"cached", "cache-hit", "reused", "reuse"}:
            return "reused"
        if normalized in {"no-op", "noop", "unchanged", "skipped"}:
            return "no-op"
        if normalized in {"incremental", "updated"}:
            return "incremental"
        if normalized in {"full", "rebuilt", "created"}:
            return "full"
    if payload.get("cached") is True or payload.get("reused") is True:
        return "reused"
    return "unknown"


def _duration_seconds(payload: dict[str, Any]) -> float | None:
    timing = payload.get("timing")
    if isinstance(timing, dict):
        duration = nonnegative_number(timing.get("duration_seconds"))
        if duration is not None:
            return duration
    for key in ("duration_seconds", "elapsed_seconds", "total_seconds"):
        duration = nonnegative_number(payload.get(key))
        if duration is not None:
            return duration
    for key in ("duration_ms", "elapsed_ms", "total_ms"):
        duration_ms = nonnegative_number(payload.get(key))
        if duration_ms is not None:
            return duration_ms / 1_000
    return None


def _empty_stage(stage: str, state: str, error_code: str) -> dict[str, Any]:
    return {
        "stage": stage,
        "artifact_state": state,
        "status": "unknown",
        "model": "unknown",
        "effort": "unknown",
        "duration_bucket": "unknown",
        "count_buckets": [],
        "cache_outcome": "unknown",
        "approval_category": "none",
        "error_code": error_code,
    }


def stage_projection(stage: str, path: Path) -> tuple[dict[str, Any], dict[str, str] | None]:
    if not path.exists():
        return _empty_stage(stage, "missing", "none"), None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (
            _empty_stage(stage, "unreadable", "manifest_unreadable"),
            {"code": "manifest_unreadable", "stage": stage},
        )
    if not isinstance(payload, dict):
        return (
            _empty_stage(stage, "unreadable", "manifest_unreadable"),
            {"code": "manifest_unreadable", "stage": stage},
        )
    status = _normalized_status(payload)
    projection = {
        "stage": stage,
        "artifact_state": "present",
        "status": status,
        "model": normalized_model(payload.get("model")),
        "effort": closed_enum(
            payload.get("reasoning_effort") or payload.get("effort"), EFFORTS
        ),
        "duration_bucket": duration_bucket(_duration_seconds(payload)),
        "count_buckets": _counts(payload),
        "cache_outcome": _cache_outcome(payload),
        "approval_category": _approval(stage, payload, status),
        "error_code": "stage_failed" if status == "failed" else "none",
    }
    observation = {"code": "stage_failed", "stage": stage} if status == "failed" else None
    if status == "blocked":
        observation = {
            "code": "expected_gate" if projection["approval_category"] != "none" else "workflow_blocked",
            "stage": stage,
        }
    return projection, observation
