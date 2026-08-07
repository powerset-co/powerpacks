"""Frozen rows passed through attached-identity selection, judging, and settlement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packs.ingestion.primitives.deep_context.judge_models import (
    IdentityTask,
)
from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp
from packs.ingestion.primitives.deep_context.research_result import ResearchResult
from packs.ingestion.primitives.deep_context.profile_models import ProfileResult


@dataclass(frozen=True)
class GuidedProviderResult:
    """One parsed research-provider result passed into identity settlement."""

    new_url: str
    detail: str
    research_result: ResearchResult


@dataclass(frozen=True)
class GuidanceOutcome:
    """One durable guidance result before its HTTP/JSON serialization edge."""

    slug: str
    row_key: str
    name: str
    guidance: str
    state: str
    detail: str
    submitted_at: IsoTimestamp
    updated_at: IsoTimestamp
    new_url: str = ""
    resolved_pubs: tuple[str, ...] = ()
    candidate_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "slug": self.slug,
            "row_key": self.row_key,
            "name": self.name,
            "guidance": self.guidance,
            "state": self.state,
            "detail": self.detail,
            "submitted_at": self.submitted_at,
            "updated_at": self.updated_at,
        }
        if self.new_url:
            values["new_url"] = self.new_url
        if self.resolved_pubs:
            values["resolved_pubs"] = list(self.resolved_pubs)
        if self.candidate_url:
            values["candidate_url"] = self.candidate_url
        return values


@dataclass(frozen=True)
class IdentityProfileSource:
    """Typed SQLite row used to build the normalized judge profile."""

    public_identifier: str = ""
    linkedin_url: str = ""
    display_name: str = ""
    full_name: str = ""
    headline: str = ""
    profile_picture_url: str = ""
    work_experiences: object = None
    education: object = None
    city: str = ""
    state: str = ""
    country: str = ""


@dataclass(frozen=True)
class ProfileFetchResult:
    tasks: tuple[IdentityTask, ...]
    fetch_wanted: int = 0
    fetch_ok: int = 0
    fetch_failed: int = 0
    fetch_skipped_no_key: int = 0

    def as_counts(self) -> ProfileFetchCounts:
        return ProfileFetchCounts(
            self.fetch_wanted,
            self.fetch_ok,
            self.fetch_failed,
            self.fetch_skipped_no_key,
        )


@dataclass(frozen=True)
class ProfileFetchCounts:
    fetch_wanted: int
    fetch_ok: int
    fetch_failed: int
    fetch_skipped_no_key: int


@dataclass(frozen=True)
class IdentityProjectionResult:
    path: str
    detached: int
    verified: int
    pending: int
    preserved_user_rows: int
    total_rows: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "path": self.path,
            "detached": self.detached,
            "verified": self.verified,
            "pending": self.pending,
            "preserved_user_rows": self.preserved_user_rows,
            "total_rows": self.total_rows,
        }


@dataclass(frozen=True)
class ResearchReject:
    llm_reject: str
    llm_reject_confidence: str
    llm_reject_reason: str
    confidence: str


@dataclass(frozen=True)
class HealCandidate:
    parent_id: str
    parent_slug: str
    name: str
    candidate_key: str
    public_identifier: str
    linkedin_url: str


@dataclass(frozen=True)
class HealSelection:
    candidates: tuple[HealCandidate, ...]
    skipped_pending_retarget: int
    uncapped: int


@dataclass(frozen=True)
class HealFetchState:
    candidate_key: str
    state: str
    fetched: bool
    from_cache: bool

    @classmethod
    def from_result(
        cls,
        candidate_key: str,
        result: ProfileResult | None,
    ) -> HealFetchState:
        return cls(
            candidate_key,
            result.state or "error" if result else "error",
            bool(result.fetched) if result else False,
            bool(result.from_cache) if result else False,
        )


@dataclass(frozen=True)
class HealFetchResult:
    states: tuple[HealFetchState, ...]

    def state_for(self, candidate_key: str) -> HealFetchState:
        return next(row for row in self.states if row.candidate_key == candidate_key)


@dataclass(frozen=True)
class HealRejudgeResult:
    candidates: int
    parents: int
    verified: int = 0
    detached: int = 0
    pending: int = 0
    restored_pending_retargets: int = 0
    skipped_no_openai_key: bool = False

@dataclass(frozen=True)
class HealTerminationResult:
    candidates: int
    detached: int = 0
    stood_synthetic: int = 0
    minted_synthetic: int = 0
    pending_reresearch: int = 0
    skipped_human_decided: int = 0
    assemble: None = None

@dataclass(frozen=True)
class HealProfileCounts:
    content: int
    empty_fetched: int
    empty_unfetched: int
    error: int
    fetched: int
    from_cache: int



@dataclass(frozen=True)
class HealReviewSummary:
    primitive: str
    status: str
    queue_pending_before: int
    queue_pending_after: int
    candidates: int
    candidates_uncapped: int
    capped: bool
    cap: int | None
    skipped_pending_retarget: int
    profiles: HealProfileCounts
    rejudge: HealRejudgeResult
    terminated: HealTerminationResult
    elapsed_ms: int
