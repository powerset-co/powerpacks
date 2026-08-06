"""Typed Deep Context SQLite domain rows and value vocabularies.

The schema owns only DDL construction. Runtime callers import these definitions
from their concrete home so the domain model can be read without the SQL text.
"""
from __future__ import annotations

import re
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


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    NEEDS_APPROVAL = "needs_approval"
    COMPLETE = "complete"
    FAILED = "failed"


HUMAN_DECISION_SOURCES = frozenset({ReviewSource.REVIEW.value, ReviewSource.USER_GUIDANCE.value})
PARENT_WORTH_PREFIX = "parent-worth:"
LLM_REJECT_VALUES = ("yes", "no", "spam")
JUDGE_CONFIRM_THRESHOLD = 0.70
JUDGE_DETACH_THRESHOLD = 0.85
DECISIVE_CONFIRM_THRESHOLD = 0.95
RESEARCH_CONFIRM_THRESHOLD = 0.80
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def classify_review_key(key: str) -> RowKind:
    if key.startswith(PARENT_WORTH_PREFIX):
        return RowKind.PARENT
    if key.startswith("candidate:email:"):
        return RowKind.CANDIDATE_EMAIL
    if key.startswith("candidate:phone:"):
        return RowKind.CANDIDATE_PHONE
    if key.startswith("message-linkedin:"):
        return RowKind.MESSAGE_LINKEDIN
    if _UUID_RE.match(key):
        return RowKind.PERSON_UUID
    return RowKind.PUB


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


@dataclass(frozen=True)
class LinkRow:
    row_key: str
    parent_id: str
    public_identifier: str
    kind: str
    linkedin_url: str | None = None
    display_name: str | None = None
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
    candidate_origin: int = 0
    raw_import: int = 0
    paid_profile: int = 0
    judgment_fingerprint: str | None = None
    judgment_artifact_path: str | None = None
    judgment_payload_json: str | None = None
    source: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class IdentityMachineProjection:
    row_key: str
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
class CandidatePersonRow:
    row_key: str
    person_id: str
    parent_id: str


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
class StageStateRow:
    stage: str
    status: str
    selection_fingerprint: str | None = None
    artifact_fingerprint: str | None = None
    completed_at: str | None = None
    error: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class SpendApprovalRow:
    stage: str
    selection_fingerprint: str
    approved_count: int
    approved_amount: float | None = None
    approved_at: str | None = None


@dataclass(frozen=True)
class ResetReviewCounts:
    human_worth_cleared: int
    human_identity_cleared: int
    stage_states_reset: int
    spend_approvals_cleared: int
