"""Typed boundary for one Parallel JSON output.

Parallel owns the persisted provider envelope (``content`` plus ``basis``).
Powerpacks parses that envelope once into the small projection consumed by
identity judging, synthetic-profile assembly, and candidate cards.
"""

from __future__ import annotations

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
    def from_payload(cls, payload: dict[str, object]) -> ResearchPosition:
        return cls(
            text(payload.get("title")),
            text(payload.get("company_name")),
            text(payload.get("company_domain")),
            text(payload.get("description")),
            text(payload.get("start_date")),
            text(payload.get("end_date")),
            boolean(payload.get("is_current")),
        )

@dataclass(frozen=True)
class ResearchEducation:
    school_name: str | None
    degree: str | None = None
    field_of_study: str | None = None
    start_year: str | None = None
    end_year: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ResearchEducation:
        return cls(
            text(payload.get("school_name")),
            text(payload.get("degree")),
            text(payload.get("field_of_study")),
            text(payload.get("start_year")),
            text(payload.get("end_year")),
        )

@dataclass(frozen=True)
class ResearchPerson:
    full_name: str | None


@dataclass(frozen=True)
class ResearchLocation:
    city: str | None
    country: str | None

    @property
    def display(self) -> str:
        return ", ".join(value for value in (self.city, self.country) if value)


@dataclass(frozen=True)
class ResearchResult:
    """One canonical typed projection of the SDK-owned output envelope."""

    output: TaskRunJsonOutput
    linkedin_url: str
    reason: str
    usable: bool
    person: ResearchPerson
    location: ResearchLocation
    summary: str | None
    positions: tuple[ResearchPosition, ...]
    education: tuple[ResearchEducation, ...]
    @classmethod
    def from_output(cls, output: TaskRunJsonOutput) -> ResearchResult:
        content = output.content
        positions_value = content.get("work_experience")
        education_value = content.get("education")
        if not isinstance(positions_value, list) or not isinstance(education_value, list):
            raise ValueError("Parallel work_experience and education must be arrays")
        if any(not isinstance(value, dict) for value in positions_value):
            raise ValueError("Parallel work_experience entries must be objects")
        if any(not isinstance(value, dict) for value in education_value):
            raise ValueError("Parallel education entries must be objects")
        positions = tuple(ResearchPosition.from_payload(value) for value in positions_value)
        education = tuple(ResearchEducation.from_payload(value) for value in education_value)
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
        return cls(
            output,
            linkedin_url,
            reason,
            bool(real_name and (positions or city or country)),
            ResearchPerson(real_name),
            ResearchLocation(city, country),
            text(content.get("summary")),
            positions,
            education,
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ResearchResult:
        return cls.from_output(TaskRunJsonOutput.model_validate(payload))

    @classmethod
    def from_json(cls, value: str | None) -> ResearchResult | None:
        try:
            return cls.from_output(TaskRunJsonOutput.model_validate_json(value or ""))
        except ValueError:
            return None

    @property
    def basis(self) -> tuple[FieldBasis, ...]:
        return tuple(self.output.basis)

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
            has_profile=bool(self.person.full_name or self.positions or self.education or place),
            _present=RESEARCH_PRESENT_FIELDS,
        )
