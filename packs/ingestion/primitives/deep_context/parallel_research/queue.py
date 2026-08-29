"""Queue parsing, dossier input shaping, fingerprinting, and paid-result reuse."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

from packs.ingestion.primitives.deep_context.db.models import ArtifactRow


def candidate_handle(row: dict[str, str]) -> str:
    """Return the stable fixed-directory key for one queue row."""
    handle = (row.get("handle") or "").strip()
    if handle:
        return handle
    email = (row.get("primary_email") or "").strip()
    if email:
        return email.split("@", 1)[0].lower().replace(".", "_")
    digits = re.sub(r"\D", "", row.get("phone_e164") or "")
    if digits:
        return f"phone-{digits[-10:]}"
    name = " ".join(
        value.strip()
        for value in (
            row.get("display_name") or row.get("first_name") or "",
            row.get("last_name") or "",
        )
        if value.strip()
    ).lower()
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_") or "unknown"


def build_input(row: dict[str, str], handle: str) -> dict[str, Any]:
    """Collapse a queue row into one dossier plus optional human guidance."""
    name = (row.get("display_name") or "").strip()
    if not name:
        name = " ".join(
            value.strip()
            for value in (row.get("first_name") or "", row.get("last_name") or "")
            if value.strip()
        )
    guidance = (row.get("retarget_hint") or "").strip()
    known = (row.get("known_info") or "").strip()
    if guidance and known.startswith(guidance):
        known = known[len(guidance) :].strip()
    lines = [f"Name: {name or handle}"]
    for label, value in (
        ("Relationship dossier", row.get("bio") or ""),
        ("Email", row.get("primary_email") or ""),
        ("Phone", row.get("phone_e164") or ""),
        ("Area code", row.get("area_code") or ""),
        ("Company domain", row.get("domain") or ""),
        ("Website", row.get("website_url") or ""),
        ("Additional context", known),
    ):
        text = str(value).strip()
        if text:
            lines.append(f"{label}: {text}")
    payload: dict[str, Any] = {"handle": handle, "dossier": "\n".join(lines)}
    if guidance:
        payload["guidance"] = guidance
    return payload


def input_fingerprint(row: dict[str, str], handle: str) -> str:
    """Return the pinned paid-cache key for one canonical provider input."""
    data = json.dumps(
        build_input(row, handle),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def filter_already_done(
    rows: list[dict[str, str]],
    artifacts: Iterable[ArtifactRow],
) -> tuple[list[dict[str, str]], int]:
    """Reuse projected paid outputs; changed inputs overwrite the fixed path."""
    completed = {
        artifact.artifact_key.removeprefix("research:").lower(): artifact.input_fingerprint
        for artifact in artifacts
        if artifact.kind == "research" and artifact.status == "projected"
    }
    todo: list[dict[str, str]] = []
    skipped = 0
    seen: set[str] = set()
    for source in rows:
        row = dict(source)
        handle = candidate_handle(row)
        if handle in seen:
            continue
        seen.add(handle)
        row["handle"] = handle
        if handle.lower() in completed:
            stored = str(completed[handle.lower()] or "")
            if not stored or stored == input_fingerprint(row, handle):
                skipped += 1
                continue
        todo.append(row)
    return todo, skipped
