"""Relational schema and typed projection rows for Deep Context SQLite.

Files remain the durable enrichment evidence.  These rows are the queryable
projection and human decisions; every owner relation is enforced by SQLite.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

SCHEMA_VERSION = 6


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
class CandidatePersonRow:
    row_key: str
    person_id: str
    parent_id: str


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


def _values(*items: object) -> str:
    return "({})".format(", ".join(f"'{getattr(v, 'value', v)}'" for v in items))


_ACTIONS = _values(*ReviewAction)
_APPROVALS = _values(*ApprovedState)
_KINDS = _values(*(kind for kind in RowKind if kind is not RowKind.PARENT))
_WORTH = _values(*MachineWorth)

DDL = f"""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE parents (
  parent_id TEXT PRIMARY KEY, public_identifier TEXT NOT NULL,
  display_name TEXT, display_slug TEXT,
  machine_worth TEXT CHECK (machine_worth IS NULL OR machine_worth IN {_WORTH}),
  machine_worth_reason TEXT,
  human_worth TEXT CHECK (human_worth IS NULL OR human_worth IN {_values(*HumanWorth)}),
  human_worth_note TEXT, human_worth_source TEXT, human_worth_at TEXT,
  source TEXT, updated_at TEXT
);

CREATE TABLE people (
  person_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL,
  child_slug TEXT, parent_slug TEXT, display_name TEXT,
  is_owner INTEGER NOT NULL DEFAULT 0 CHECK (is_owner IN (0, 1)),
  is_ghost INTEGER NOT NULL DEFAULT 0 CHECK (is_ghost IN (0, 1)),
  facts_json TEXT CHECK (facts_json IS NULL OR json_valid(facts_json)),
  confidence REAL, updated_at TEXT,
  UNIQUE (person_id, parent_id),
  FOREIGN KEY (parent_id) REFERENCES parents(parent_id) ON DELETE CASCADE
);
CREATE INDEX people_by_parent ON people(parent_id);

CREATE TABLE person_identifiers (
  person_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN {_values(*IdentifierKind)}),
  normalized_value TEXT NOT NULL, display_value TEXT,
  PRIMARY KEY (person_id, kind, normalized_value),
  FOREIGN KEY (person_id) REFERENCES people(person_id) ON DELETE CASCADE
);
CREATE INDEX identifiers_by_value ON person_identifiers(kind, normalized_value);

CREATE TABLE person_sources (
  person_id TEXT NOT NULL, source TEXT NOT NULL CHECK (length(trim(source)) > 0),
  PRIMARY KEY (person_id, source),
  FOREIGN KEY (person_id) REFERENCES people(person_id) ON DELETE CASCADE
);
CREATE INDEX person_sources_by_source ON person_sources(source, person_id);

CREATE TABLE links (
  row_key TEXT PRIMARY KEY, parent_id TEXT NOT NULL, public_identifier TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN {_KINDS}), linkedin_url TEXT, display_name TEXT,
  machine_action TEXT CHECK (machine_action IS NULL OR machine_action IN {_ACTIONS}),
  machine_approved TEXT CHECK (machine_approved IS NULL OR machine_approved IN {_APPROVALS}),
  machine_confidence REAL, machine_reason TEXT, machine_judgment TEXT,
  machine_reject TEXT CHECK (machine_reject IS NULL OR machine_reject IN {_values(*LLM_REJECT_VALUES)}),
  machine_reject_confidence REAL, machine_reject_reason TEXT,
  machine_proposed_url TEXT, machine_proposed_public_identifier TEXT,
  authoritative_detach INTEGER NOT NULL DEFAULT 0 CHECK (authoritative_detach IN (0, 1)),
  candidate_origin INTEGER NOT NULL DEFAULT 0 CHECK (candidate_origin IN (0, 1)),
  raw_import INTEGER NOT NULL DEFAULT 0 CHECK (raw_import IN (0, 1)),
  paid_profile INTEGER NOT NULL DEFAULT 0 CHECK (paid_profile IN (0, 1)),
  judgment_fingerprint TEXT, judgment_artifact_path TEXT,
  judgment_payload_json TEXT CHECK (judgment_payload_json IS NULL OR json_valid(judgment_payload_json)),
  decision_action TEXT CHECK (decision_action IS NULL OR decision_action IN {_ACTIONS}),
  decision_approved TEXT CHECK (decision_approved IS NULL OR decision_approved IN {_APPROVALS}),
  decision_source TEXT, decision_note TEXT, decided_at TEXT,
  replacement_url TEXT, replacement_public_identifier TEXT,
  source TEXT, updated_at TEXT,
  UNIQUE (row_key, parent_id),
  FOREIGN KEY (parent_id) REFERENCES parents(parent_id) ON DELETE CASCADE,
  CHECK ((machine_action = 'retarget') OR
         (machine_proposed_url IS NULL AND machine_proposed_public_identifier IS NULL)),
  CHECK ((decision_action = 'retarget') OR
         (replacement_url IS NULL AND replacement_public_identifier IS NULL))
);
CREATE INDEX links_by_parent ON links(parent_id);

