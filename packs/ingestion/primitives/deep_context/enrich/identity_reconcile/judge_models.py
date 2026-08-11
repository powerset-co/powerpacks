"""Frozen identity-judge rows shared by attached and research identity stages."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from packs.ingestion.primitives.deep_context.db.models import IdentityOrigin
from packs.ingestion.primitives.deep_context.shared.dossier_evidence import DossierEvidence


# Order and membership feed judge.judgment_fingerprint's payload
# (via as_judge_dict below): adding, removing, or reordering a field changes
# the paid-judge cache key for every future judgment.
_PROFILE_FIELDS = (
    "public_identifier",
    "linkedin_url",
    "full_name",
    "headline",
    "profile_pic_url",
    "experiences",
    "education",
    "location",
    "reason",
    "source",
    "has_profile",
)

# What a research-built JudgeProfile carries (see ResearchResult.identity_profile).
# "source" is deliberately absent: a research profile has never had one, and
# adding it would put `"source": ""` into as_judge_dict and change every
# research judgment fingerprint.
RESEARCH_PRESENT_FIELDS = frozenset(_PROFILE_FIELDS) - {"source"}


@dataclass(frozen=True)
class JudgeProfile:
    """Normalized LinkedIn evidence with an exact judge-packet projection."""

    public_identifier: str = ""
    linkedin_url: str = ""
    full_name: str = ""
    headline: str = ""
    profile_pic_url: str = ""
    experiences: tuple[str, ...] = ()
    education: tuple[str, ...] = ()
    location: str = ""
    reason: str = ""
    source: str = ""
    has_profile: bool = False
    research_confidence: float = 0.0
    research_unverified: bool = False
    # Gates whether _research_confidence/_research_unverified enter
    # as_judge_dict (and so the fingerprint) at all — IdentityTask.packet()
    # only sets this True for IdentityOrigin.RESEARCH.
    research_metadata: bool = False
    _present: frozenset[str] = frozenset()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> JudgeProfile:
        """Parse a profile-shaped payload once at its provider/query boundary."""
        return cls(
            public_identifier=str(payload.get("public_identifier") or ""),
            linkedin_url=str(payload.get("linkedin_url") or ""),
            full_name=str(payload.get("full_name") or ""),
            headline=str(payload.get("headline") or ""),
            profile_pic_url=str(payload.get("profile_pic_url") or ""),
            experiences=tuple(str(value) for value in payload.get("experiences") or ()),
            education=tuple(str(value) for value in payload.get("education") or ()),
            location=str(payload.get("location") or ""),
            reason=str(payload.get("reason") or ""),
            source=str(payload.get("source") or ""),
            has_profile=bool(payload.get("has_profile")),
            _present=frozenset(payload).intersection(_PROFILE_FIELDS),
        )

    def as_judge_dict(self) -> dict[str, Any]:
        """Serialize only fields present at the input edge, preserving packet bytes."""
        values: dict[str, Any] = {
            "public_identifier": self.public_identifier,
            "linkedin_url": self.linkedin_url,
            "full_name": self.full_name,
            "headline": self.headline,
            "profile_pic_url": self.profile_pic_url,
            "experiences": list(self.experiences),
            "education": list(self.education),
            "location": self.location,
            "reason": self.reason,
            "source": self.source,
            "has_profile": self.has_profile,
        }
        payload = {key: values[key] for key in _PROFILE_FIELDS if key in self._present}
        if self.research_metadata:
            payload["_research_confidence"] = self.research_confidence
            payload["_research_unverified"] = self.research_unverified
        return payload


@dataclass(frozen=True)
class IdentityVerdict:
    """One provider verdict parsed for policy while retaining its exact payload."""

    value: str
    confidence: float
    reason: str
    supporting_evidence: tuple[str, ...]
    contradicting_evidence: tuple[str, ...]
    linkedin_plausibly_absent: bool
    recommend_deep_research: bool
    _payload_json: str

    @classmethod
    def from_payload(cls, payload: object) -> IdentityVerdict:
        if not isinstance(payload, dict):
            raise TypeError("identity verdict payload must be an object")
        try:
            confidence = float(payload.get("confidence") or 0)
        except (TypeError, ValueError):
            # A malformed confidence value degrades to 0.0 (never auto-trusted
            # by judgment_policy's thresholds) instead of aborting a
            # strict-schema response the provider already validated.
            confidence = 0.0
        return cls(
            value=str(payload.get("verdict") or ""),
            confidence=confidence,
            reason=str(payload.get("reason") or ""),
            supporting_evidence=tuple(
                str(value) for value in payload.get("supporting_evidence") or ()
            ),
            contradicting_evidence=tuple(
                str(value) for value in payload.get("contradicting_evidence") or ()
            ),
            linkedin_plausibly_absent=bool(payload.get("linkedin_plausibly_absent")),
            recommend_deep_research=bool(payload.get("recommend_deep_research")),
            _payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the original provider shape only at a persistence/API edge."""
        payload = json.loads(self._payload_json)
        if not isinstance(payload, dict):
            raise TypeError("identity verdict payload must be an object")
        return payload


