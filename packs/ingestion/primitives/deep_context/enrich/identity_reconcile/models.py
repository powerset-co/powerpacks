"""Frozen rows passed through attached-identity selection, judging, and settlement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    IdentityTask,
)
from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp
from packs.ingestion.primitives.deep_context.enrich.profiles.models import ProfileResult


@dataclass(frozen=True)
class IdentityEstimate:
    """Provider work predicted before any attached-identity spend."""

    profile_fetch_misses: int
    parents: int
    tasks: int
    judgeable: int
    reused: int
    human_settled: int
    billed: int
    ground_truth_connections: int
    conflicts: int
    estimated_cost_usd_low: float
    estimated_cost_usd_high: float
    model: str
    reasoning_effort: str
    elapsed_ms: int
    updated_at: IsoTimestamp

    def to_payload(self) -> dict[str, Any]:
        """Serialize at the CLI/manifest boundary."""
        return {
            "source": "reconcile_linkedin",
            "status": "dry_run",
            "profile_fetch_misses": self.profile_fetch_misses,
            "estimated_rapidapi_credits": self.profile_fetch_misses,
            "parents": self.parents,
            "tasks": self.tasks,
            "judgeable": self.judgeable,
            "reused": self.reused,
            "human_settled": self.human_settled,
            "billed": self.billed,
            "ground_truth_connections": self.ground_truth_connections,
            "conflicts": self.conflicts,
            "estimated_cost_usd_low": self.estimated_cost_usd_low,
            "estimated_cost_usd_high": self.estimated_cost_usd_high,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "elapsed_ms": self.elapsed_ms,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class GuidanceOutcome:
    """One durable guidance result before its HTTP/JSON serialization edge."""

    slug: str
    row_key: str
    name: str
    guidance: str
    state: str
    detail: str
    submitted_at: IsoTimestamp | None
    updated_at: IsoTimestamp
    new_url: str = ""
    resolved_pubs: tuple[str, ...] = ()
    candidate_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Serialize with optional fields omitted, not present-but-empty."""
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
    """Typed SQLite row used to build the normalized judge profile.

    Every field defaults empty so a hydrated ProfileResult (fresher, richer)
    can fully override this row rather than merge with it — see
    queue.linkedin_view.
    """

    public_identifier: str = ""
    linkedin_url: str = ""
    display_name: str = ""
    full_name: str = ""
    headline: str = ""
    profile_picture_url: str = ""


@dataclass(frozen=True)
class ProfileFetchResult:
    """Fetch tally for the ordinary (non-heal) reconcile queue.

    Parallels HealFetchState/HealFetchResult/HealProfileCounts below, which
    are heal's own fetch tally over HealCandidate rows — the two pipelines
    each keep their own shape rather than sharing one.
    """

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
    """write_overrides' settlement tally — the local-write step, never billed."""

    path: str
    detached: int
    verified: int
    pending: int
    # Rows settle_machine_identities left untouched because a human already
    # approved/rejected them — the count that makes a human decision durable
    # across reruns of rejudge/terminate.
    preserved_user_rows: int
    total_rows: int


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
    """Selected heal candidates plus enough context to report what was left out."""

    candidates: tuple[HealCandidate, ...]
    skipped_pending_retarget: int
    # Queue size before any `cap` truncation — the gap between this and
    # len(candidates) is what a capped run defers to its next pass.
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
        """A missing hydrate result (exception, no data) settles to "error".

        This is a distinct outcome from a fetch that succeeded and came back
        empty — "error" candidates fall through heal_review's own bucketing
        (neither content nor a fetched-empty) and end up neither rejudged
        nor terminated this run.
        """
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
        """Raises if `candidate_key` has no fetch state — every heal candidate
        must have gone through fetch_states first; there is no default."""
        return next(row for row in self.states if row.candidate_key == candidate_key)


@dataclass(frozen=True)
class HealRejudgeResult:
    candidates: int
    parents: int
    verified: int = 0
    detached: int = 0
    pending: int = 0
    # True when rejudge found no OPENAI_API_KEY and skipped the judge call
    # outright — the paid LinkedIn fetch these candidates already used still
    # stands, only the judgment is missing (see healing.rejudge).
    skipped_no_openai_key: bool = False

@dataclass(frozen=True)
class HealTerminationResult:
    candidates: int
    detached: int = 0
    # Sum of already-approved-and-left-alone plus newly-confirmed-this-run
    # synthetics — see healing.terminate.
    stood_synthetic: int = 0
    # Dead links with no synthetic to fall back on; needs guided re-research.
    pending_reresearch: int = 0
    skipped_human_decided: int = 0

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
    # None (the caller default) means this run had no cap — every eligible
    # row healed. These three fields exist to report that fact, not to
    # enforce a limit that isn't there.
    cap: int | None
    skipped_pending_retarget: int
    profiles: HealProfileCounts
    rejudge: HealRejudgeResult
    terminated: HealTerminationResult
    elapsed_ms: int
