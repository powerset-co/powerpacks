"""The operator's profile: read owner.json into the store, render it for prompts. No provider call.

The file is written once by `deep_context_v2/owner.py` (the `owner` command) from the owner's own
LinkedIn. Emails and phones are normalized here, once, with the same normalizers the import uses for
candidate identifiers, so a caller compares values and never normalizes again.

Created: 2026-10-06
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, field_validator

from packs.ingestion.primitives.common.contact_fields import normalize_email, normalize_phone
from packs.ingestion.primitives.deep_context_v2.db import queries
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso



def _year(value: int) -> str:
    # owner.json stores a year, or 0 when unknown or, for an end, current; 0 renders as "present".
    return "" if value == 0 else str(value)


Year = Annotated[str, BeforeValidator(_year)]


class OwnerEducation(BaseModel):
    model_config = ConfigDict(frozen=True)

    school: str
    start: Year
    end: Year
    note: str


class OwnerWork(BaseModel):
    model_config = ConfigDict(frozen=True)

    company: str
    title: str
    start: Year
    end: Year


class OwnerProfile(BaseModel):
    """owner.json, parsed. Emails and phones are normalized here, once."""

    model_config = ConfigDict(frozen=True)

    name: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    linkedin_url: str
    education: tuple[OwnerEducation, ...]
    work: tuple[OwnerWork, ...]
    locations: tuple[str, ...]
    notes: str

    @field_validator("emails")
    @classmethod
    def _normalize_emails(cls, emails: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for email in emails:
            normalized.append(normalize_email(email))
        return tuple(normalized)

    @field_validator("phones")
    @classmethod
    def _normalize_phones(cls, phones: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for phone in phones:
            normalized.append(normalize_phone(phone))
        return tuple(normalized)


def load_owner(conn: sqlite3.Connection, owner_json: Path) -> OwnerProfile:
    """Project owner.json into the `owner` row. The file is the operator's own, written at setup."""
    raw: bytes = owner_json.read_bytes()
    payload: dict[str, object] = json.loads(raw)
    owner = OwnerProfile.model_validate(payload)
    queries.upsert_owner(conn, json.dumps(payload, ensure_ascii=False, sort_keys=True), hashlib.sha256(raw).hexdigest(), now_iso())
    return owner


def read_owner(conn: sqlite3.Connection) -> OwnerProfile:
    return OwnerProfile.model_validate_json(queries.owner_payload_json(conn))


def _span(start: str, end: str) -> str:
    if start and end:
        return f"{start}-{end}"
    if end:
        return f"until {end}"
    if start:
        return f"{start}-present"
    return "dates unknown"


def owner_background_block(owner: OwnerProfile) -> str:
    """The owner's bio as a prompt block: v1 `shared/common.py:owner_background_block` plus one line naming the
    owner's email domains, so a judge can tie an employer LinkedIn lists under its legal name (Brain of Things)
    to the name a profile uses (Caspar AI, from arthur@caspar.ai)."""
    lines = [f"MAILBOX OWNER BACKGROUND (me): {owner.name}".strip()]
    for education in owner.education:
        note = f" ({education.note})" if education.note else ""
        lines.append(f"- School: {education.school} [{_span(education.start, education.end)}]{note}")
    for job in owner.work:
        title = f" as {job.title}" if job.title else ""
        lines.append(f"- Work: {job.company}{title} [{_span(job.start, job.end)}]")
    domains: list[str] = []
    for email in owner.emails:
        domain = email.rsplit("@", 1)[-1]
        if domain not in domains:
            domains.append(domain)
    if domains:
        lines.append(f"- My email domains, past and present employers among them: {', '.join(domains)}")
    if owner.locations:
        lines.append(f"- Locations over time: {', '.join(owner.locations)}")
    if owner.notes:
        lines.append(f"- Notes: {owner.notes}")
    return "\n".join(lines)
