"""Typed synthesis planning and execution state."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.db.models import OwnerProfile
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.dossier.models import SynthesizedFacts


TOKEN_KEYS = ("input_tokens", "output_tokens", "reasoning_tokens")


@dataclass(frozen=True)
class SynthesisPlan:
    owner: OwnerProfile | None
    system_prompt: str
    bundles: tuple[CollectionBundle, ...]


class SynthesisStage(Protocol):
    db: Db
    facts_dir: Path
    model: str
    reasoning_effort: str
    chunk_chars: int
    target_confidence: float
    saturation_rounds: int
    max_batches: int
    concurrency: int | None
    chunk_people: int
    timeout: int
    max_retries: int
    rejudge: bool

    def _plan(self) -> SynthesisPlan: ...


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
        facts = SynthesizedFacts.from_payload(
            facts_payload if isinstance(facts_payload, dict) else payload
        )
        return cls(
            synthesis_version=str(payload.get("synthesis_version") or ""),
            input_evidence_fingerprint=str(
                payload.get("input_evidence_fingerprint") or ""
            ),
            facts=facts,
            usage=SynthesisUsage.from_payload(
                payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            ),
            batches_used=int(payload.get("batches_used") or 0),
            batches_total=int(payload.get("batches_total") or 0),
            messages_used=int(payload.get("messages_used") or 0),
            messages_available=int(payload.get("messages_available") or 0),
            final_confidence=float(payload.get("final_confidence") or 0),
            stop_reason=str(payload.get("stop_reason") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize the historical facts record in its pinned key order."""
        return {
            "chunk_index": 0,
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
    without_worth: int = 0

    def record(self, result: SynthesisResult) -> None:
        record = result.record
        for key, value in record.usage.as_dict().items():
            self.tokens[key] += value
        self.people_done += 1
        self.errors += result.errors
        self.batches += record.batches_used
        reason = record.stop_reason
        self.stop_reasons[reason] = self.stop_reasons.get(reason, 0) + 1
