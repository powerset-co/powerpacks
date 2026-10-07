"""The operator's profile: read owner.json into the store, render it for prompts. No provider call.

Emails and phones are normalized here, once, with the same normalizers the import uses for
candidate identifiers, so a caller compares values and never normalizes again.

Created: 2026-10-06
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from packs.ingestion.primitives.common.contact_fields import normalize_email, normalize_phone
from packs.ingestion.primitives.deep_context_v2.db import queries
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
    # owner.json stores 0 for a current job's end; 0 means "no end", which renders as "present".
    return "" if value is None or value == 0 else str(value).strip()


def _strings(value: object) -> tuple[str, ...]:
    items = []
    for item in value or []:
        if _text(item):
            items.append(_text(item))
    return tuple(items)


def owner_from_payload(payload: dict[str, object]) -> OwnerProfile:
    emails = []
    for email in _strings(payload.get("emails")):
        emails.append(normalize_email(email))
    phones = []
    for phone in _strings(payload.get("phones")):
        phones.append(normalize_phone(phone))
    education = []
    for e in payload.get("education") or []:
        education.append(OwnerEducation(_text(e.get("school")), _text(e.get("start")), _text(e.get("end")), _text(e.get("note"))))
    work = []
    for w in payload.get("work") or []:
        work.append(OwnerWork(_text(w.get("company")), _text(w.get("title")), _text(w.get("start")), _text(w.get("end"))))
    return OwnerProfile(
        name=_text(payload.get("name")),
        emails=tuple(emails),
        phones=tuple(phones),
        linkedin_url=_text(payload.get("linkedin_url")),
        education=tuple(education),
        work=tuple(work),
        locations=_strings(payload.get("locations")),
        notes=_text(payload.get("notes")),
    )


def load_owner(conn: sqlite3.Connection, owner_json: Path) -> OwnerProfile:
    """Project owner.json into the `owner` row. The file is the operator's own, written at setup."""
    raw: bytes = owner_json.read_bytes()
    payload: dict[str, object] = json.loads(raw)
    queries.upsert_owner(conn, json.dumps(payload, ensure_ascii=False, sort_keys=True), hashlib.sha256(raw).hexdigest(), now_iso())
    return owner_from_payload(payload)


def read_owner(conn: sqlite3.Connection) -> OwnerProfile:
    return owner_from_payload(json.loads(queries.owner_payload_json(conn)))


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
