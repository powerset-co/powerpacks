"""deep-context store schema: the value vocabulary and the tables, one home.

Single-user local tool (rule zero, docs/deep-context-sqlite-rewrite.md): the
db is the record, the CSVs are re-derivable export batons, and there is no
lock/recovery/version ceremony — a schema-version mismatch drops and rebuilds
from the batons.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# v4: the rewrite schema (db package). Derived state: mismatch -> rebuild.
SCHEMA_VERSION = 4


class ReviewAction(StrEnum):
    """Identity outcomes an action can carry (empty = no proposal)."""

    VERIFY = "verify"
    DETACH = "detach"
    RETARGET = "retarget"
    EXCLUDE = "exclude"
    REVIEW = "review"  # name-match proposal awaiting the human


class ApprovedState(StrEnum):
    """Who settled a decision: machine-standing, human yes, human no."""

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
    """Every writer that stamps a source, one member per stamp site."""

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
    """The typed spelling of the review key namespaces."""

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
    """THE namespace decision for a normalized review key."""
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
class LinkRow:
    """One identity row (any non-parent review key)."""

    row_key: str
    public_identifier: str
    kind: str
    person_id: str = ""
    linkedin_url: str = ""
    proposed_action: str = ""
    new_linkedin_url: str = ""
    new_public_identifier: str = ""
    confidence: str = ""
    reason: str = ""
    match_emails: str = ""
    match_phones: str = ""
    llm_reject: str = ""
    llm_reject_confidence: str = ""
    llm_reject_reason: str = ""
    llm_judge_fingerprint: str = ""  # paid-verdict cache key: copied verbatim
    llm_worth: str = ""
    llm_worth_reason: str = ""
    source: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ParentRow:
    """One parent-worth row: machine worth bookkeeping for a cluster."""

    parent_id: str
    public_identifier: str
    worth_person_ids: str = ""
    llm_worth: str = ""
    llm_worth_reason: str = ""
    source: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class DecisionRow:
    """One terminal outcome; absence of a row == pending."""

    kind: str
    target: str
    value: str
    approved: str = ""
    source: str = ""
    note: str = ""
    decided_at: str = ""


def _values(*items) -> str:
    return "({})".format(", ".join(f"'{getattr(v, 'value', v)}'" for v in items))


_ACTIONS = _values("", *ReviewAction)
_SOURCES = _values("", *ReviewSource)
_MACHINE_WORTH = _values("", *MachineWorth)
_LINK_KINDS = _values(*(k for k in RowKind if k is not RowKind.PARENT))

DDL = f"""
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS people (
  -- The parent/child relation as DATA: siblings of X is a JOIN, never a
  -- runtime re-derivation.
  person_id   TEXT PRIMARY KEY,
  parent_id   TEXT NOT NULL CHECK (parent_id != ''),
  child_slug  TEXT NOT NULL DEFAULT '',
  parent_slug TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS people_by_parent ON people(parent_id);

CREATE TABLE IF NOT EXISTS links (
  row_key           TEXT PRIMARY KEY CHECK (row_key != ''),
  public_identifier TEXT NOT NULL,
  kind              TEXT NOT NULL CHECK (kind IN {_LINK_KINDS}),
  person_id         TEXT NOT NULL DEFAULT '',
  linkedin_url      TEXT NOT NULL DEFAULT '',
  proposed_action   TEXT NOT NULL DEFAULT '' CHECK (proposed_action IN {_ACTIONS}),
  new_linkedin_url  TEXT NOT NULL DEFAULT '',
  new_public_identifier TEXT NOT NULL DEFAULT '',
  confidence        TEXT NOT NULL DEFAULT '',
  reason            TEXT NOT NULL DEFAULT '',
  match_emails      TEXT NOT NULL DEFAULT '',
  match_phones      TEXT NOT NULL DEFAULT '',
  llm_reject        TEXT NOT NULL DEFAULT '' CHECK (llm_reject IN {_values("", *LLM_REJECT_VALUES)}),
  llm_reject_confidence TEXT NOT NULL DEFAULT '',
  llm_reject_reason TEXT NOT NULL DEFAULT '',
  llm_judge_fingerprint TEXT NOT NULL DEFAULT '',
  llm_worth         TEXT NOT NULL DEFAULT '' CHECK (llm_worth IN {_MACHINE_WORTH}),
  llm_worth_reason  TEXT NOT NULL DEFAULT '',
  source            TEXT NOT NULL DEFAULT '' CHECK (source IN {_SOURCES}),
  updated_at        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS links_by_person ON links(person_id);

CREATE TABLE IF NOT EXISTS parents (
  parent_id         TEXT PRIMARY KEY CHECK (parent_id != ''),
  public_identifier TEXT NOT NULL,
  worth_person_ids  TEXT NOT NULL DEFAULT '',
  llm_worth         TEXT NOT NULL DEFAULT '' CHECK (llm_worth IN {_MACHINE_WORTH}),
  llm_worth_reason  TEXT NOT NULL DEFAULT '',
  source            TEXT NOT NULL DEFAULT '' CHECK (source IN {_SOURCES}),
  updated_at        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS decisions (
  kind       TEXT NOT NULL CHECK (kind IN {_values(*DecisionKind)}),
  target     TEXT NOT NULL CHECK (target != ''),
  value      TEXT NOT NULL,
  approved   TEXT NOT NULL DEFAULT '',
  source     TEXT NOT NULL DEFAULT '' CHECK (source IN {_SOURCES}),
  note       TEXT NOT NULL DEFAULT '',
  decided_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (kind, target),
  CHECK ((kind = 'identity' AND value IN {_values(*ReviewAction)})
         OR (kind != 'identity' AND value IN {_values(*HumanWorth)})),
  CHECK ((kind = 'identity' AND approved IN {_values(*ApprovedState)})
         OR (kind = 'worth' AND approved = '')
         OR (kind = 'synthetic_gate' AND approved IN ('auto', 'yes')))
);
"""
