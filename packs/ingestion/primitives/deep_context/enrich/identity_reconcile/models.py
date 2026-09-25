"""Frozen rows for guided-research outcomes and judge profile sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp


@dataclass(frozen=True)
class GuidanceOutcome:
    """One durable guidance result before its HTTP/JSON serialization edge."""

    slug: str
    row_key: str
    name: str
    guidance: str
    state: str
    detail: str
    submitted_at: IsoTimestamp | None
    updated_at: IsoTimestamp
    new_url: str = ""
    resolved_pubs: tuple[str, ...] = ()
    candidate_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Serialize with optional fields omitted, not present-but-empty."""
        values: dict[str, Any] = {
            "slug": self.slug,
            "row_key": self.row_key,
            "name": self.name,
            "guidance": self.guidance,
            "state": self.state,
            "detail": self.detail,
            "submitted_at": self.submitted_at,
            "updated_at": self.updated_at,
        }
        if self.new_url:
            values["new_url"] = self.new_url
        if self.resolved_pubs:
            values["resolved_pubs"] = list(self.resolved_pubs)
        if self.candidate_url:
            values["candidate_url"] = self.candidate_url
        return values


@dataclass(frozen=True)
class IdentityProfileSource:
    """Typed SQLite row used to build the normalized judge profile.

    Every field defaults empty so a hydrated ProfileResult (fresher, richer)
    can fully override this row rather than merge with it — see
    queue.linkedin_view.
    """

    public_identifier: str = ""
    linkedin_url: str = ""
    display_name: str = ""
    full_name: str = ""
    headline: str = ""
    profile_picture_url: str = ""
