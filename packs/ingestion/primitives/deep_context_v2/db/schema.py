"""The whole v2 store, declared once.

Every enum below is the CHECK list of its column: the DDL is rendered from the
enums, so the two cannot drift. Every nullable column names what its NULL means
in a comment; none exists for a fallback.

Id space:
  candidate_id  'candidate:email:<address>' | 'candidate:phone:+<digits>'
                Email and phone normalization are pinned. There is no LinkedIn candidate: the
                LinkedIn export is the `connections` lookup, keyed by URL, never a candidate.
  parent_id     'p:<16 hex>' minted by free merge, dedupe or a split | 'li:<member_id>' once confirmed
  seq           AUTOINCREMENT, one counter per ledger table; comparable within a table only
  verdict_ref   'pair_verdicts:<candidate_a>|<candidate_b>|<signature>' | 'candidate_linkedins:<seq>'
  synthetic key 'synthetic:<research handle>': the research row a human accepted as the profile

Four views define the current state once for every reader. Parent rows are
latest-wins (a later merge must take effect for every member). Worth and LinkedIn
verdicts rank a human row above every machine row, then latest seq. SQLite views
are saved queries, not materialized; the indexes beneath them are what make the
reads fast. Readers per view are listed on the spec page.

Changelog:
- 2026-10-06 (page v29-v31): current_parent is latest-wins; current_profile reads any member's
  confirmed row and the synthetic key is the research handle; the LinkedIn export becomes the
  `connections` lookup (candidates.linkedin_url, SourceChannel.LINKEDIN and MergeReason.NAME_MATCH
  dropped; the name match is the pre-match in enrich, a proposed URL, not a merge).
- 2026-10-06 (Arthur): Yes on a synthetic card = use that profile. candidate_linkedins takes
  'synthetic:<parent_id>' as url and member id with origin synthetic, human-only; current_profile
  returns it once accepted. SYNTHETIC_PROFILE_PREFIX names the key.
- 2026-10-06 (round 2): pair_verdicts.judge dropped (constant); current_profile yields the
  confirmed URL or NULL only; the synthetic key 'synthetic:<parent_id>' is formed by the
  research reader for the current evidence handle, not stored or derived from history.
- 2026-10-06: Astra schema review applied (NOT NULL text keys, signature in the pair key,
  human precedence in the views, current_worth and current_linkedins views, worth fingerprint
  CHECK, research payload CHECK, URL index covering candidate, slam_dunk out of PairJudge,
  facts.confidence dropped, 16-hex minted parent ids).

Created: 2026-10-06
"""
from __future__ import annotations

from enum import StrEnum

SCHEMA_VERSION = 1
MINTED_PARENT_PREFIX = "p:"
MINTED_PARENT_HEX = 16
LINKEDIN_PARENT_PREFIX = "li:"
SYNTHETIC_PROFILE_PREFIX = "synthetic:"


class IdentifierKind(StrEnum):
    EMAIL = "email"
    PHONE = "phone"


class SourceChannel(StrEnum):
    GMAIL = "gmail_msgvault"
    IMESSAGE = "imessage"
    WHATSAPP = "whatsapp"


class MergeReason(StrEnum):
    SLAM_DUNK = "slam_dunk"
    SOL_SAME = "sol_same"
    SINGLETON = "singleton"
    JUDGE_CONFIRMED = "judge_confirmed"
    JUDGE_WRONG_PERSON = "judge_wrong_person"
    HUMAN = "human"


class Worth(StrEnum):
    YES = "yes"
    MAYBE = "maybe"
    NO = "no"


class DecidedBy(StrEnum):
    MACHINE = "machine"
    HUMAN = "human"


class Origin(StrEnum):
    LINKEDIN_NETWORK = "linkedin_network"
    RESEARCH = "research"
    SYNTHETIC = "synthetic"  # a human-accepted or human-rejected synthetic card; the machine never writes it
    HUMAN_OVERRIDE = "human_override"


class Verdict(StrEnum):
    CONFIRMED = "confirmed"
    WRONG_PERSON = "wrong_person"
    NEEDS_REVIEW = "needs_review"


