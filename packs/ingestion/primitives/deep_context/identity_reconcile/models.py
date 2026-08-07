"""Frozen rows passed through attached-identity selection, judging, and settlement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packs.ingestion.primitives.deep_context.judge_models import (
    IdentityTask,
)


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

    def as_counts(self) -> dict[str, int]:
        return {
            "fetch_wanted": self.fetch_wanted,
            "fetch_ok": self.fetch_ok,
            "fetch_failed": self.fetch_failed,
            "fetch_skipped_no_key": self.fetch_skipped_no_key,
        }


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
    def from_payload(
        cls,
        candidate_key: str,
        payload: dict[str, Any],
    ) -> HealFetchState:
        return cls(
            candidate_key,
            str(payload.get("state") or "error"),
            bool(payload.get("fetched")),
            bool(payload.get("from_cache")),
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

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "candidates": self.candidates,
            "parents": self.parents,
            "verified": self.verified,
            "detached": self.detached,
            "pending": self.pending,
            "restored_pending_retargets": self.restored_pending_retargets,
            "skipped_no_openai_key": self.skipped_no_openai_key,
        }


@dataclass(frozen=True)
class HealTerminationResult:
    candidates: int
    detached: int = 0
    stood_synthetic: int = 0
    minted_synthetic: int = 0
    pending_reresearch: int = 0
    skipped_human_decided: int = 0
    assemble: None = None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "candidates": self.candidates,
            "detached": self.detached,
            "stood_synthetic": self.stood_synthetic,
            "minted_synthetic": self.minted_synthetic,
            "pending_reresearch": self.pending_reresearch,
            "skipped_human_decided": self.skipped_human_decided,
            "assemble": self.assemble,
        }


@dataclass(frozen=True)
class HealProfileCounts:
    content: int
    empty_fetched: int
    empty_unfetched: int
    error: int
    fetched: int
    from_cache: int

    def as_dict(self) -> dict[str, int]:
        return {
            "content": self.content,
            "empty_fetched": self.empty_fetched,
            "empty_unfetched": self.empty_unfetched,
            "error": self.error,
            "fetched": self.fetched,
            "from_cache": self.from_cache,
        }
