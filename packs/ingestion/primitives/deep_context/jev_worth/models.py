"""Typed worth evidence, provider answers, and classification results."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum

from packs.ingestion.primitives.deep_context.synthesis.models import JevUsage, NetworkWorthFact, SynthesizedFacts


@dataclass(frozen=True)
class WorthFacts:
    facts: SynthesizedFacts
    # Exact input ordering and old fields bind the paid request cache.
    serialized: str

    @classmethod
    def from_payload(cls, payload: dict) -> WorthFacts:
        return cls(SynthesizedFacts.from_payload(payload) or SynthesizedFacts(), json.dumps(payload, ensure_ascii=False))


class AnswerKind(StrEnum):
    NOUL = 'noul'
    CHOICE = 'choice'
    SCORE = 'score'


@dataclass(frozen=True)
class WorthAnswer:
    kind: AnswerKind
    noul: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)

    @classmethod
    def parse_all(cls, payload: dict) -> dict[str, WorthAnswer]:
        return {name: cls(AnswerKind(row['type']), float(row.get('noul', 0)),
                          {key: float(value) for key, value in row.get('probabilities', {}).items()})
                for name, row in payload.items()}


@dataclass(frozen=True)
class WorthResult:
    worth: NetworkWorthFact
    labels: dict[str, str | float]
    usage: JevUsage
    input_tokens: int
    output_tokens: int

    def usage_payload(self) -> dict[str, int | bool]:
        return {'input_tokens': self.input_tokens, 'output_tokens': self.output_tokens, 'cached': bool(self.usage.cached)}


@dataclass(frozen=True)
class WorthEstimate:
    input_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False
