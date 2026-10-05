"""Typed user-guidance request and LinkedIn hint parsing."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from packs.ingestion.primitives.deep_context.db.models import GuidanceState, IsoTimestamp
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    normalize_linkedin_url,
)

# The coarse persisted `guidance` table state (GuidanceRow/GuidanceSnapshotRow):
# only ever pending/running/applied/failed. Do not confuse with the
# fine-grained progress code carried in GuidanceOutcome.state/detail_json
# ("queued", "researching", "no_match", ...) — review/server.py's
# IN_FLIGHT_RETARGET_STATES covers that unrelated, wire-level vocabulary.
ACTIVE_GUIDANCE_STATES = {
    GuidanceState.PENDING.value,
    GuidanceState.RUNNING.value,
}


@dataclass(frozen=True)
class GuidanceRequest:
    slug: str
    row_key: str
    name: str
    guidance: str
    person_ids: tuple[str, ...] = ()
    # The candidate's existing/currently-attached URL, for context — not a
    # new URL the human is proposing. A user-pasted replacement is parsed out
    # of `guidance` itself; see linkedin_url_in_guidance below.
    linkedin_url: str = ""
    submitted_at: IsoTimestamp | None = None
    match_emails: tuple[str, ...] = ()
    match_phones: tuple[str, ...] = ()


def linkedin_url_in_guidance(guidance: str) -> tuple[str, str]:
    """Accept a standalone supported profile URL as an explicit human choice."""
    text = guidance.strip()
    if not text or any(character.isspace() for character in text):
        return "", ""
    parsed = urlsplit(text if "://" in text else f"https://{text}")
    if not (parsed.hostname and (parsed.hostname == "linkedin.com" or parsed.hostname.endswith(".linkedin.com"))
            and parsed.path.startswith("/in/")):
        return "", ""
    url = normalize_linkedin_url(text)
    public_identifier = extract_public_identifier(url).lower()
    return (url, public_identifier) if public_identifier else ("", "")
