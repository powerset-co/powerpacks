"""Frozen rows for synthetic-profile assembly."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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
            str(payload["title"]) if payload.get("title") else None,
            str(payload["company_name"]) if payload.get("company_name") else None,
            str(payload["start_date"]) if payload.get("start_date") else None,
            bool(payload.get("is_current")),
            _json(payload),
        )

    @property
    def key(self) -> tuple[str, str, str]:
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
            str(payload["school_name"]) if payload.get("school_name") else None,
            str(payload["school"]) if payload.get("school") else None,
            str(payload["degree"]) if payload.get("degree") else None,
            _json(payload),
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
    full_name: str | None
    first_name: str | None
    last_name: str | None
    name_confidence: object
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
            str(person["full_name"]) if person.get("full_name") else None,
            str(person["first_name"]) if person.get("first_name") else None,
            str(person["last_name"]) if person.get("last_name") else None,
            person.get("confidence"),
            str(location["city"]) if location.get("city") else None,
            str(location["state"]) if location.get("state") else None,
            str(location["country"]) if location.get("country") else None,
            str(location["raw"]) if location.get("raw") else None,
            str(headline["text"]) if headline.get("text") else None,
            str(summary["text"]) if summary.get("text") else None,
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
            str(social["twitter_handle"]) if social.get("twitter_handle") else None,
            str(social["linkedin_url"]) if social.get("linkedin_url") else None,
            float(metadata.get("estimated_completeness") or 0),
            tuple(str(value).strip() for value in metadata.get("gaps") or () if str(value).strip()),
            str(metadata["research_date"]) if metadata.get("research_date") else None,
            str(metadata["research_method"]) if metadata.get("research_method") else None,
            str(metadata["source_channel"]) if metadata.get("source_channel") else None,
        )


@dataclass(frozen=True)
class SyntheticCsvRow:
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
            str(payload["approved"]) if payload.get("approved") else None,
            str(payload["full_name"]) if payload.get("full_name") else None,
            str(payload.get("linkedin_url") or social.get("linkedin_url") or "") or None,
            str(payload["source_parent_slug"]) if payload.get("source_parent_slug") else None,
            _json(payload),
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
        payload = self.to_payload()
        payload["approved"] = approved or ""
        return self.from_payload(payload)

    def to_payload(self) -> dict[str, str]:
        return {
            str(key): str(value or "")
            for key, value in json.loads(self._payload_json).items()
        }
