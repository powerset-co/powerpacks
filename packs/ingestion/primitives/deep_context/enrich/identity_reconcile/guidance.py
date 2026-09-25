"""Typed user-guidance request and LinkedIn hint parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

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
_LINKEDIN_RE = re.compile(
    # Locale subdomains (de.linkedin.com, uk.linkedin.com, ...) are real URLs
    # people paste. This only detects a LinkedIn URL inside free-text
    # guidance; normalize_linkedin_url — the one pinned normalizer — still
    # does the actual parsing below.
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
    # The candidate's existing/currently-attached URL, for context — not a
    # new URL the human is proposing. A user-pasted replacement is parsed out
    # of `guidance` itself; see linkedin_url_in_guidance below.
    linkedin_url: str = ""
    submitted_at: IsoTimestamp | None = None
    match_emails: tuple[str, ...] = ()
    match_phones: tuple[str, ...] = ()


def linkedin_url_in_guidance(guidance: str) -> tuple[str, str]:
    """Find (url, public_identifier) if guidance text names a LinkedIn URL.

    ``("", "")`` means none found. A match is the fast path: the caller can
    settle identity directly from a pasted URL without spending a
    deep-research call at all — see GuidedRetargetWorker.submit.
    """
    match = _LINKEDIN_RE.search(guidance)
    if not match:
        return "", ""
    raw = match.group(0)
    url = normalize_linkedin_url(
        raw if raw.lower().startswith("http") else f"https://{raw}"
    )
    public_identifier = extract_public_identifier(url).lower()
    return (url, public_identifier) if public_identifier else ("", "")
