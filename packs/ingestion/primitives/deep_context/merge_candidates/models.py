"""Typed merge-candidate stage values."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesizedFacts


@dataclass(frozen=True)
class MergePerson:
    """One canonical parent; ``person_id`` is its schema-v8 cache anchor child."""

    slug: str
    person_id: str
    name: str
    name_key: str
    parent_id: str = ""
    member_person_ids: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    extra_emails: tuple[str, ...] = ()
    phone_digits: tuple[str, ...] = ()
    extra_phones: tuple[str, ...] = ()
    evidence: DossierEvidence = field(default_factory=DossierEvidence)
    all_emails: frozenset[str] = field(init=False)
    all_phones: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "all_emails", frozenset((*self.emails, *self.extra_emails)))
        object.__setattr__(self, "all_phones", frozenset((*self.phone_digits, *self.extra_phones)))


@dataclass(frozen=True)
class MergeDecision:
    """One parsed deterministic or LLM same-person decision."""

    same_person: bool
    confidence: float
    tone_consistent: bool
    reason: str
    judge: str

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        judge: str,
    ) -> MergeDecision:
        """Parse the raw judge response once at the provider/cache boundary."""
        return cls(
            same_person=bool(payload.get("same_person")),
            confidence=float(payload.get("confidence") or 0),
            tone_consistent=bool(payload.get("tone_consistent")),
            reason=str(payload.get("reason") or ""),
            judge=judge,
        )


@dataclass(frozen=True)
class MergePairVerdict:
    left: int
    right: int
    signature: str
    decision: MergeDecision


@dataclass(frozen=True)
class CachedMergeVerdict:
    signature: str
    decision: MergeDecision


@dataclass(frozen=True)
class MergeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def __add__(self, other: MergeUsage) -> MergeUsage:
        return type(self)(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MergeUsage:
        return cls(
            int(payload.get("input_tokens") or 0),
            int(payload.get("output_tokens") or 0),
            int(payload.get("reasoning_tokens") or 0),
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass(frozen=True)
class MergeJudgeResult:
    decision: MergeDecision
    usage: MergeUsage
    error: str = ""


@dataclass(frozen=True)
class ConfirmedMergeRow:
    slug_a: str
    name_a: str
    slug_b: str
    name_b: str
    confidence: float
    tone_consistent: bool
    reason: str

    def csv_dict(self) -> dict[str, Any]:
        return {
            "slug_a": self.slug_a,
            "name_a": self.name_a,
            "slug_b": self.slug_b,
            "name_b": self.name_b,
            "confidence": self.confidence,
            "tone_consistent": self.tone_consistent,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PairSurvey:
    people: list[MergePerson]
    pairs: list[tuple[int, int]]
    slam: list[tuple[int, int, MergeDecision]]
    shared_unsettled: list[tuple[int, int]]
    reused: list[MergePairVerdict]
    to_judge: list[tuple[int, int, str]]


@dataclass(frozen=True)
class ChildEntry:
    slug: str
    name: str
    score: float
    reason: str
    channels: tuple[str, ...]
    person_id: str


@dataclass(frozen=True)
class ParentPlan:
    parent_id: str
    slug: str
    name: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    confirmed: tuple[ChildEntry, ...]
    merged: SynthesizedFacts
