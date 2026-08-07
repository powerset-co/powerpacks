"""Frozen synthesized-fact rows shared by synthesis and dossier rendering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self


@dataclass(frozen=True)
class EmployerFact:
    name: str
    role: str
    status: str

    @classmethod
    def from_payload(cls, payload: object) -> Self | None:
        if not isinstance(payload, dict):
            return None
        return cls(
            str(payload.get("name") or ""),
            str(payload.get("role") or ""),
            str(payload.get("status") or "unknown"),
        )

    def to_payload(self) -> dict[str, str]:
        return {"name": self.name, "role": self.role, "status": self.status}


@dataclass(frozen=True)
class NotableEvent:
    date: str
    summary: str

    @classmethod
    def from_payload(cls, payload: object) -> Self | None:
        if not isinstance(payload, dict):
            return None
        return cls(str(payload.get("date") or ""), str(payload.get("summary") or ""))

    def to_payload(self) -> dict[str, str]:
        return {"date": self.date, "summary": self.summary}


@dataclass(frozen=True)
class SharedContextFact:
    overlap: str
    detail: str
    evidence: str

    @classmethod
    def from_payload(cls, payload: object) -> Self | None:
        if not isinstance(payload, dict):
            return None
        return cls(
            str(payload.get("overlap") or "other"),
            str(payload.get("detail") or ""),
            str(payload.get("evidence") or ""),
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "overlap": self.overlap,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class OwnedIdentifiers:
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        source = payload if isinstance(payload, dict) else {}

        def strings(key: str) -> tuple[str, ...]:
            values = source.get(key)
            return (
                tuple(str(value) for value in values)
                if isinstance(values, list)
                else ()
            )

        return cls(strings("emails"), strings("phones"), strings("urls"))

    def to_payload(self) -> dict[str, list[str]]:
        return {
            "emails": list(self.emails),
            "phones": list(self.phones),
            "urls": list(self.urls),
        }


@dataclass(frozen=True)
class NetworkWorthFact:
    decision: str
    reason: str

    @classmethod
    def from_payload(cls, payload: object) -> Self | None:
        if not isinstance(payload, dict):
            return None
        decision = str(payload.get("decision") or "").lower()
        if not decision:
            return None
        return cls(decision, str(payload.get("reason") or ""))

    def to_payload(self) -> dict[str, str]:
        return {"decision": self.decision, "reason": self.reason}


@dataclass(frozen=True)
class SynthesizedFacts:
    """One parsed fact payload; ``present`` preserves sparse artifact bytes."""

    canonical_name: str = ""
    aliases: tuple[str, ...] = ()
    employers: tuple[EmployerFact, ...] = ()
    title: str = ""
    school: str = ""
    field_of_study: str = ""
    location: str = ""
    relationship_to_owner: str = ""
    relationship_category: str | None = None
    topics: tuple[str, ...] = ()
    notable_events: tuple[NotableEvent, ...] = ()
    identifiers: tuple[str, ...] = ()
    owned_identifiers: OwnedIdentifiers = OwnedIdentifiers()
    shared_context: tuple[SharedContextFact, ...] = ()
    confidence: float = 0.0
    is_owner: bool | None = None
    network_worth: NetworkWorthFact | None = None
    present: frozenset[str] = frozenset()

    @classmethod
    def from_payload(cls, payload: object) -> Self | None:
        if not isinstance(payload, dict) or not payload:
            return None

        def strings(key: str) -> tuple[str, ...]:
            values = payload.get(key)
            return (
                tuple(str(value) for value in values)
                if isinstance(values, list)
                else ()
            )

        return cls(
            canonical_name=str(payload.get("canonical_name") or ""),
            aliases=strings("aliases"),
            employers=tuple(
                row
                for value in payload.get("employers") or []
                if (row := EmployerFact.from_payload(value)) is not None
            ),
            title=str(payload.get("title") or ""),
            school=str(payload.get("school") or ""),
            field_of_study=str(payload.get("field_of_study") or ""),
            location=str(payload.get("location") or ""),
            relationship_to_owner=str(payload.get("relationship_to_owner") or ""),
            relationship_category=(
                str(payload.get("relationship_category") or "")
                if "relationship_category" in payload
                else None
            ),
            topics=strings("topics"),
            notable_events=tuple(
                row
                for value in payload.get("notable_events") or []
                if (row := NotableEvent.from_payload(value)) is not None
            ),
            identifiers=strings("identifiers"),
            owned_identifiers=OwnedIdentifiers.from_payload(
                payload.get("owned_identifiers")
            ),
            shared_context=tuple(
                row
                for value in payload.get("shared_context") or []
                if (row := SharedContextFact.from_payload(value)) is not None
            ),
            confidence=float(payload.get("confidence") or 0.0),
            is_owner=(bool(payload.get("is_owner")) if "is_owner" in payload else None),
            network_worth=NetworkWorthFact.from_payload(payload.get("network_worth")),
            present=frozenset(str(key) for key in payload),
        )

    def to_payload(self) -> dict[str, Any]:
        values: tuple[tuple[str, Any], ...] = (
            ("canonical_name", self.canonical_name),
            ("aliases", list(self.aliases)),
            ("employers", [row.to_payload() for row in self.employers]),
            ("title", self.title),
            ("school", self.school),
            ("field_of_study", self.field_of_study),
            ("location", self.location),
            ("relationship_to_owner", self.relationship_to_owner),
            ("relationship_category", self.relationship_category or ""),
            ("topics", list(self.topics)),
            ("notable_events", [row.to_payload() for row in self.notable_events]),
            ("identifiers", list(self.identifiers)),
            ("owned_identifiers", self.owned_identifiers.to_payload()),
            ("shared_context", [row.to_payload() for row in self.shared_context]),
            ("confidence", self.confidence),
            ("is_owner", bool(self.is_owner)),
            (
                "network_worth",
                self.network_worth.to_payload() if self.network_worth else {},
            ),
        )
        return {key: value for key, value in values if key in self.present}


@dataclass(frozen=True)
class FactRecord:
    facts: SynthesizedFacts

    @classmethod
    def from_payload(cls, payload: object) -> Self | None:
        if not isinstance(payload, dict):
            return None
        facts = SynthesizedFacts.from_payload(payload.get("facts"))
        return cls(facts) if facts is not None else None


@dataclass(frozen=True)
class DossierDepth:
    messages_used: int | None = None
    messages_available: int | None = None
    batches_used: int = 0
    stop_reason: str = ""

    @classmethod
    def from_payload(cls, payload: object) -> Self | None:
        if not isinstance(payload, dict):
            return None
        return cls(
            int(payload["messages_used"]) if "messages_used" in payload else None,
            (
                int(payload["messages_available"])
                if "messages_available" in payload
                else None
            ),
            int(payload.get("batches_used") or 0),
            str(payload.get("stop_reason") or ""),
        )
