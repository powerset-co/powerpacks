"""The operator's profile: read owner.json into the store, render it for prompts. No provider call.

Created: 2026-10-06
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db.store import now_iso


@dataclass(frozen=True)
class OwnerEducation:
    school: str
    start: str
    end: str
    note: str


@dataclass(frozen=True)
class OwnerWork:
    company: str
    title: str
    start: str
    end: str


@dataclass(frozen=True)
class OwnerProfile:
    name: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    linkedin_url: str
    education: tuple[OwnerEducation, ...]
    work: tuple[OwnerWork, ...]
    locations: tuple[str, ...]
    notes: str


def _text(value: object) -> str:
    # owner.json stores 0 for a current job's end; v1 kept the number and its falsiness printed
    # "present". Found 2026-10-06 when the v2 system prompt did not hash equal to v1's.
    return "" if value is None or value == 0 else str(value).strip()


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"expected a list, got {type(value).__name__}")
    return tuple(_text(item) for item in value if _text(item))


def owner_from_payload(payload: dict[str, object]) -> OwnerProfile:
    education = payload.get("education", [])
    work = payload.get("work", [])
    if not isinstance(education, list) or not isinstance(work, list):
        raise ValueError("owner.json: education and work must be lists")
    return OwnerProfile(
        name=_text(payload.get("name")),
        emails=_strings(payload.get("emails", [])),
        phones=_strings(payload.get("phones", [])),
        linkedin_url=_text(payload.get("linkedin_url")),
        education=tuple(
            OwnerEducation(_text(e.get("school")), _text(e.get("start")), _text(e.get("end")), _text(e.get("note")))
            for e in education
        ),
        work=tuple(
            OwnerWork(_text(w.get("company")), _text(w.get("title")), _text(w.get("start")), _text(w.get("end")))
            for w in work
        ),
        locations=_strings(payload.get("locations", [])),
        notes=_text(payload.get("notes")),
    )


def load_owner(conn: sqlite3.Connection, owner_json: Path) -> OwnerProfile:
    """Project owner.json into the `owner` row. The file is the operator's own, written at setup."""
    raw = owner_json.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"{owner_json} must contain a JSON object")
    owner = owner_from_payload(payload)
    conn.execute(
        "INSERT INTO owner (owner_key, payload_json, content_fingerprint, projected_at) VALUES ('owner', ?, ?, ?) "
        "ON CONFLICT (owner_key) DO UPDATE SET payload_json = excluded.payload_json, "
        "content_fingerprint = excluded.content_fingerprint, projected_at = excluded.projected_at",
        (json.dumps(payload, ensure_ascii=False, sort_keys=True), hashlib.sha256(raw).hexdigest(), now_iso()),
    )
    return owner


def read_owner(conn: sqlite3.Connection) -> OwnerProfile:
    row = conn.execute("SELECT payload_json FROM owner WHERE owner_key = 'owner'").fetchone()
    if row is None:
        raise LookupError("owner row missing: run the import block first")
    return owner_from_payload(json.loads(row["payload_json"]))


def _span(start: str, end: str) -> str:
    if start and end:
        return f"{start}-{end}"
    if end:
        return f"until {end}"
    if start:
        return f"{start}-present"
    return "dates unknown"


def owner_background_block(owner: OwnerProfile) -> str:
    """The owner's bio as a prompt block. Byte-identical to v1 `shared/common.py:owner_background_block`."""
    lines = [f"MAILBOX OWNER BACKGROUND (me): {owner.name}".strip()]
    for education in owner.education:
        note = f" ({education.note})" if education.note else ""
        lines.append(f"- School: {education.school} [{_span(education.start, education.end)}]{note}")
    for job in owner.work:
        title = f" as {job.title}" if job.title else ""
        lines.append(f"- Work: {job.company}{title} [{_span(job.start, job.end)}]")
    if owner.locations:
        lines.append(f"- Locations over time: {', '.join(owner.locations)}")
    if owner.notes:
        lines.append(f"- Notes: {owner.notes}")
    return "\n".join(lines)