CREATE TABLE candidate_people (
  row_key TEXT NOT NULL, person_id TEXT NOT NULL, parent_id TEXT NOT NULL,
  PRIMARY KEY (row_key, person_id),
  FOREIGN KEY (row_key, parent_id) REFERENCES links(row_key, parent_id) ON DELETE CASCADE,
  FOREIGN KEY (person_id, parent_id) REFERENCES people(person_id, parent_id) ON DELETE CASCADE
);
CREATE INDEX candidate_people_by_person ON candidate_people(person_id);

CREATE TABLE artifacts (
  artifact_key TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN {_values(*ArtifactKind)}),
  parent_id TEXT NOT NULL, person_id TEXT, candidate_key TEXT,
  path TEXT NOT NULL, content_fingerprint TEXT NOT NULL, input_fingerprint TEXT,
  status TEXT NOT NULL CHECK (status IN {_values(*ProjectionStatus)}), error TEXT,
  payload_json TEXT CHECK (payload_json IS NULL OR json_valid(payload_json)), projected_at TEXT,
  UNIQUE (artifact_key, parent_id),
  FOREIGN KEY (parent_id) REFERENCES parents(parent_id) ON DELETE CASCADE,
  FOREIGN KEY (person_id, parent_id) REFERENCES people(person_id, parent_id) ON DELETE CASCADE,
  FOREIGN KEY (candidate_key, parent_id) REFERENCES links(row_key, parent_id) ON DELETE CASCADE,
  CHECK (person_id IS NULL OR candidate_key IS NULL)
);
CREATE INDEX artifacts_by_owner ON artifacts(parent_id, person_id, candidate_key, kind);

CREATE TABLE facts (
  subject_key TEXT PRIMARY KEY, parent_id TEXT NOT NULL, person_id TEXT,
  artifact_key TEXT NOT NULL UNIQUE,
  machine_worth TEXT CHECK (machine_worth IS NULL OR machine_worth IN {_WORTH}),
  machine_worth_reason TEXT, confidence REAL,
  is_owner INTEGER NOT NULL DEFAULT 0 CHECK (is_owner IN (0, 1)),
  facts_json TEXT CHECK (facts_json IS NULL OR json_valid(facts_json)), projected_at TEXT,
  FOREIGN KEY (parent_id) REFERENCES parents(parent_id) ON DELETE CASCADE,
  FOREIGN KEY (person_id, parent_id) REFERENCES people(person_id, parent_id) ON DELETE CASCADE,
  FOREIGN KEY (artifact_key, parent_id) REFERENCES artifacts(artifact_key, parent_id) ON DELETE CASCADE
);
CREATE INDEX facts_by_parent ON facts(parent_id);

CREATE TABLE synthetic_profiles (
  public_identifier TEXT PRIMARY KEY, candidate_key TEXT NOT NULL UNIQUE,
  source_artifact_key TEXT, linkedin_url TEXT, name TEXT,
  profile_json TEXT NOT NULL CHECK (json_valid(profile_json)), updated_at TEXT,
  FOREIGN KEY (candidate_key) REFERENCES links(row_key) ON DELETE CASCADE,
  FOREIGN KEY (source_artifact_key) REFERENCES artifacts(artifact_key) ON DELETE SET NULL
);

