"""Typed Deep Context SQLite domain rows and value vocabularies.

The schema owns only DDL construction. Runtime callers import these definitions
from their concrete home so the domain model can be read without the SQL text.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReviewAction(StrEnum):
    VERIFY = "verify"
    DETACH = "detach"
    RETARGET = "retarget"
    EXCLUDE = "exclude"
    REVIEW = "review"


class ApprovedState(StrEnum):
    AUTO = "auto"
    YES = "yes"
    NO = "no"


class MachineWorth(StrEnum):
    YES = "yes"
    MAYBE = "maybe"
    NO = "no"


class HumanWorth(StrEnum):
    YES = "yes"
    NO = "no"


class ReviewSource(StrEnum):
    REVIEW = "deep-context-review"
    USER_GUIDANCE = "user-guidance"
    RECONCILE = "deep-context-reconcile"
    DEEP_RESEARCH = "deep-research"
    SYNTHESIS = "deep-context-synthesis"
    PARENT_WORTH = "deep-context-parent-worth"
    HEAL = "deep-context-heal"
    NAME_MATCH = "deep-context-name-match"
    SELF_REPORTED = "dossier-self-reported"
    SIBLING_SETTLE = "legacy-sibling-settle"
    LEGACY_MIGRATION = "legacy-migration"


class RowKind(StrEnum):
    PUB = "pub"
    PERSON_UUID = "person_uuid"
    CANDIDATE_EMAIL = "candidate_email"
    CANDIDATE_PHONE = "candidate_phone"
    MESSAGE_LINKEDIN = "message_linkedin"
    SYNTHETIC = "synthetic"
    GHOST = "ghost"
    RESEARCH = "research"
    PARENT = "parent"


class IdentifierKind(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    LINKEDIN = "linkedin"


class ArtifactKind(StrEnum):
    FACTS = "facts"
    DOSSIER = "dossier"
    PROFILE = "profile"
    AVATAR = "avatar"
    RESEARCH = "research"
    SYNTHETIC = "synthetic"
    SOURCE_BUNDLE = "source_bundle"
    RAW_RESULT = "raw_result"


class ProjectionStatus(StrEnum):
    PROJECTED = "projected"
    FAILED = "failed"


class ResearchStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    NO_MATCH = "no_match"
    FAILED = "failed"


class GuidanceState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    APPLIED = "applied"
    FAILED = "failed"


class JobKind(StrEnum):
    GUIDED_RETARGET = "guided_retarget"
    ENRICHMENT = "enrichment"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    APPLIED = "applied"
    SYNTHETIC = "synthetic"
    NO_MATCH = "no_match"
    FAILED = "failed"


HUMAN_DECISION_SOURCES = frozenset({ReviewSource.REVIEW.value, ReviewSource.USER_GUIDANCE.value})
PARENT_WORTH_PREFIX = "parent-worth:"
LLM_REJECT_VALUES = ("yes", "no", "spam")
JUDGE_CONFIRM_THRESHOLD = 0.70
JUDGE_DETACH_THRESHOLD = 0.85
DECISIVE_CONFIRM_THRESHOLD = 0.95
RESEARCH_CONFIRM_THRESHOLD = 0.80


@dataclass(frozen=True)
class OwnerContextRow:
    context_key: str
    payload_json: str
    path: str
    content_fingerprint: str
    projected_at: str | None = None


@dataclass(frozen=True)
class ParentRow:
    parent_id: str
    public_identifier: str
    display_name: str | None = None
    display_slug: str | None = None
    machine_worth: str | None = None
    machine_worth_reason: str | None = None
    source: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class PersonRow:
    person_id: str
    parent_id: str
    child_slug: str | None = None
    parent_slug: str | None = None
    display_name: str | None = None
    is_owner: int = 0
    is_ghost: int = 0
    facts_json: str | None = None
    confidence: float | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class PersonIdentifierRow:
    person_id: str
    kind: str
    normalized_value: str
    display_value: str | None = None


@dataclass(frozen=True)
class PersonSourceRow:
    person_id: str
    source: str


@dataclass(frozen=True, kw_only=True)
class _IdentityMachineFields:
    """Machine-owned link columns shared by writes and hydrated snapshots."""

    machine_action: str | None = None
    machine_approved: str | None = None
    machine_confidence: float | None = None
    machine_reason: str | None = None
    machine_judgment: str | None = None
    machine_reject: str | None = None
    machine_reject_confidence: float | None = None
    machine_reject_reason: str | None = None
    machine_proposed_url: str | None = None
    machine_proposed_public_identifier: str | None = None
    authoritative_detach: int = 0
    paid_profile: int = 0
    judgment_fingerprint: str | None = None
    judgment_artifact_path: str | None = None
    judgment_payload_json: str | None = None
    source: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class LinkRow(_IdentityMachineFields):
    row_key: str
    parent_id: str
    public_identifier: str
    kind: str
    linkedin_url: str | None = None
    display_name: str | None = None
    candidate_origin: int = 0
    raw_import: int = 0


@dataclass(frozen=True)
class IdentityMachineProjection(_IdentityMachineFields):
    row_key: str


@dataclass(frozen=True)
class CandidatePersonRow:
    row_key: str
    person_id: str
    parent_id: str


@dataclass(frozen=True)
class PersonIdentifiersProjection:
    person_id: str
    rows: tuple[PersonIdentifierRow, ...]


@dataclass(frozen=True)
class PersonSourcesProjection:
    person_id: str
    rows: tuple[PersonSourceRow, ...]


@dataclass(frozen=True)
class CandidatePeopleProjection:
    row_key: str
    rows: tuple[CandidatePersonRow, ...]


@dataclass(frozen=True)
class CanonicalGraphProjection:
    parents: tuple[ParentRow, ...]
    people: tuple[PersonRow, ...]
    identifiers: tuple[PersonIdentifierRow, ...]
    sources: tuple[PersonSourceRow, ...]


@dataclass(frozen=True)
class CanonicalGraphCounts:
    parents: int
    people: int
    identifiers: int
    sources: int
    parents_removed: int


@dataclass(frozen=True)
class ArtifactRow:
    artifact_key: str
    kind: str
    parent_id: str
    path: str
    content_fingerprint: str
    status: str
    person_id: str | None = None
    candidate_key: str | None = None
    input_fingerprint: str | None = None
    error: str | None = None
    payload_json: str | None = None
    projected_at: str | None = None


@dataclass(frozen=True)
class ArtifactReplacement:
    kind: str
    rows: tuple[ArtifactRow, ...]
    person_id: str | None = None


@dataclass(frozen=True)
class FactRow:
    subject_key: str
    parent_id: str
    artifact_key: str
    person_id: str | None = None
    machine_worth: str | None = None
    machine_worth_reason: str | None = None
    confidence: float | None = None
    is_owner: int = 0
    facts_json: str | None = None
    projected_at: str | None = None


@dataclass(frozen=True)
class SyntheticProfileRow:
    public_identifier: str
    candidate_key: str
    profile_json: str
    source_artifact_key: str | None = None
    linkedin_url: str | None = None
    name: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class ResearchRow:
    handle: str
    parent_id: str
    status: str
    candidate_key: str | None = None
    artifact_key: str | None = None
    selection_fingerprint: str | None = None
    result_json: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class GuidanceRow:
    handle: str
    parent_id: str
    guidance: str
    state: str = GuidanceState.PENDING.value
    candidate_key: str | None = None
    submitted_at: str | None = None
    applied_url: str | None = None
    detail_json: str | None = None


@dataclass(frozen=True)
class JobRow:
    name: str
    kind: str
    status: str
    parent_id: str | None = None
    candidate_key: str | None = None
    selection_fingerprint: str | None = None
    completed_count: int = 0
    total_count: int = 0
    error: str | None = None
    result_json: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class MergeVerdictRow:
    person_a: str
    person_b: str
    slug_a: str
    slug_b: str
    signature: str
    judge: str
    same_person: int
    confidence: float
    tone_consistent: int
    reason: str = ""
    accepted: int = 0
    updated_at: str | None = None


@dataclass(frozen=True)
class ResetReviewCounts:
    human_worth_cleared: int
    human_identity_cleared: int


@dataclass(frozen=True)
class ParentSnapshotRow(ParentRow):
    """Persisted parent row with its human review columns."""

    human_worth: str | None = None
    human_worth_note: str | None = None
    human_worth_source: str | None = None
    human_worth_at: str | None = None


@dataclass(frozen=True)
class DossierSnapshotRow:
    slug: str
    name: str
    path: str
    artifact_path: str
    headline: str
    full_name: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    parent_id: str
    person_id: str | None = None
    children: tuple[str, ...] = ()
    body: str = ""
    source_channels: tuple[str, ...] = ()


@dataclass(frozen=True)
class LinkSnapshotRow(LinkRow):
    """Persisted link row with its human review columns."""

    decision_action: str | None = None
    decision_approved: str | None = None
    decision_source: str | None = None
    decision_note: str | None = None
    decided_at: str | None = None
    replacement_url: str | None = None
    replacement_public_identifier: str | None = None


@dataclass(frozen=True)
class CanonicalSnapshot:
    owner: dict | None
    owner_path: str | None
    parents: tuple[ParentSnapshotRow, ...]
    people: tuple[PersonRow, ...]
    identifiers: tuple[PersonIdentifierRow, ...]
    sources: tuple[PersonSourceRow, ...]
    artifacts: tuple[ArtifactRow, ...]
    facts: tuple[FactRow, ...]
    dossiers: tuple[DossierSnapshotRow, ...]
    merge_verdicts: tuple[MergeVerdictRow, ...]


@dataclass(frozen=True)
class IdentitySnapshot:
    links: tuple[LinkSnapshotRow, ...]
    memberships: tuple[CandidatePersonRow, ...]
    synthetic_profiles: tuple[SyntheticProfileRow, ...]
    research: tuple[ResearchRow, ...]
    review_rows: tuple[ReviewExportRow, ...]
    guidance: tuple[dict, ...]
    link_decisions: dict[str, dict[str, str]]


@dataclass(frozen=True)
class ReviewExportRow:
    key: str
    public_identifier: str = ""
    worth_person_ids: str = ""
    action: str = ""
    approved: str = ""
    new_linkedin_url: str = ""
    new_public_identifier: str = ""
    linkedin_url: str = ""
    match_emails: str = ""
    match_phones: str = ""
    confidence: str = ""
    reason: str = ""
    person_id: str = ""
    source: str = ""
    updated_at: str = ""
    llm_reject: str = ""
    llm_reject_confidence: str = ""
    llm_reject_reason: str = ""
    llm_judge_fingerprint: str = ""
    llm_worth: str = ""
    llm_worth_reason: str = ""
    network_worth: str = ""
    user_worth_note: str = ""
