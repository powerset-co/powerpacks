"""Frozen rows for synthetic-profile assembly: `SyntheticResearchProfile` is
the evidence side (parsed once from a Parallel research payload);
`SyntheticCsvRow` is the CSV/JSON round-trip wrapper for an assembled row."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.primitives.deep_context.shared.coerce import compact_json, number, text


@dataclass(frozen=True)
class SyntheticPosition:
    title: str | None
    company_name: str | None
    start_date: str | None
    is_current: bool
    _payload_json: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SyntheticPosition:
        return cls(
            text(payload.get("title")),
            text(payload.get("company_name")),
            text(payload.get("start_date")),
            bool(payload.get("is_current")),
            compact_json(payload),
        )

    @property
    def key(self) -> tuple[str, str, str]:
        # Deliberately excludes is_current/end_date: the same role logged
        # slightly differently across two research runs still dedupes to one
        # entry in _merge_profiles's unique_positions.
        return tuple(
            (value or "").strip().lower()
            for value in (self.company_name, self.title, self.start_date)
        )

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


@dataclass(frozen=True)
class SyntheticEducation:
    school_name: str | None
    school: str | None
    degree: str | None
    _payload_json: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SyntheticEducation:
        return cls(
            text(payload.get("school_name")),
            text(payload.get("school")),
            text(payload.get("degree")),
            compact_json(payload),
        )

    @property
    def key(self) -> tuple[str, str, str]:
        return tuple(
            (value or "").strip().lower()
            for value in (self.school_name, self.school, self.degree)
        )

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


@dataclass(frozen=True)
class SyntheticResearchProfile:
    """The evidence side of a synthetic profile: every field here is pulled
    straight from one Parallel deep-research payload. `build_synthetic_row`
    (synthetic/assemble.py) copies these through as-is and adds its
    own derived fields (id, public_identifier, entity_urn, approved) on top."""

    full_name: str | None
    first_name: str | None
    last_name: str | None
    # Coerced the same way as parallel_research.result.ResearchPerson.confidence: a
    # missing/non-numeric provider value degrades to 0.0 here instead of
    # landing in synthetic_metadata["name_confidence"] with whatever type
    # (string, None, ...) the provider happened to send.
    name_confidence: float
    city: str | None
    state: str | None
    country: str | None
    location_raw: str | None
    headline: str | None
    summary: str | None
    positions: tuple[SyntheticPosition, ...]
    education: tuple[SyntheticEducation, ...]
    twitter_handle: str | None
    linkedin_url: str | None
    completeness: float
    gaps: tuple[str, ...]
    research_date: str | None
    research_method: str | None
    source_channel: str | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SyntheticResearchProfile:
        person = payload.get("person") if isinstance(payload.get("person"), dict) else {}
        location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        social = payload.get("social") if isinstance(payload.get("social"), dict) else {}
        headline = payload.get("headline") if isinstance(payload.get("headline"), dict) else {}
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        return cls(
            text(person.get("full_name")),
            text(person.get("first_name")),
            text(person.get("last_name")),
            number(person.get("confidence"), 0.0),
            text(location.get("city")),
            text(location.get("state")),
            text(location.get("country")),
            text(location.get("raw")),
            text(headline.get("text")),
            text(summary.get("text")),
            tuple(
                SyntheticPosition.from_payload(row)
                for row in payload.get("positions") or ()
                if isinstance(row, dict)
            ),
            tuple(
                SyntheticEducation.from_payload(row)
                for row in payload.get("education") or ()
                if isinstance(row, dict)
            ),
            text(social.get("twitter_handle")),
            text(social.get("linkedin_url")),
            number(metadata.get("estimated_completeness"), 0.0),
            tuple(str(value).strip() for value in metadata.get("gaps") or () if str(value).strip()),
            text(metadata.get("research_date")),
            text(metadata.get("research_method")),
            text(metadata.get("source_channel")),
        )

    @classmethod
    def from_result(cls, result: ResearchResult) -> SyntheticResearchProfile:
        """The one research-to-synthetic door. linkedin_url is always dropped:
        every row reaching synthetic assembly either found no LinkedIn or is a
        rejected retarget whose unconfirmed URL must not leak into the
        synthetic artifact (research_reconcile.judging confirms URLs
        separately). Everything else — twitter_handle included — carries over
        from the research payload untouched."""
        return replace(cls.from_payload(result.to_payload()), linkedin_url=None)


@dataclass(frozen=True)
class SyntheticCsvRow:
    """CSV/JSON round-trip wrapper for one assembled row. Only the fields call
    sites actually branch on are materialized as typed attributes; the rest
    of the ~30 people-schema columns live solely in `_payload_json`, reached
    through `to_payload()`."""

    public_identifier: str
    approved: str | None
    full_name: str | None
    linkedin_url: str | None
    source_parent_slug: str | None
    _payload_json: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SyntheticCsvRow:
        social = payload.get("social") if isinstance(payload.get("social"), dict) else {}
        return cls(
            str(payload.get("public_identifier") or "").lower(),
            text(payload.get("approved")),
            text(payload.get("full_name")),
            text(payload.get("linkedin_url") or social.get("linkedin_url")),
            text(payload.get("source_parent_slug")),
            compact_json(payload),
        )

    @classmethod
    def from_json(
        cls,
        value: str,
        *,
        approved: str | None = None,
    ) -> SyntheticCsvRow | None:
        try:
            payload = json.loads(value or "{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        row = cls.from_payload(payload)
        return row.with_approved(approved) if approved is not None else row

    def with_approved(self, approved: str | None) -> SyntheticCsvRow:
        """Flip only `approved`, rebuilding from the full payload — how a
        prior human yes/no survives a re-assembled row (see
        AssembleSyntheticProfile.execute)."""
        payload = self.to_payload()
        payload["approved"] = approved or ""
        return self.from_payload(payload)

    def to_payload(self) -> dict[str, str]:
        return {
            str(key): str(value or "")
            for key, value in json.loads(self._payload_json).items()
        }
