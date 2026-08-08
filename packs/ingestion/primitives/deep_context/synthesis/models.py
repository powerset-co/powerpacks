"""Typed synthesis planning and execution state."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.shared.openai_responses import (
    OpenAIResponsesConfig,
)


@dataclass(frozen=True)
class EmployerFact:
    name: str
    role: str
    status: str  # "current" | "past" | "unknown" by convention, not enforced here; facts.py's status_rank ranks anything else as unknown.

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
        # Any non-empty string is accepted here; the yes/maybe/no vocabulary
        # (facts.NETWORK_WORTH_VALUES) is only checked by readers (merge_fact_records,
        # db/projectors.project_parent_fact), which treat an unrecognized value as
        # absent rather than raising. A stray value still round-trips through
        # to_payload() into the stored record.
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
    # None = key absent from the payload (pre-v6 record, or a merged/normalized
    # parent — facts._MERGED_FIELDS deliberately never sets this); "" = present but
    # empty. Every other string field on this class collapses that distinction to "".
    relationship_category: str | None = None
    topics: tuple[str, ...] = ()
    notable_events: tuple[NotableEvent, ...] = ()
    identifiers: tuple[str, ...] = ()
    owned_identifiers: OwnedIdentifiers = OwnedIdentifiers()
    shared_context: tuple[SharedContextFact, ...] = ()
    confidence: float = 0.0
    # Same absent-vs-false split as relationship_category. Merged parent facts
    # never set it, so bool(facts.is_owner) reads False for every parent — fine
    # today because build_parents.py already excludes owner rows before merging.
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
    """Thin ``{"facts": {...}}`` envelope adapter feeding merge_fact_records.

    Only the "facts" key is read, so a full SynthesisRecord-shaped dict works
    unchanged; callers with a bare facts payload wrap it as ``{"facts": payload}``
    to match (see normalization.py, build_parents.py).
    """

    facts: SynthesizedFacts

    @classmethod
    def from_payload(cls, payload: object) -> Self | None:
        if not isinstance(payload, dict):
            return None
        facts = SynthesizedFacts.from_payload(payload.get("facts"))
        return cls(facts) if facts is not None else None


@dataclass(frozen=True)
class DossierDepth:
    """Rendering-only view of a facts artifact's own stored progress fields.

    Parses the same JSON keys as SynthesisRecord, but keeps messages_used/
    messages_available as None when absent rather than SynthesisRecord.from_payload's
    0-coerced tally ints; rendering.py treats None as "fall back to the bundle's
    own message count", not a real zero.
    """

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


TOKEN_KEYS = ("input_tokens", "output_tokens", "reasoning_tokens")


@dataclass(frozen=True)
class SynthesisPlan:
    system_prompt: str
    bundles: tuple[CollectionBundle, ...]


@dataclass(frozen=True)
class SynthesisConfig:
    raw_dir: Path
    facts_dir: Path
    responses: OpenAIResponsesConfig
    chunk_chars: int
    target_confidence: float
    saturation_rounds: int
    max_batches: int
    force: bool
    rejudge: bool


@dataclass(frozen=True)
class SynthesisUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SynthesisUsage:
        return cls(*(int(payload.get(key) or 0) for key in TOKEN_KEYS))

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass(frozen=True)
class SynthesisCallResult:
    facts: SynthesizedFacts | None
    usage: SynthesisUsage
    failed: bool


@dataclass(frozen=True)
class SynthesisRecord:
    synthesis_version: str
    input_evidence_fingerprint: str
    facts: SynthesizedFacts | None
    usage: SynthesisUsage
    batches_used: int
    batches_total: int
    messages_used: int
    messages_available: int
    final_confidence: float
    stop_reason: str

    @classmethod
    def from_payload(cls, payload: object) -> SynthesisRecord | None:
        if not isinstance(payload, dict) or not payload:
            return None
        facts_payload = payload.get("facts")
        # When "facts" isn't a dict (missing/legacy shape), parse the whole payload as
        # facts instead — so a bare facts-shaped dict works here too, not only the
        # full {"facts": ..., "synthesis_version": ...} record shape.
        facts = SynthesizedFacts.from_payload(facts_payload if isinstance(facts_payload, dict) else payload)
        return cls(
            synthesis_version=str(payload.get("synthesis_version") or ""),
            input_evidence_fingerprint=str(payload.get("input_evidence_fingerprint") or ""),
            facts=facts,
            usage=SynthesisUsage.from_payload(payload.get("usage") if isinstance(payload.get("usage"), dict) else {}),
            batches_used=int(payload.get("batches_used") or 0),
            batches_total=int(payload.get("batches_total") or 0),
            messages_used=int(payload.get("messages_used") or 0),
            messages_available=int(payload.get("messages_available") or 0),
            final_confidence=float(payload.get("final_confidence") or 0),
            stop_reason=str(payload.get("stop_reason") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize the historical facts record in its pinned key order.

        PINNED: runner.py json.dumps's this dict without sort_keys, and
        db/projectors.py sha256's those exact bytes into the FACTS artifact's
        content_fingerprint. Reordering these keys changes every existing
        record's fingerprint with no underlying data change.
        """
        return {
            "chunk_index": 0,  # Vestigial: one record now covers a whole person, not a chunk; kept only for key-order/shape compatibility with old per-chunk records.
            "synthesis_version": self.synthesis_version,
            "input_evidence_fingerprint": self.input_evidence_fingerprint,
            "facts": self.facts.to_payload() if self.facts else {},
            "usage": self.usage.as_dict(),
            "batches_used": self.batches_used,
            "batches_total": self.batches_total,
            "messages_used": self.messages_used,
            "messages_available": self.messages_available,
            "final_confidence": self.final_confidence,
            "stop_reason": self.stop_reason,
        }


@dataclass(frozen=True)
class SynthesisResult:
    person_id: str
    record: SynthesisRecord
    errors: int


@dataclass(frozen=True)
class WorthSyncResult:
    # synced_people and total_rows are both the full parent fact-row count at the
    # sole call site (synthesize_person_context.py) — despite the name, neither is
    # scoped to this run. synced_rows (tally.projected_rows) is the only field that
    # actually reflects rows written this run.
    path: str
    synced_people: int
    synced_rows: int
    without_worth: int
    total_rows: int


@dataclass
class SynthesisTally:
    people_done: int = 0
    errors: int = 0
    batches: int = 0
    stop_reasons: dict[str, int] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=lambda: dict.fromkeys(TOKEN_KEYS, 0))
    projected_rows: int = 0

    def record(self, result: SynthesisResult) -> None:
        record = result.record
        for key, value in record.usage.as_dict().items():
            self.tokens[key] += value
        self.people_done += 1
        self.errors += result.errors
        self.batches += record.batches_used
        reason = record.stop_reason
        self.stop_reasons[reason] = self.stop_reasons.get(reason, 0) + 1
