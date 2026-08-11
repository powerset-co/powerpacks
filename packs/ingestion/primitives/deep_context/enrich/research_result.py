"""Typed boundary for one normalized Parallel research artifact.

Parses the SQLite-stored ``result_json`` blob once, here; every downstream
reader (retarget judging, synthetic-profile assembly, candidate cards) takes
the typed dataclasses below instead of touching the provider's raw dict again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
    ParallelEducation,
    ParallelPosition,
)
from packs.ingestion.schemas.people_schema import extract_public_identifier


# Parallel's provider returns free-text notes/status, not a verified boolean —
# this tuple is the whole "did research actually confirm an identity" check,
# scanned against notes+linkedin_status in from_payload's `unverified` below.
_UNVERIFIED_MARKERS = (
    "could not directly verify",
    "could not verify",
    "unable to verify",
    "not verified",
    "unverified",
    "no confirming match",
    "not_found",
    "not found",
    "best contextual match",
    "best-guess",
    "best guess",
    "inferred",
    "no direct confirmation",
    "cannot confirm",
    "could not confirm",
)


@dataclass(frozen=True)
class ResearchIdentityProfile:
    """Judge-ready identity evidence parsed from one Parallel result."""

    public_identifier: str
    linkedin_url: str
    full_name: str
    headline: str
    profile_pic_url: str
    experiences: tuple[str, ...]
    education: tuple[str, ...]
    location: str
    reason: str
    has_profile: bool


@dataclass(frozen=True)
class ResearchPerson:
    full_name: str | None
    confidence: float
    notes: str | None
    present: bool

    @classmethod
    def from_payload(cls, payload: object) -> ResearchPerson:
        # Provider JSON is free-form; a missing/malformed block degrades to an
        # empty typed value here (and in ResearchLocation/ResearchSocial below)
        # rather than raising.
        if not isinstance(payload, dict):
            return cls(None, 0.0, None, False)
        try:
            confidence = float(payload.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        return cls(
            str(payload["full_name"]) if payload.get("full_name") else None,
            confidence,
            str(payload["notes"]) if payload.get("notes") else None,
            bool(payload),
        )


@dataclass(frozen=True)
class ResearchLocation:
    raw: str | None
    city: str | None
    state: str | None
    country: str | None
    present: bool

    @classmethod
    def from_payload(cls, payload: object) -> ResearchLocation:
        if not isinstance(payload, dict):
            return cls(None, None, None, None, False)
        return cls(
            str(payload["raw"]) if payload.get("raw") else None,
            str(payload["city"]) if payload.get("city") else None,
            str(payload["state"]) if payload.get("state") else None,
            str(payload["country"]) if payload.get("country") else None,
            bool(payload),
        )

    @property
    def display(self) -> str:
        # Prefer the provider's raw string; fall back to city/state/country
        # only when raw is empty.
        return (self.raw or "").strip() or ", ".join(
            value.strip() for value in (self.city, self.state, self.country) if value and value.strip()
        )


@dataclass(frozen=True)
class ResearchSocial:
    linkedin_url: str | None
    linkedin_status: str | None

    @classmethod
    def from_payload(cls, payload: object) -> ResearchSocial:
        if not isinstance(payload, dict):
            return cls(None, None)
        return cls(
            str(payload["linkedin_url"]).strip() if payload.get("linkedin_url") else None,
            str(payload["linkedin_status"]) if payload.get("linkedin_status") else None,
        )


@dataclass(frozen=True)
class ResearchResult:
    """One projected Parallel result, parsed once at the SQLite boundary."""

    _payload_json: str
    linkedin_url: str
    confidence: float
    reason: str
    unverified: bool
    usable: bool
    person: ResearchPerson
    location: ResearchLocation
    social: ResearchSocial
    headline: str | None
    positions: tuple[ParallelPosition, ...]
    education: tuple[ParallelEducation, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ResearchResult:
        person: ResearchPerson = ResearchPerson.from_payload(payload.get("person"))
        location: ResearchLocation = ResearchLocation.from_payload(payload.get("location"))
        social: ResearchSocial = ResearchSocial.from_payload(payload.get("social"))
        metadata: object = payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        # metadata.research_notes is the provider's own summary; person.notes is
        # the fallback when that block is absent.
        notes = str(metadata.get("research_notes") or person.notes or "").strip()
        headline_value: object = payload.get("headline")
        headline: str | None = (
            str(headline_value.get("text") or "") if isinstance(headline_value, dict) else str(headline_value or "")
        ) or None
        position_values: object = payload.get("positions")
        positions: tuple[ParallelPosition, ...] = (
            tuple(row for value in position_values if (row := ParallelPosition.from_payload(value)))
            if isinstance(position_values, list)
            else ()
        )
        education_values: object = payload.get("education")
        education: tuple[ParallelEducation, ...] = (
            tuple(row for value in education_values if (row := ParallelEducation.from_payload(value)))
            if isinstance(education_values, list)
            else ()
        )
        status = social.linkedin_status or ""
        # "Usable" means enough to act on downstream, independent of whether a
        # LinkedIn URL was found: a name plus either a real position or a
        # located city/country.
        usable = bool(
            (person.full_name or "").strip()
            and (any(row.company_name or row.title for row in positions) or location.city or location.country)
        )
        # Positional order below must match ResearchResult's field order:
        # `reason` falls back to a generic message when the provider gave no
        # notes — one that states whether a LinkedIn actually came back, so a
        # no-notes no-URL result can't carry "found a correct LinkedIn" into
        # guided review (the truthy lie used to beat guided's honest
        # "no LinkedIn found" fallback). `unverified` then scans notes+status
        # for the markers above.
        return cls(
            json.dumps(payload, ensure_ascii=False),
            social.linkedin_url or "",
            person.confidence,
            f"deep research: {notes}"
            if notes
            else ("deep research found a LinkedIn" if social.linkedin_url else "deep research found no LinkedIn"),
            any(marker in f"{notes} {status}".lower() for marker in _UNVERIFIED_MARKERS),
            usable,
            person,
            location,
            social,
            headline,
            positions,
            education,
        )

    @classmethod
    def from_json(cls, value: str | None) -> ResearchResult | None:
        try:
            payload = json.loads(value or "")
        except json.JSONDecodeError:
            return None
        return cls.from_payload(payload) if isinstance(payload, dict) else None

    def to_payload(self, *, without_linkedin: bool = False) -> dict[str, Any]:
        # assemble_synthetic_profile strips a found LinkedIn URL when calling
        # this: an unconfirmed retarget candidate should not leak into the
        # synthetic-profile artifact before research_reconcile.judging confirms
        # it separately.
        payload = json.loads(self._payload_json)
        if without_linkedin:
            social = payload.get("social")
            payload["social"] = {
                **(social if isinstance(social, dict) else {}),
                "linkedin_url": "",
            }
        return payload

    def identity_profile(self) -> ResearchIdentityProfile:
        # The judge-ready projection: research_reconcile.judging's retarget
        # judge and the candidate detail card both consume this shape, never
        # the raw payload. public_identifier goes through the same canonical
        # extract_public_identifier used everywhere else identity is compared.
        experiences = [
            f"{row.title or '?'} @ {row.company_name or '?'}" for row in self.positions if row.title or row.company_name
        ]
        education = [
            " — ".join(
                filter(
                    None,
                    (
                        ", ".join(filter(None, (str(row.degree or ""), str(row.field_of_study or "")))),
                        str(row.school_name or ""),
                    ),
                )
            )
            for row in self.education
            if row.school_name or row.degree or row.field_of_study
        ]
        place = self.location.display
        return ResearchIdentityProfile(
            public_identifier=extract_public_identifier(self.linkedin_url).lower(),
            linkedin_url=self.linkedin_url,
            full_name=self.person.full_name or "",
            headline=self.headline or "",
            profile_pic_url="",
            experiences=tuple(experiences),
            education=tuple(education),
            location=place,
            reason=self.reason,
            has_profile=bool(self.person.present or self.positions or self.education or place),
        )