class ResearchStatus(StrEnum):
    COMPLETE = "complete"
    NO_MATCH = "no_match"
    FAILED = "failed"


def _in(enum: type[StrEnum]) -> str:
    return "(" + ", ".join(f"'{member.value}'" for member in enum) + ")"


DDL = f"""
CREATE TABLE meta (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE owner (
  owner_key TEXT NOT NULL PRIMARY KEY CHECK (owner_key = 'owner'),
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  content_fingerprint TEXT NOT NULL,
  projected_at TEXT NOT NULL
);

CREATE TABLE candidates (
  candidate_id TEXT NOT NULL PRIMARY KEY,
  display_name TEXT NOT NULL,
  is_owner INTEGER NOT NULL CHECK (is_owner IN (0, 1)),
  import_json TEXT NOT NULL CHECK (json_valid(import_json)),
  imported_at TEXT NOT NULL
);
CREATE TABLE candidate_names (
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  PRIMARY KEY (candidate_id, name)
);
CREATE TABLE candidate_identifiers (
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN {_in(IdentifierKind)}),
  normalized_value TEXT NOT NULL,
  display_value TEXT NOT NULL,
  PRIMARY KEY (candidate_id, kind, normalized_value)
);
CREATE INDEX identifiers_by_value ON candidate_identifiers(kind, normalized_value);
CREATE TABLE candidate_sources (
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
  source TEXT NOT NULL CHECK (source IN {_in(SourceChannel)}),
  PRIMARY KEY (candidate_id, source)
);
CREATE INDEX candidate_sources_by_source ON candidate_sources(source, candidate_id);

CREATE TABLE connections (
  linkedin_url TEXT NOT NULL PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT,                                      -- NULL = the export had none
  position TEXT NOT NULL,
  company TEXT NOT NULL,
  imported_at TEXT NOT NULL
);

CREATE TABLE bundles (
  candidate_id TEXT NOT NULL PRIMARY KEY REFERENCES candidates(candidate_id) ON DELETE CASCADE,
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  content_fingerprint TEXT NOT NULL,
  collected_at TEXT NOT NULL
);

CREATE TABLE facts (
  candidate_id TEXT NOT NULL PRIMARY KEY REFERENCES candidates(candidate_id) ON DELETE CASCADE,
  facts_json TEXT NOT NULL CHECK (json_valid(facts_json)),
  input_fingerprint TEXT NOT NULL,
  model TEXT NOT NULL,
  reasoning_effort TEXT NOT NULL,
  synthesized_at TEXT NOT NULL
);

CREATE TABLE candidate_parent (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  parent_id TEXT NOT NULL,
  reason TEXT NOT NULL CHECK (reason IN {_in(MergeReason)}),
  verdict_ref TEXT,                                -- NULL on free-merge and singleton rows
  created_at TEXT NOT NULL
);
CREATE INDEX candidate_parent_by_candidate ON candidate_parent(candidate_id, seq);
CREATE INDEX candidate_parent_by_parent ON candidate_parent(parent_id, candidate_id, seq);

CREATE TABLE pair_verdicts (
  candidate_a TEXT NOT NULL REFERENCES candidates(candidate_id),
  candidate_b TEXT NOT NULL REFERENCES candidates(candidate_id),
  signature TEXT NOT NULL,
  same_person INTEGER CHECK (same_person IN (0, 1)),  -- NULL = Sol said uncertain
  confidence REAL NOT NULL,
  reason TEXT NOT NULL,
  judged_at TEXT NOT NULL,
  PRIMARY KEY (candidate_a, candidate_b, signature),
  CHECK (candidate_a < candidate_b)
);

CREATE TABLE worth (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  worth TEXT NOT NULL CHECK (worth IN {_in(Worth)}),
  decided_by TEXT NOT NULL CHECK (decided_by IN {_in(DecidedBy)}),
  reason TEXT NOT NULL,
  labels_json TEXT CHECK (labels_json IS NULL OR json_valid(labels_json)),  -- NULL on a human row
  input_fingerprint TEXT,                          -- NULL on a human row; a machine row must carry it
  created_at TEXT NOT NULL,
  CHECK (decided_by = 'human' OR input_fingerprint IS NOT NULL)
);
CREATE INDEX worth_by_candidate ON worth(candidate_id, seq);

CREATE TABLE candidate_linkedins (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  linkedin_url TEXT NOT NULL,
  member_id TEXT NOT NULL,
  origin TEXT NOT NULL CHECK (origin IN {_in(Origin)}),
  verdict TEXT NOT NULL CHECK (verdict IN {_in(Verdict)}),
  decided_by TEXT NOT NULL CHECK (decided_by IN {_in(DecidedBy)}),
  judgment_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK ((origin = 'synthetic') = (linkedin_url LIKE 'synthetic:%')),
  CHECK (origin <> 'synthetic' OR decided_by = 'human')
);
CREATE INDEX candidate_linkedins_by_member ON candidate_linkedins(candidate_id, member_id, seq);
CREATE INDEX candidate_linkedins_by_url ON candidate_linkedins(linkedin_url, candidate_id, seq);

CREATE TABLE research (
  handle TEXT NOT NULL PRIMARY KEY,
  parent_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN {_in(ResearchStatus)}),
  result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),  -- NULL unless status is complete, or a usable no_match card
  researched_at TEXT NOT NULL,
  CHECK (status <> 'complete' OR result_json IS NOT NULL)
);
CREATE INDEX research_by_parent ON research(parent_id);

CREATE VIEW current_parent AS
  SELECT candidate_id, parent_id, reason, seq FROM (
    SELECT m.*, row_number() OVER (PARTITION BY candidate_id ORDER BY seq DESC) AS rn
    FROM candidate_parent m)
  WHERE rn = 1;

CREATE VIEW current_worth AS
  SELECT parent_id, candidate_id, worth, decided_by, reason, labels_json, input_fingerprint, created_at, seq FROM (
    SELECT p.parent_id, w.*, row_number() OVER (
      PARTITION BY p.parent_id ORDER BY (w.decided_by = 'human') DESC, w.seq DESC) AS rn
    FROM current_parent p JOIN worth w USING (candidate_id))
  WHERE rn = 1;

CREATE VIEW current_linkedins AS
  SELECT seq, candidate_id, linkedin_url, member_id, origin, verdict, decided_by, judgment_fingerprint, created_at FROM (
    SELECT l.*, row_number() OVER (
      PARTITION BY candidate_id, member_id ORDER BY (decided_by = 'human') DESC, seq DESC) AS rn
    FROM candidate_linkedins l)
  WHERE rn = 1;

CREATE VIEW current_profile AS
  SELECT p.parent_id,
         COALESCE(
           (SELECT MIN(l.linkedin_url) FROM current_linkedins l JOIN current_parent cp USING (candidate_id)
            WHERE cp.parent_id = p.parent_id AND 'li:' || l.member_id = p.parent_id AND l.verdict = 'confirmed'),
           (SELECT MIN(l.linkedin_url) FROM current_linkedins l JOIN current_parent cp USING (candidate_id)
            WHERE cp.parent_id = p.parent_id AND l.origin = 'synthetic' AND l.verdict = 'confirmed')
         ) AS profile_key
  FROM (SELECT DISTINCT parent_id FROM current_parent) p;
"""

TABLES: tuple[str, ...] = (
    "meta", "owner", "candidates", "candidate_names", "candidate_identifiers", "candidate_sources", "connections",
    "bundles", "facts", "candidate_parent", "pair_verdicts", "worth", "candidate_linkedins", "research",
)
VIEWS: tuple[str, ...] = ("current_parent", "current_worth", "current_linkedins", "current_profile")
# A view is a saved query over these tables; a node that declares the view reads them.
VIEW_TABLES: dict[str, tuple[str, ...]] = {
    "current_parent": ("candidate_parent",),
    "current_worth": ("candidate_parent", "worth"),
    "current_linkedins": ("candidate_linkedins",),
    "current_profile": ("candidate_parent", "candidate_linkedins"),
}