@dataclass(frozen=True)
class IdentityUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> IdentityUsage:
        return cls(
            input_tokens=int(payload.get("input_tokens") or 0),
            output_tokens=int(payload.get("output_tokens") or 0),
            reasoning_tokens=int(payload.get("reasoning_tokens") or 0),
        )

    def __add__(self, other: IdentityUsage) -> IdentityUsage:
        return IdentityUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )

@dataclass(frozen=True)
class IdentityJudgeResult:
    """Typed result parsed immediately after one judge/provider call."""

    verdict: IdentityVerdict | None
    usage: IdentityUsage
    error: str
    fingerprint: str


@dataclass(frozen=True)
class IdentityTask:
    """The sole immutable row passed through identity judging and settlement."""

    evidence: DossierEvidence
    linkedin: JudgeProfile
    parent_slug: str = ""
    parent_id: str = ""
    name: str = ""
    candidate_key: str = ""
    person_ids: tuple[str, ...] = ()
    conflict: bool = False
    from_connections: bool = False
    origin: IdentityOrigin = IdentityOrigin.ATTACHED
    research_confidence: float = 0.0
    research_unverified: bool = False
    verdict: IdentityVerdict | None = None
    error: str = ""
    judgment_fingerprint: str = ""
    action: str = ""
    via: str = ""

    def packet(self) -> tuple[DossierEvidence, JudgeProfile, IdentityOrigin]:
        """Project the byte-pinned judge input at the provider-call edge."""
        profile = self.linkedin
        if self.origin == IdentityOrigin.RESEARCH:
            profile = replace(
                profile,
                research_confidence=self.research_confidence,
                research_unverified=self.research_unverified,
                research_metadata=True,
            )
        return self.evidence, profile, self.origin

    def with_judgment(self, result: IdentityJudgeResult) -> IdentityTask:
        """Attach what the judge answered, including its paid-cache key.

        ``result.fingerprint`` is always set: judge_identity computes it before
        it branches, and every return path (answered, refused, errored) carries
        it. This used to take a `fallback_fingerprint` the caller re-derived
        per task — a third computation of a hash the judge had already handed
        back — for a case no return path can produce.
        """
        return replace(
            self,
            verdict=result.verdict,
            error=result.error,
            judgment_fingerprint=result.fingerprint,
        )

    def as_artifact_dict(self) -> dict[str, Any]:
        """Serialize the historical verdict receipt shape at the file edge."""
        return {
            "parent_slug": self.parent_slug,
            "parent_id": self.parent_id,
            "name": self.name,
            "candidate_key": self.candidate_key,
            "person_ids": list(self.person_ids),
            "conflict": self.conflict,
            "linkedin": self.linkedin.as_judge_dict(),
            "verdict": self.verdict.as_dict() if self.verdict else {},
            "error": self.error,
        }
