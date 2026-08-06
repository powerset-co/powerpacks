"""Queue parsing, dossier input shaping, fingerprinting, and paid-result reuse."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from packs.shared.csv_io import CsvIO


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


def load_queue(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise SystemExit(f"input CSV not found: {path}")
    return CsvIO.read_dict_rows_normalized(path)


def filter_already_done(
    rows: list[dict[str, str]],
    output_dir: Path,
) -> tuple[list[dict[str, str]], int]:
    """Reuse exact paid outputs; changed inputs overwrite the fixed path."""
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
        path = output_dir / handle / "01_research_parallel.json"
        if path.is_file():
            try:
                prior = json.loads(path.read_text(encoding="utf-8"))
                stored = str(
                    (prior.get("metadata") or {}).get("input_fingerprint") or ""
                )
            except (AttributeError, json.JSONDecodeError, OSError):
                stored = "invalid"
            if not stored or stored == input_fingerprint(row, handle):
                skipped += 1
                continue
        todo.append(row)
    return todo, skipped