CREATE TRIGGER synthetic_kind_insert BEFORE INSERT ON synthetic_profiles
WHEN (SELECT kind FROM links WHERE row_key = NEW.candidate_key) != 'synthetic'
BEGIN SELECT RAISE(ABORT, 'synthetic profile candidate must have synthetic kind'); END;
CREATE TRIGGER synthetic_kind_update BEFORE UPDATE OF candidate_key ON synthetic_profiles
WHEN (SELECT kind FROM links WHERE row_key = NEW.candidate_key) != 'synthetic'
BEGIN SELECT RAISE(ABORT, 'synthetic profile candidate must have synthetic kind'); END;
CREATE TRIGGER synthetic_link_kind_update BEFORE UPDATE OF kind ON links
WHEN OLD.kind = 'synthetic' AND NEW.kind != 'synthetic'
 AND EXISTS (SELECT 1 FROM synthetic_profiles WHERE candidate_key = OLD.row_key)
BEGIN SELECT RAISE(ABORT, 'synthetic profile owns candidate kind'); END;

CREATE TABLE research (
  handle TEXT PRIMARY KEY, parent_id TEXT NOT NULL, candidate_key TEXT, artifact_key TEXT,
  status TEXT NOT NULL CHECK (status IN {_values(*ResearchStatus)}), selection_fingerprint TEXT,
  result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)), updated_at TEXT,
  FOREIGN KEY (parent_id) REFERENCES parents(parent_id) ON DELETE CASCADE,
  FOREIGN KEY (candidate_key, parent_id) REFERENCES links(row_key, parent_id) ON DELETE CASCADE,
  FOREIGN KEY (artifact_key, parent_id) REFERENCES artifacts(artifact_key, parent_id) ON DELETE RESTRICT
);

CREATE TABLE guidance (
  handle TEXT PRIMARY KEY, parent_id TEXT NOT NULL, candidate_key TEXT,
  guidance TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN {_values(*GuidanceState)}),
  submitted_at TEXT, applied_url TEXT,
  detail_json TEXT CHECK (detail_json IS NULL OR json_valid(detail_json)),
  FOREIGN KEY (parent_id) REFERENCES parents(parent_id) ON DELETE CASCADE,
  FOREIGN KEY (candidate_key, parent_id) REFERENCES links(row_key, parent_id) ON DELETE CASCADE
);

CREATE TABLE jobs (
  name TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN {_values(*JobKind)}),
  status TEXT NOT NULL CHECK (status IN {_values(*JobStatus)}), parent_id TEXT, candidate_key TEXT,
  selection_fingerprint TEXT, completed_count INTEGER NOT NULL DEFAULT 0,
  total_count INTEGER NOT NULL DEFAULT 0, error TEXT,
  result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
  started_at TEXT, finished_at TEXT,
  FOREIGN KEY (parent_id) REFERENCES parents(parent_id) ON DELETE CASCADE,
  FOREIGN KEY (candidate_key, parent_id) REFERENCES links(row_key, parent_id) ON DELETE CASCADE,
  CHECK (completed_count >= 0 AND total_count >= 0 AND completed_count <= total_count),
  CHECK (candidate_key IS NULL OR parent_id IS NOT NULL)
);

CREATE TABLE stage_state (
  stage TEXT PRIMARY KEY, status TEXT NOT NULL CHECK (status IN {_values(*StageStatus)}),
  selection_fingerprint TEXT, artifact_fingerprint TEXT,
  completed_at TEXT, error TEXT, updated_at TEXT
);

CREATE TABLE spend_approvals (
  stage TEXT PRIMARY KEY, selection_fingerprint TEXT NOT NULL,
  approved_count INTEGER NOT NULL CHECK (approved_count >= 0),
  approved_amount REAL CHECK (approved_amount IS NULL OR approved_amount >= 0),
  approved_at TEXT,
  FOREIGN KEY (stage) REFERENCES stage_state(stage) ON DELETE CASCADE
);
"""


ROW_TYPES = {
    "parents": ParentRow,
    "people": PersonRow,
    "person_identifiers": PersonIdentifierRow,
    "person_sources": PersonSourceRow,
    "links": LinkRow,
    "candidate_people": CandidatePersonRow,
    "artifacts": ArtifactRow,
    "facts": FactRow,
    "synthetic_profiles": SyntheticProfileRow,
    "research": ResearchRow,
    "guidance": GuidanceRow,
    "jobs": JobRow,
    "stage_state": StageStateRow,
    "spend_approvals": SpendApprovalRow,
}
