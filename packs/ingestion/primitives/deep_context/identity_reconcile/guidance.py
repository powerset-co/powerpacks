"""Typed user-guidance request and LinkedIn hint parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context.db.models import GuidanceState, IsoTimestamp
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    normalize_linkedin_url,
)

ACTIVE_GUIDANCE_STATES = {
    GuidanceState.PENDING.value,
    GuidanceState.RUNNING.value,
}
_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_%.\-]+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GuidanceRequest:
    slug: str
    row_key: str
    name: str
    guidance: str
    person_ids: tuple[str, ...] = ()
    linkedin_url: str = ""
    submitted_at: IsoTimestamp | None = None
    match_emails: tuple[str, ...] = ()
    match_phones: tuple[str, ...] = ()


def linkedin_url_in_guidance(guidance: str) -> tuple[str, str]:
    match = _LINKEDIN_RE.search(guidance)
    if not match:
        return "", ""
    raw = match.group(0)
    url = normalize_linkedin_url(
        raw if raw.lower().startswith("http") else f"https://{raw}"
    )
    public_identifier = extract_public_identifier(url).lower()
    return (url, public_identifier) if public_identifier else ("", "")
