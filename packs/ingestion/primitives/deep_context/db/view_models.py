"""Frozen rows materialized by the named Deep Context database views."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Literal

# These row/query pairs are pinned contracts: update both sides together.
# WorthRow <-> _view_sql.WORTH_SELECT
# CandidateViewRow <-> _view_sql.CANDIDATE_SELECT
# ParentViewRow <-> _view_sql.PARENT_SELECT
# AttachedIdentityQueueRow <-> identity_views._attached_identity_queue SELECT
# HealIdentityQueueRow <-> identity_views._heal_identity_queue SELECT
# EnrichmentQueueRow <-> identity_views._enrichment_queue SELECT
# SyntheticFallbackRow <-> identity_views._synthetic_fallback SELECT
# LatestJobRow <-> identity_views.linkedin_review latest_job SELECT


@dataclass(frozen=True)
class WorthHumanRow:
    decision: str
    updated_at: str
    note: str


@dataclass(frozen=True)
class WorthMachineRow:
    decision: str
    reason: str
    source: str


@dataclass(frozen=True)
class WorthSummary:
    decision: str
    source: str


@dataclass(frozen=True)
class WorthRow:
    """Pinned to ``_view_sql.WORTH_SELECT`` plus ``_worth_row``."""

    key: str
    parent_id: str
    parent_slug: str
    person_ids: tuple[str, ...]
    name: str
    machine: WorthMachineRow
    human: WorthHumanRow | None
    effective: str
    source: str


@dataclass(frozen=True)
class WorthCounts:
    total: int
    pending: int
    yes: int
    no: int


@dataclass(frozen=True)
class LinkedInProgress:
    total: int
    pending: int
    done: int


@dataclass(frozen=True)
class CandidateProfile:
    """One candidate profile after its kind-specific boundary parser."""

    full_name: str = ""
    headline: str = ""
    profile_pic_url: str = ""
    experiences: tuple[Any, ...] = ()
    education: tuple[Any, ...] = ()
    location: str = ""
    linkedin_url: str = ""
    has_profile: bool = False


@dataclass(frozen=True)
class CandidateViewRow:
    """Pinned to ``_view_sql.CANDIDATE_SELECT`` plus ``_candidate_row``."""

    pub: str
    row_key: str
    profile_pub: str
    url: str
    full_name: str
    headline: str
    profile_pic_url: str
    experiences: tuple[Any, ...]
    education: tuple[Any, ...]
    location: str
    has_profile: bool
    verdict: str
    confidence: float
    supporting: tuple[Any, ...]
    contradicting: tuple[Any, ...]
    reason: str
    plausibly_absent: bool
    recommend_dr: bool
    match_emails: tuple[str, ...]
    match_phones: tuple[str, ...]
    conflict: bool
    import_candidate: bool
    candidate_origin: bool
    synthetic: bool
    action: str
    approved: str
    new_url: str
    new_public_identifier: str
    llm_reject: str
    llm_reject_confidence: float | None
    llm_reject_reason: str
    pending: bool


@dataclass(frozen=True)
class ParentViewRow:
    """Pinned to ``_view_sql.PARENT_SELECT`` plus ``_parent_row``."""

    parent_id: str
    slug: str
    dossier_path: str
    dossier_body: str
    name: str
    person_ids: tuple[str, ...]
    sources: tuple[str, ...]
    source_channels: tuple[str, ...]
    worth_row: WorthRow
    worth: WorthSummary
    machine_worth: WorthMachineRow
    candidates: tuple[CandidateViewRow, ...] = ()


@dataclass(frozen=True)
class AttachedIdentityQueueRow:
    """Pinned to ``identity_views._attached_identity_queue`` SELECT aliases."""

    parent_id: str
    parent_slug: str
    name: str
    candidate_key: str
    public_identifier: str
    linkedin_url: str
    person_ids: tuple[str, ...]
    conflict: bool
    from_connections: bool


@dataclass(frozen=True)
class HealIdentityQueueRow:
    """Pinned to ``identity_views._heal_identity_queue`` SELECT aliases."""

    parent_id: str
    parent_slug: str
    name: str
    candidate_key: str
    public_identifier: str
    linkedin_url: str
    selection: Literal["candidate", "pending_retarget"]


@dataclass(frozen=True)
class ApprovedIdentityRow:
    """Derived from canonical and identity snapshots; it has no direct SELECT."""

    row_key: str
    name: str
    action: str
    linkedin_url: str
    person_id: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]


@dataclass(frozen=True)
class EnrichmentQueueRow:
    """Pinned to ``identity_views._enrichment_queue`` SELECT aliases."""

    parent_id: str
    parent_slug: str
    name: str
    person_ids: tuple[str, ...]
    row_key: str
    candidate_exists: bool
    linkedin_url: str
    verdict: str
    verdict_reason: str
    match_emails: tuple[str, ...]
    match_phones: tuple[str, ...]
    candidate_origin: bool


@dataclass(frozen=True)
class SyntheticCandidateState:
    public_identifier: str
    profile_json: str
    action: str
    approved: str


@dataclass(frozen=True)
class SyntheticFallbackRow:
    """Pinned to ``identity_views._synthetic_fallback`` SELECT aliases."""

    handle: str
    parent_id: str
    candidate_key: str
    result_json: str
    display_name: str
    display_slug: str
    effective_worth: str
    machine_reject: str
    person_ids: tuple[str, ...]
    primary_email: str
    phone_e164: str
    existing_synthetics: tuple[SyntheticCandidateState, ...]


@dataclass(frozen=True)
class LatestJobRow:
    """Pinned to the ``linkedin_review(..., 'latest_job')`` jobs SELECT."""

    name: str
    kind: str
    status: str
    parent_id: str | None
    candidate_key: str | None
    selection_fingerprint: str | None
    completed_count: int
    total_count: int
    error: str | None
    result_json: str | None
    started_at: str | None
    finished_at: str | None

    @classmethod
    def from_row(cls, row: Any) -> LatestJobRow:
        """Materialize every selected jobs column from this frozen row's fields."""
        values = {field.name: row[field.name] for field in fields(cls)}
        values["completed_count"] = int(values["completed_count"] or 0)
        values["total_count"] = int(values["total_count"] or 0)
        return cls(**values)
