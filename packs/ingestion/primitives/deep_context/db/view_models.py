"""Frozen rows materialized by the named Deep Context database views."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    FactRow,
    IsoTimestamp,
    ParentSnapshotRow,
    PersonRow,
)

# These row/query pairs are pinned contracts: update both sides together.
# WorthRow <-> _view_sql.WORTH_SELECT
# CandidateViewRow <-> _view_sql.CANDIDATE_SELECT
# ParentViewRow <-> _view_sql.PARENT_SELECT
# AttachedIdentityQueueRow <-> identity_views.attached_identity_queue SELECT
# HealIdentityQueueRow <-> identity_views.heal_identity_queue SELECT
# EnrichmentQueueRow <-> identity_views.enrichment_queue SELECT
# SyntheticFallbackRow <-> identity_views.synthetic_fallback SELECT


@dataclass(frozen=True)
class DossierEvidenceRows:
    """Narrow parent-family inputs for one identity evidence packet."""

    parents: tuple[ParentSnapshotRow, ...]
    people: tuple[PersonRow, ...]
    facts: tuple[FactRow, ...]
    source_bundles: tuple[ArtifactRow, ...]


@dataclass(frozen=True)
class CollectionSourceRow:
    """One message-bearing parent and its observed store lookup keys."""

    parent_id: str
    display_name: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    source_channels: tuple[str, ...]


@dataclass(frozen=True)
class WorthHumanRow:
    decision: str
    updated_at: IsoTimestamp
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
    experiences: tuple[str, ...] = ()
    education: tuple[str, ...] = ()
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
    experiences: tuple[str, ...]
    education: tuple[str, ...]
    location: str
    has_profile: bool
    verdict: str
    confidence: float | None
    reason: str
    match_emails: tuple[str, ...]
    match_phones: tuple[str, ...]
    import_candidate: bool
    candidate_origin: bool
    synthetic: bool
    action: str
    approved: str
    new_url: str
    new_public_identifier: str
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
class PersonLookupRow:
    slug: str
    name: str
    path: str
    dossier_path: str
    dossier_body: str
    headline: str
    full_name: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    parent_id: str
    person_id: str


@dataclass(frozen=True)
class ParentLookupRow:
    slug: str
    name: str
    path: str
    dossier_path: str
    dossier_body: str
    headline: str
    full_name: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    parent_id: str
    children: tuple[str, ...]


@dataclass(frozen=True)
class AvatarPayload:
    base64: str
    content_type: str


@dataclass(frozen=True)
class AttachedIdentityQueueRow:
    """Pinned to ``identity_views.attached_identity_queue`` SELECT aliases."""

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
    """Pinned to ``identity_views.heal_identity_queue`` SELECT aliases."""

    parent_id: str
    parent_slug: str
    name: str
    candidate_key: str
    public_identifier: str
    linkedin_url: str
    selection: Literal["candidate", "pending_retarget"]


@dataclass(frozen=True)
class ApprovedIdentityRow:
    """Resolved from review rows plus one scoped parent-family join."""

    row_key: str
    name: str
    action: str
    linkedin_url: str
    person_id: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]


@dataclass(frozen=True)
class EnrichmentQueueRow:
    """Pinned to ``identity_views.enrichment_queue`` SELECT aliases."""

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
class SyntheticFallbackRow:
    """Pinned to ``identity_views.synthetic_fallback`` SELECT aliases."""

    parent_id: str
    artifact_key: str | None
    result_json: str
    display_name: str
    research_link_rejected: bool
    person_ids: tuple[str, ...]
    existing_approved: str
