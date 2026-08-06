"""Typed boundary for one normalized Parallel research artifact."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.schemas.people_schema import extract_public_identifier


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
class ResearchResult:
    """One ``01_research_parallel.json``, parsed once at the file boundary."""

    _payload_json: str
    linkedin_url: str
    confidence: float
    reason: str
    unverified: bool
    usable: bool

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ResearchResult:
        person = payload.get("person") or {}
        metadata = payload.get("metadata") or {}
        social = payload.get("social") or {}
        location = payload.get("location") or {}
        notes = str(metadata.get("research_notes") or person.get("notes") or "").strip()
        try:
            confidence = float(person.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        status = str(social.get("linkedin_status") or "")
        usable = bool(
            str(person.get("full_name") or "").strip()
            and (
                any(
                    row.get("company_name") or row.get("title")
                    for row in payload.get("positions") or []
                    if isinstance(row, dict)
                )
                or location.get("city")
                or location.get("country")
            )
        )
        return cls(
            json.dumps(payload, ensure_ascii=False),
            str(social.get("linkedin_url") or "").strip(),
            confidence,
            f"deep research: {notes}" if notes else "deep research found a correct LinkedIn",
            any(marker in f"{notes} {status}".lower() for marker in _UNVERIFIED_MARKERS),
            usable,
        )

    @classmethod
    def load(cls, path: Path) -> ResearchResult | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return cls.from_payload(payload) if isinstance(payload, dict) else None

    def to_payload(self, *, without_linkedin: bool = False) -> dict[str, Any]:
        payload = json.loads(self._payload_json)
        if without_linkedin:
            social = payload.get("social")
            payload["social"] = {
                **(social if isinstance(social, dict) else {}),
                "linkedin_url": "",
            }
        return payload

    def identity_profile(self) -> dict[str, Any]:
        payload = self.to_payload()
        person = payload.get("person") or {}
        location = payload.get("location") or {}
        positions = payload.get("positions") or []
        education_rows = payload.get("education") or []
        headline = payload.get("headline") or {}
        headline = str(headline.get("text") or "") if isinstance(headline, dict) else str(headline)
        experiences = [
            f"{row.get('title') or '?'} @ {row.get('company_name') or '?'}"
            for row in positions
            if isinstance(row, dict) and (row.get("title") or row.get("company_name"))
        ]
        education = [
            " — ".join(filter(None, (
                ", ".join(filter(None, (
                    str(row.get("degree") or ""), str(row.get("field_of_study") or "")
                ))),
                str(row.get("school_name") or ""),
            )))
            for row in education_rows
            if isinstance(row, dict)
            and (row.get("school_name") or row.get("degree") or row.get("field_of_study"))
        ]
        place = str(location.get("raw") or "").strip() or ", ".join(
            str(location.get(key) or "").strip()
            for key in ("city", "state", "country")
            if str(location.get(key) or "").strip()
        )
        return {
            "public_identifier": extract_public_identifier(self.linkedin_url).lower(),
            "linkedin_url": self.linkedin_url,
            "full_name": str(person.get("full_name") or ""),
            "headline": headline,
            "profile_pic_url": "",
            "experiences": experiences,
            "education": education,
            "location": place,
            "reason": self.reason,
            "has_profile": bool(person or positions or education_rows or place),
        }
