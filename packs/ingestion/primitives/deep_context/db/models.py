"""Typed Deep Context SQLite domain rows and value vocabularies.

The schema owns only DDL construction. Runtime callers import these definitions
from their concrete home so the domain model can be read without the SQL text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

IsoTimestamp = str


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
    """Human DECISION provenance: who made a review decision.

    Governs `links.decision_source` and `parents.human_worth_source` only.
    These are the two decision columns `Db.decide_identity`/`Db.decide_worth`
    write, gated by `HUMAN_DECISION_SOURCES`/identity_policy's settling rules.
    Do not use this enum to record which stage WROTE a link or parent row —
    that is writer provenance, `WriterSource`, even when the literal string
    value is identical (e.g. "deep-context-review" and "user-guidance" each
    also appear as writer values, because the same stage name can describe
    both who ran the machine step and who decided its outcome).
    """

    REVIEW = "deep-context-review"
    USER_GUIDANCE = "user-guidance"
    SIBLING_SETTLE = "sibling-settle"


class WriterSource(StrEnum):
    """Machine WRITER provenance: which stage created/wrote this row.

    Governs `links.source` and `parents.source` only. This is a superset of
    the stage names in `ReviewSource` by design: e.g. a guided-retarget
    research run stamps `links.source = "user-guidance"` to record that the
    guidance flow produced the machine judgment, which is a different fact
    from `links.decision_source = "user-guidance"` recording that a human
    approved it. See `ReviewSource` for the decision-provenance half.
    """

    RECONCILE = "deep-context-reconcile"
    SYNTHESIS = "deep-context-synthesis"
    DEEP_RESEARCH = "deep-research"
    LEGACY_MIGRATION = "legacy-migration"
    REVIEW = "deep-context-review"
    USER_GUIDANCE = "user-guidance"
    HEAL = "deep-context-heal"
    PARENT_WORTH = "deep-context-parent-worth"
    DOSSIER_SELF_REPORTED = "dossier-self-reported"
    NAME_MATCH = "deep-context-name-match"


class RowKind(StrEnum):
    """Retired: MESSAGE_LINKEDIN (removed 2026-08-07). The legacy migration now
    skips minting a `links` row for that key shape outright — see
    `primitives/common/legacy.py`'s "Retired message-linkedin identity
    aliases" section — so no writer can produce it anymore.
    """

    PUB = "pub"
    PERSON_UUID = "person_uuid"
    CANDIDATE_EMAIL = "candidate_email"
    CANDIDATE_PHONE = "candidate_phone"
    SYNTHETIC = "synthetic"
    GHOST = "ghost"
    RESEARCH = "research"
    PARENT = "parent"


class IdentifierKind(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    LINKEDIN = "linkedin"


class SourceChannel(StrEnum):
    """Person-source vocabulary; message payloads use collection.MessageChannel."""

    GMAIL = "gmail_msgvault"
    IMESSAGE = "imessage"
    WHATSAPP = "whatsapp"
    LINKEDIN = "linkedin_csv"


# Person-source eligibility: which channels can supply message bodies to collect.
# LinkedIn-only people have none.
MESSAGE_CHANNELS: frozenset[str] = frozenset(
    {SourceChannel.GMAIL, SourceChannel.IMESSAGE, SourceChannel.WHATSAPP}
)


class ArtifactKind(StrEnum):
    FACTS = "facts"
    DOSSIER = "dossier"
    PROFILE = "profile"
    AVATAR = "avatar"
    RESEARCH = "research"
    SYNTHETIC = "synthetic"
    SOURCE_BUNDLE = "source_bundle"


PARENT_DOSSIER_ARTIFACT_PREFIX = "dossier-parent:"


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


class IdentityOrigin(StrEnum):
    """Evidence origin selects policy; guided research is still research evidence."""

    ATTACHED = "attached"
    RESEARCH = "research"


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
HUMAN_REVIEW_ACTIONS = frozenset(
    {
        ReviewAction.VERIFY.value,
        ReviewAction.DETACH.value,
        ReviewAction.RETARGET.value,
        ReviewAction.EXCLUDE.value,
    }
)
PARENT_WORTH_PREFIX = "parent-worth:"
LLM_REJECT_VALUES = ("yes", "no", "spam")
# These provenance-specific risk limits are policy, not caller tuning knobs:
# changing one changes which paid judgments auto-apply before human review.
IDENTITY_THRESHOLDS = {
    "attached_confirm": 0.70,  # Imported links are already anchored to observed identity evidence.
    "research_confirm": 0.80,  # Speculative research needs stronger corroboration before retargeting.
    "detach": 0.85,  # Destructive removal remains more conservative than attached confirmation.
    "decisive": 0.95,  # A conflict can auto-settle only with near-certain positive evidence.
}


class ResearchHandle:
    """Stable paid-research cache key shared by batch and guided paths."""

    @staticmethod
    def for_parent(parent_id: str, display_slug: str | None) -> str:
        return (display_slug or "").strip() or parent_id


JUDGE_CONFIRM_THRESHOLD = IDENTITY_THRESHOLDS["attached_confirm"]
RESEARCH_CONFIRM_THRESHOLD = IDENTITY_THRESHOLDS["research_confirm"]
JUDGE_DETACH_THRESHOLD = IDENTITY_THRESHOLDS["detach"]
DECISIVE_CONFIRM_THRESHOLD = IDENTITY_THRESHOLDS["decisive"]


@dataclass(frozen=True)
class OwnerContextRow:
    context_key: str
    payload_json: str
    path: str
    content_fingerprint: str
    projected_at: IsoTimestamp | None = None


@dataclass(frozen=True)
class ParentRow:
    parent_id: str
    public_identifier: str
    display_name: str | None = None
    display_slug: str | None = None
    machine_worth: str | None = None
    machine_worth_reason: str | None = None
    source: str | None = None
    updated_at: IsoTimestamp | None = None


@dataclass(frozen=True)
class PersonRow:
    person_id: str
    parent_id: str
    child_slug: str | None = None
    parent_slug: str | None = None
    display_name: str | None = None
    is_owner: bool = False
    is_ghost: bool = False
    facts_json: str | None = None
    confidence: float | None = None
    updated_at: IsoTimestamp | None = None


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
    authoritative_detach: bool = False
    paid_profile: bool = False
    judgment_fingerprint: str | None = None
    judgment_artifact_path: str | None = None
    judgment_payload_json: str | None = None
    source: str | None = None
    updated_at: IsoTimestamp | None = None


@dataclass(frozen=True)
class LinkRow(_IdentityMachineFields):
    row_key: str
    parent_id: str
    public_identifier: str
    kind: str
    linkedin_url: str | None = None
    display_name: str | None = None
    candidate_origin: bool = False
    raw_import: bool = False


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
    """Migration-only whole-graph input.

    Removal countdown (2026-08-06): delete once no supported install predates
    powerpacks v1.19.0.
    """

    parents: tuple[ParentRow, ...]
    people: tuple[PersonRow, ...]
    identifiers: tuple[PersonIdentifierRow, ...]
    sources: tuple[PersonSourceRow, ...]


@dataclass(frozen=True)
class CanonicalGraphCounts:
    """Migration-only whole-graph result counts.

    Removal countdown (2026-08-06): delete once no supported install predates
    powerpacks v1.19.0.
    """

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
    projected_at: IsoTimestamp | None = None


@dataclass(frozen=True)
class ArtifactReplacement:
    kind: str
    rows: tuple[ArtifactRow, ...]
    person_id: str | None = None
    parent_id: str | None = None


@dataclass(frozen=True)
class FactRow:
    subject_key: str
    parent_id: str
    artifact_key: str
    person_id: str | None = None
    machine_worth: str | None = None
    machine_worth_reason: str | None = None
    confidence: float | None = None
    is_owner: bool = False
    facts_json: str | None = None
    projected_at: IsoTimestamp | None = None


@dataclass(frozen=True)
class SyntheticProfileRow:
    public_identifier: str
    candidate_key: str
    profile_json: str
    source_artifact_key: str | None = None
    linkedin_url: str | None = None
    name: str | None = None
    updated_at: IsoTimestamp | None = None


@dataclass(frozen=True)
class ResearchRow:
    handle: str
    parent_id: str
    status: str
    candidate_key: str | None = None
    artifact_key: str | None = None
    selection_fingerprint: str | None = None
    result_json: str | None = None
    updated_at: IsoTimestamp | None = None


@dataclass(frozen=True)
class ArtifactProjection:
    """One artifact and the typed rows refreshed only when its content changes."""

    artifact: ArtifactRow
    raw_artifact: ArtifactRow | None = None
    candidate: LinkRow | None = None
    candidate_people: CandidatePeopleProjection | None = None
    fact: FactRow | None = None
    research: ResearchRow | None = None
    synthetic_profile: SyntheticProfileRow | None = None


@dataclass(frozen=True)
class GuidanceRow:
    handle: str
    parent_id: str
    guidance: str
    state: str = GuidanceState.PENDING.value
    candidate_key: str | None = None
    submitted_at: IsoTimestamp | None = None
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
    started_at: IsoTimestamp | None = None
    finished_at: IsoTimestamp | None = None


@dataclass(frozen=True)
class MergeVerdictRow:
    person_a: str
    person_b: str
    slug_a: str
    slug_b: str
    signature: str
    judge: str
    same_person: bool
    confidence: float
    tone_consistent: bool
    reason: str = ""
    accepted: bool = False
    updated_at: IsoTimestamp | None = None


@dataclass(frozen=True)
class ResetReviewCounts:
    human_worth_cleared: int
    human_identity_cleared: int


@dataclass(frozen=True)
class DerivedResetCounts:
    artifacts: int
    facts: int
    research: int
    jobs: int
    guidance: int


@dataclass(frozen=True)
class ParentSnapshotRow(ParentRow):
    """Persisted parent row with its human review columns."""

    human_worth: str | None = None
    human_worth_note: str | None = None
    human_worth_source: str | None = None
    human_worth_at: IsoTimestamp | None = None


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
    decided_at: IsoTimestamp | None = None
    replacement_url: str | None = None
    replacement_public_identifier: str | None = None


@dataclass(frozen=True)
class OwnerEducation:
    school: str
    start: int | str | None = None
    end: int | str | None = None
    note: str = ""


@dataclass(frozen=True)
class OwnerWork:
    company: str
    title: str = ""
    start: int | str | None = None
    end: int | str | None = None


@dataclass(frozen=True)
class OwnerProfile:
    name: str
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    education: tuple[OwnerEducation, ...] = ()
    work: tuple[OwnerWork, ...] = ()
    locations: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class GuidanceRequestSnapshot:
    slug: str
    row_key: str
    name: str
    guidance: str
    person_ids: tuple[str, ...] = ()
    linkedin_url: str = ""
    submitted_at: IsoTimestamp | None = None
    match_emails: tuple[str, ...] = ()
    match_phones: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuidanceDetailSnapshot:
    slug: str
    row_key: str
    name: str
    guidance: str
    state: str
    detail: str
    submitted_at: IsoTimestamp | None
    updated_at: IsoTimestamp | None
    new_url: str | None = None
    request: GuidanceRequestSnapshot | None = None
    wire_fields: tuple[str, ...] = ()
    extra_json: str = "{}"


@dataclass(frozen=True)
class GuidanceSnapshotRow:
    handle: str
    parent_id: str
    guidance: str
    state: str
    candidate_key: str | None = None
    submitted_at: IsoTimestamp | None = None
    applied_url: str | None = None
    detail: GuidanceDetailSnapshot | None = None


@dataclass(frozen=True)
class CanonicalSnapshot:
    """Migration-proof graph; delete once no install predates v1.19.0."""

    owner: OwnerProfile | None
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
class ReviewExportRow:
    key: str
    public_identifier: str = ""
    worth_person_ids: str | None = None
    action: str | None = None
    approved: str | None = None
    new_linkedin_url: str | None = None
    new_public_identifier: str | None = None
    linkedin_url: str | None = None
    match_emails: str | None = None
    match_phones: str | None = None
    confidence: str | None = None
    reason: str | None = None
    person_id: str | None = None
    source: str = ""
    updated_at: IsoTimestamp | None = None
    llm_reject: str | None = None
    llm_reject_confidence: str | None = None
    llm_reject_reason: str | None = None
    llm_judge_fingerprint: str | None = None
    llm_worth: str | None = None
    llm_worth_reason: str | None = None
    network_worth: str | None = None
    user_worth_note: str | None = None
