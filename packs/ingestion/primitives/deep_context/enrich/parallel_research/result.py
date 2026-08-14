"""Typed boundary for one Parallel JSON output.

Parallel owns the persisted provider envelope (``content`` plus ``basis``).
Powerpacks parses that envelope once into the small projection consumed by
identity judging, synthetic-profile assembly, and candidate cards.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from parallel.types import FieldBasis, TaskRunJsonOutput

from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    RESEARCH_PRESENT_FIELDS,
    JudgeProfile,
)
from packs.ingestion.primitives.deep_context.shared.coerce import boolean, clean_text, text
from packs.ingestion.schemas.people_schema import extract_public_identifier


@dataclass(frozen=True)
class ResearchPosition:
    title: str | None
    company_name: str | None
    company_domain: str | None = None
    description: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False

    @classmethod
    def from_payload(cls, payload: object) -> ResearchPosition | None:
        if not isinstance(payload, dict):
            return None
        return cls(
            text(payload.get("title")),
            text(payload.get("company_name")),
            text(payload.get("company_domain")),
            text(payload.get("description")),
            text(payload.get("start_date")),
            text(payload.get("end_date")),
            boolean(payload.get("is_current")),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "title": self.title,
            "company_name": self.company_name,
            "company_domain": self.company_domain,
            "description": self.description,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "is_current": self.is_current,
        }


@dataclass(frozen=True)
class ResearchEducation:
    school_name: str | None
    degree: str | None = None
    field_of_study: str | None = None
    start_year: str | None = None
    end_year: str | None = None

    @classmethod
    def from_payload(cls, payload: object) -> ResearchEducation | None:
        if not isinstance(payload, dict):
            return None
        return cls(
            text(payload.get("school_name")),
            text(payload.get("degree")),
            text(payload.get("field_of_study")),
            text(payload.get("start_year")),
            text(payload.get("end_year")),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "school_name": self.school_name,
            "degree": self.degree,
            "field_of_study": self.field_of_study,
            "start_year": self.start_year,
            "end_year": self.end_year,
        }


@dataclass(frozen=True)
class ResearchPerson:
    full_name: str | None
    present: bool


@dataclass(frozen=True)
class ResearchLocation:
    city: str | None
    country: str | None

    @property
    def display(self) -> str:
        return ", ".join(value for value in (self.city, self.country) if value)


@dataclass(frozen=True)
class ResearchSocial:
    linkedin_url: str | None
    github_url: str | None


@dataclass(frozen=True)
class ResearchResult:
    """One canonical typed projection of the SDK-owned output envelope."""

    _payload_json: str
    linkedin_url: str
    reason: str
    usable: bool
    person: ResearchPerson
    location: ResearchLocation
    social: ResearchSocial
    summary: str | None
    positions: tuple[ResearchPosition, ...]
    education: tuple[ResearchEducation, ...]
    basis: tuple[FieldBasis, ...]

    @classmethod
    def from_output(cls, output: TaskRunJsonOutput) -> ResearchResult:
        content = output.content
        positions_value = content.get("work_experience")
        education_value = content.get("education")
        if not isinstance(positions_value, list) or not isinstance(education_value, list):
            raise ValueError("Parallel work_experience and education must be arrays")
        positions = tuple(
            row for value in positions_value if (row := ResearchPosition.from_payload(value))
        )
        education = tuple(
            row for value in education_value if (row := ResearchEducation.from_payload(value))
        )
        real_name = text(content.get("real_name"))
        city = text(content.get("location_city"))
        country = text(content.get("location_country"))
        linkedin_url = clean_text(content.get("linkedin_url")) or ""
        basis = tuple(output.basis)
        relevant_basis = next(
            (item for field in ("linkedin_url", "real_name") for item in basis if item.field == field),
            None,
        )
        reason = (
            f"deep research: {relevant_basis.reasoning.strip()}"
            if relevant_basis and relevant_basis.reasoning.strip()
            else ("deep research found a LinkedIn" if linkedin_url else "deep research found no LinkedIn")
        )
        payload = output.model_dump(mode="json", exclude_none=True)
        return cls(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            linkedin_url,
            reason,
            bool(real_name and (positions or city or country)),
            ResearchPerson(real_name, bool(real_name)),
            ResearchLocation(city, country),
            ResearchSocial(linkedin_url or None, clean_text(content.get("github_url"))),
            text(content.get("summary")),
            positions,
            education,
            basis,
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ResearchResult:
        if payload.get("type") == "json" and isinstance(payload.get("content"), dict):
            return cls.from_output(TaskRunJsonOutput.model_validate(payload))
        return cls._from_legacy_payload(payload)

    @classmethod
    def _from_legacy_payload(cls, payload: dict[str, Any]) -> ResearchResult:
        """Read pre-v1.19 normalized SQLite rows; delete after v1.19 is the floor."""
        person = payload.get("person") if isinstance(payload.get("person"), dict) else {}
        location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
        social = payload.get("social") if isinstance(payload.get("social"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        headline = payload.get("headline") if isinstance(payload.get("headline"), dict) else {}
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        content = {
            "real_name": text(person.get("full_name")),
            "work_experience": payload.get("positions") if isinstance(payload.get("positions"), list) else [],
            "education": payload.get("education") if isinstance(payload.get("education"), list) else [],
            "location_city": text(location.get("city")),
            "location_country": text(location.get("country")),
            "linkedin_url": clean_text(social.get("linkedin_url")),
            "github_url": clean_text(social.get("github_url")),
            "summary": text(summary.get("text")) or text(headline.get("text")) or "",
        }
        notes = text(metadata.get("research_notes")) or text(person.get("notes"))
        basis = ([{"field": "real_name", "reasoning": notes, "citations": []}] if notes else [])
        return cls.from_output(TaskRunJsonOutput(type="json", content=content, basis=basis))

    @classmethod
    def from_json(cls, value: str | None) -> ResearchResult | None:
        try:
            payload = json.loads(value or "")
            return cls.from_payload(payload) if isinstance(payload, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)

    def identity_profile(self) -> JudgeProfile:
        experiences = [
            f"{row.title or '?'} @ {row.company_name or '?'}"
            for row in self.positions
            if row.title or row.company_name
        ]
        education = [
            " — ".join(filter(None, (", ".join(filter(None, (row.degree, row.field_of_study))), row.school_name)))
            for row in self.education
            if row.school_name or row.degree or row.field_of_study
        ]
        place = self.location.display
        return JudgeProfile(
            public_identifier=extract_public_identifier(self.linkedin_url).lower(),
            linkedin_url=self.linkedin_url,
            full_name=self.person.full_name or "",
            headline=self.summary or "",
            profile_pic_url="",
            experiences=tuple(experiences),
            education=tuple(education),
            location=place,
            reason=self.reason,
            has_profile=bool(self.person.present or self.positions or self.education or place),
            _present=RESEARCH_PRESENT_FIELDS,
        )
