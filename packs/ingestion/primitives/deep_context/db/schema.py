"""Versioned relational DDL, row-to-table registry, and generated upserts."""
from __future__ import annotations

from dataclasses import fields

from packs.ingestion.primitives.deep_context.db import models

# Pre-release installs re-migrate instead of carrying an upgrade ladder.
SCHEMA_VERSION = 1


def _values(*items: object) -> str:
    return "({})".format(", ".join(f"'{getattr(v, 'value', v)}'" for v in items))


_ACTIONS = _values(*models.ReviewAction)
_APPROVALS = _values(*models.ApprovedState)
_KINDS = _values(*(kind for kind in models.RowKind if kind is not models.RowKind.PARENT))
_WORTH = _values(*models.MachineWorth)

DDL = f"""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE owner_context (
  context_key TEXT PRIMARY KEY CHECK (context_key = 'owner'),
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  path TEXT NOT NULL, content_fingerprint TEXT NOT NULL, projected_at TEXT
);

CREATE TABLE parents (
  parent_id TEXT PRIMARY KEY, public_identifier TEXT NOT NULL,
  display_name TEXT, display_slug TEXT,
  machine_worth TEXT CHECK (machine_worth IS NULL OR machine_worth IN {_WORTH}),
  machine_worth_reason TEXT,
  human_worth TEXT CHECK (human_worth IS NULL OR human_worth IN {_values(*models.HumanWorth)}),
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
  kind TEXT NOT NULL CHECK (kind IN {_values(*models.IdentifierKind)}),
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
  machine_reject TEXT CHECK (machine_reject IS NULL OR machine_reject IN {_values(*models.LLM_REJECT_VALUES)}),
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
  kind TEXT NOT NULL CHECK (kind IN {_values(*models.ArtifactKind)}),
  parent_id TEXT NOT NULL, person_id TEXT, candidate_key TEXT,
  path TEXT NOT NULL, content_fingerprint TEXT NOT NULL, input_fingerprint TEXT,
  status TEXT NOT NULL CHECK (status IN {_values(*models.ProjectionStatus)}), error TEXT,
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
  status TEXT NOT NULL CHECK (status IN {_values(*models.ResearchStatus)}), selection_fingerprint TEXT,
  result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)), updated_at TEXT,
  FOREIGN KEY (parent_id) REFERENCES parents(parent_id) ON DELETE CASCADE,
  FOREIGN KEY (candidate_key, parent_id) REFERENCES links(row_key, parent_id) ON DELETE CASCADE,
  FOREIGN KEY (artifact_key, parent_id) REFERENCES artifacts(artifact_key, parent_id) ON DELETE RESTRICT
);

CREATE TABLE guidance (
  handle TEXT PRIMARY KEY, parent_id TEXT NOT NULL, candidate_key TEXT,
  guidance TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN {_values(*models.GuidanceState)}),
  submitted_at TEXT, applied_url TEXT,
  detail_json TEXT CHECK (detail_json IS NULL OR json_valid(detail_json)),
  FOREIGN KEY (parent_id) REFERENCES parents(parent_id) ON DELETE CASCADE,
  FOREIGN KEY (candidate_key, parent_id) REFERENCES links(row_key, parent_id) ON DELETE CASCADE
);

CREATE TABLE jobs (
  name TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN {_values(*models.JobKind)}),
  status TEXT NOT NULL CHECK (status IN {_values(*models.JobStatus)}), parent_id TEXT, candidate_key TEXT,
  selection_fingerprint TEXT, completed_count INTEGER NOT NULL DEFAULT 0,
  total_count INTEGER NOT NULL DEFAULT 0, error TEXT,
  result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
  started_at TEXT, finished_at TEXT,
  FOREIGN KEY (parent_id) REFERENCES parents(parent_id) ON DELETE CASCADE,
  FOREIGN KEY (candidate_key, parent_id) REFERENCES links(row_key, parent_id) ON DELETE CASCADE,
  CHECK (completed_count >= 0 AND total_count >= 0 AND completed_count <= total_count),
  CHECK (candidate_key IS NULL OR parent_id IS NOT NULL)
);

CREATE TABLE merge_verdicts (
  person_a TEXT NOT NULL, person_b TEXT NOT NULL,
  slug_a TEXT NOT NULL, slug_b TEXT NOT NULL,
  signature TEXT NOT NULL CHECK (length(trim(signature)) > 0),
  judge TEXT NOT NULL CHECK (judge IN ('slam_dunk', 'llm')),
  same_person INTEGER NOT NULL CHECK (same_person IN (0, 1)),
  confidence REAL NOT NULL,
  tone_consistent INTEGER NOT NULL CHECK (tone_consistent IN (0, 1)),
  reason TEXT NOT NULL DEFAULT '',
  accepted INTEGER NOT NULL DEFAULT 0 CHECK (accepted IN (0, 1)),
  updated_at TEXT,
  PRIMARY KEY (person_a, person_b),
  FOREIGN KEY (person_a) REFERENCES people(person_id) ON DELETE CASCADE,
  FOREIGN KEY (person_b) REFERENCES people(person_id) ON DELETE CASCADE,
  CHECK (person_a < person_b)
);

"""


TABLES = {
    "owner_context": (models.OwnerContextRow, ("context_key",)),
    "parents": (models.ParentRow, ("parent_id",)),
    "people": (models.PersonRow, ("person_id",)),
    "person_identifiers": (models.PersonIdentifierRow, ("person_id", "kind", "normalized_value")),
    "person_sources": (models.PersonSourceRow, ("person_id", "source")),
    "links": (models.LinkRow, ("row_key",)),
    "candidate_people": (models.CandidatePersonRow, ("row_key", "person_id")),
    "artifacts": (models.ArtifactRow, ("artifact_key",)),
    "facts": (models.FactRow, ("subject_key",)),
    "synthetic_profiles": (models.SyntheticProfileRow, ("public_identifier",)),
    "research": (models.ResearchRow, ("handle",)),
    "guidance": (models.GuidanceRow, ("handle",)),
    "jobs": (models.JobRow, ("name",)),
    "merge_verdicts": (models.MergeVerdictRow, ("person_a", "person_b")),
}
TABLE_BY_TYPE = {row_type: table for table, (row_type, _) in TABLES.items()}


def _upsert_sql(table: str) -> str:
    row_type, keys = TABLES[table]
    names = [field.name for field in fields(row_type)]
    updates = [name for name in names if name not in keys]
    insert = (
        f"INSERT INTO {table} ({', '.join(names)}) VALUES "
        f"({', '.join(':' + name for name in names)}) ON CONFLICT "
        f"({', '.join(keys)}) "
    )
    return insert + (
        "DO UPDATE SET " + ", ".join(f"{name}=excluded.{name}" for name in updates)
        if updates else "DO NOTHING"
    )


UPSERTS = {table: _upsert_sql(table) for table in TABLES}
