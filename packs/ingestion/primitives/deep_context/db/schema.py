"""Canonical SQLite domain types and schema for deep-context.

The database is runtime truth. Files named here are references to retained paid
artifacts, never an alternate state store. Schema changes are explicit: opening
an incompatible database raises instead of deleting or rebuilding it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

SCHEMA_VERSION = 4


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


class DecisionKind(StrEnum):
    IDENTITY = "identity"
    WORTH = "worth"
    SYNTHETIC_GATE = "synthetic_gate"


class RowKind(StrEnum):
    PUB = "pub"
    PERSON_UUID = "person_uuid"
    CANDIDATE_EMAIL = "candidate_email"
    CANDIDATE_PHONE = "candidate_phone"
    MESSAGE_LINKEDIN = "message_linkedin"
    PARENT = "parent"


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
class PersonRow:
    person_id: str
    parent_id: str
    child_slug: str | None = None
    parent_slug: str | None = None


@dataclass(frozen=True)
class LinkRow:
    row_key: str
    public_identifier: str
    kind: str
    person_id: str | None = None
    parent_id: str | None = None
    linkedin_url: str | None = None
    proposed_action: str | None = None
    new_linkedin_url: str | None = None
    new_public_identifier: str | None = None
    confidence: float | None = None
    reason: str | None = None
    match_emails: str | None = None       # JSON array
    match_phones: str | None = None       # JSON array
    llm_reject: str | None = None
    llm_reject_confidence: float | None = None
    llm_reject_reason: str | None = None
    llm_judge_fingerprint: str | None = None
    llm_worth: str | None = None
    llm_worth_reason: str | None = None
    source: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class ParentRow:
    parent_id: str
    public_identifier: str
    worth_person_ids: str | None = None   # JSON array
    llm_worth: str | None = None
    llm_worth_reason: str | None = None
    source: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class DecisionRow:
    kind: str
    target: str
    value: str
    approved: str | None = None
    source: str | None = None
    note: str | None = None
    decided_at: str | None = None


@dataclass(frozen=True)
class FactRow:
    subject_key: str
    person_id: str | None
    parent_id: str
    path: str
    mtime_ns: int
    llm_worth: str | None = None
    llm_worth_reason: str | None = None
    confidence: float | None = None
    facts_json: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class VerdictRow:
    candidate_key: str
    parent_id: str | None = None
    verdict: str | None = None
    confidence: float | None = None
    reason: str | None = None
    fingerprint: str | None = None
    payload_json: str | None = None
    judged_at: str | None = None


@dataclass(frozen=True)
class SyntheticProfileRow:
    public_identifier: str
    person_id: str | None = None
    parent_id: str | None = None
    linkedin_url: str | None = None
    name: str | None = None
    profile_json: str | None = None
    source_path: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class ResearchRow:
    handle: str
    person_id: str | None = None
    parent_id: str | None = None
    dir_path: str | None = None
    status: str | None = None
    fingerprint: str | None = None
    result_json: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class GuidanceRow:
    handle: str
    person_id: str | None = None
    guidance: str | None = None
    state: str = "pending"
    submitted_at: str | None = None
    applied_url: str | None = None
    detail_json: str | None = None


@dataclass(frozen=True)
class JobRow:
    name: str
    status: str
    progress_json: str | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class StageStateRow:
    stage: str
    state_json: str
    updated_at: str


def _values(*items: object) -> str:
    return "({})".format(", ".join(f"'{getattr(v, 'value', v)}'" for v in items))


_ACTIONS = _values(*ReviewAction)
_KINDS = _values(*(k for k in RowKind if k is not RowKind.PARENT))
_WORTH = _values(*MachineWorth)

DDL = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS people (
  person_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL,
  child_slug TEXT, parent_slug TEXT
);
CREATE INDEX IF NOT EXISTS people_by_parent ON people(parent_id);

CREATE TABLE IF NOT EXISTS parents (
  parent_id TEXT PRIMARY KEY, public_identifier TEXT NOT NULL,
  worth_person_ids TEXT CHECK (worth_person_ids IS NULL OR json_valid(worth_person_ids)),
  llm_worth TEXT CHECK (llm_worth IS NULL OR llm_worth IN {_WORTH}),
  llm_worth_reason TEXT, source TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS links (
  row_key TEXT PRIMARY KEY, public_identifier TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN {_KINDS}), person_id TEXT, parent_id TEXT,
  linkedin_url TEXT, proposed_action TEXT CHECK (proposed_action IS NULL OR proposed_action IN {_ACTIONS}),
  new_linkedin_url TEXT, new_public_identifier TEXT, confidence REAL, reason TEXT,
  match_emails TEXT CHECK (match_emails IS NULL OR json_valid(match_emails)),
  match_phones TEXT CHECK (match_phones IS NULL OR json_valid(match_phones)),
  llm_reject TEXT CHECK (llm_reject IS NULL OR llm_reject IN {_values(*LLM_REJECT_VALUES)}),
  llm_reject_confidence REAL, llm_reject_reason TEXT, llm_judge_fingerprint TEXT,
  llm_worth TEXT CHECK (llm_worth IS NULL OR llm_worth IN {_WORTH}),
  llm_worth_reason TEXT, source TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS links_by_person ON links(person_id);
CREATE INDEX IF NOT EXISTS links_by_parent ON links(parent_id);

CREATE TABLE IF NOT EXISTS decisions (
  kind TEXT NOT NULL CHECK (kind IN {_values(*DecisionKind)}), target TEXT NOT NULL,
  value TEXT NOT NULL, approved TEXT, source TEXT, note TEXT, decided_at TEXT,
  PRIMARY KEY (kind, target),
  CHECK ((kind = 'identity' AND value IN {_ACTIONS}) OR
         (kind != 'identity' AND value IN {_values(*HumanWorth)})),
  CHECK ((kind = 'identity' AND approved IN {_values(*ApprovedState)}) OR
         (kind = 'worth' AND approved IS NULL) OR
         (kind = 'synthetic_gate' AND approved IN ('auto', 'yes')))
);

CREATE TABLE IF NOT EXISTS facts (
  subject_key TEXT PRIMARY KEY, person_id TEXT, parent_id TEXT NOT NULL,
  path TEXT NOT NULL, mtime_ns INTEGER NOT NULL, llm_worth TEXT,
  llm_worth_reason TEXT, confidence REAL,
  facts_json TEXT CHECK (facts_json IS NULL OR json_valid(facts_json)), updated_at TEXT,
  CHECK (llm_worth IS NULL OR llm_worth IN {_WORTH})
);
CREATE INDEX IF NOT EXISTS facts_by_parent ON facts(parent_id);

CREATE TABLE IF NOT EXISTS verdicts (
  candidate_key TEXT PRIMARY KEY, parent_id TEXT, verdict TEXT, confidence REAL,
  reason TEXT, fingerprint TEXT,
  payload_json TEXT CHECK (payload_json IS NULL OR json_valid(payload_json)), judged_at TEXT
);
CREATE INDEX IF NOT EXISTS verdicts_by_parent ON verdicts(parent_id);

CREATE TABLE IF NOT EXISTS synthetic_profiles (
  public_identifier TEXT PRIMARY KEY, person_id TEXT, parent_id TEXT,
  linkedin_url TEXT, name TEXT,
  profile_json TEXT CHECK (profile_json IS NULL OR json_valid(profile_json)),
  source_path TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS synthetic_by_parent ON synthetic_profiles(parent_id);

CREATE TABLE IF NOT EXISTS research (
  handle TEXT PRIMARY KEY, person_id TEXT, parent_id TEXT, dir_path TEXT,
  status TEXT, fingerprint TEXT,
  result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)), updated_at TEXT
);

CREATE TABLE IF NOT EXISTS guidance (
  handle TEXT PRIMARY KEY, person_id TEXT, guidance TEXT, state TEXT NOT NULL,
  submitted_at TEXT, applied_url TEXT,
  detail_json TEXT CHECK (detail_json IS NULL OR json_valid(detail_json))
);

CREATE TABLE IF NOT EXISTS jobs (
  name TEXT PRIMARY KEY, status TEXT NOT NULL,
  progress_json TEXT CHECK (progress_json IS NULL OR json_valid(progress_json)),
  error TEXT, started_at TEXT, finished_at TEXT
);

CREATE TABLE IF NOT EXISTS stage_state (
  stage TEXT PRIMARY KEY,
  state_json TEXT NOT NULL CHECK (json_valid(state_json)), updated_at TEXT NOT NULL
);
"""


ROW_TYPES = {
    "people": PersonRow, "parents": ParentRow, "links": LinkRow,
    "decisions": DecisionRow, "facts": FactRow, "verdicts": VerdictRow,
    "synthetic_profiles": SyntheticProfileRow, "research": ResearchRow,
    "guidance": GuidanceRow, "jobs": JobRow, "stage_state": StageStateRow,
}
