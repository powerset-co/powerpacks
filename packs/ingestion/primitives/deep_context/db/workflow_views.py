"""Queue-derived Deep Context workflow state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from packs.ingestion.primitives.deep_context.db._view_rows import (
    _linkedin_progress,
)
from packs.ingestion.primitives.deep_context.db._view_sql import (
    WORTH_CTE,
    WORTH_GATE_ACCEPTED,
    WORTH_GATE_REJECTED,
)
from packs.ingestion.primitives.deep_context.db.identity_views import enrichment_queue
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.worth_views import worth_counts, worth_rows


@dataclass(frozen=True)
class StageProgress:
    total: int
    worth_total: int
    worth_pending: int
    worth_yes: int
    worth_no: int
    lookup_ready: int
    linkedin_total: int
    linkedin_pending: int
    linkedin_done: int
    rejected: int


@dataclass(frozen=True)
class ReviewSelection:
    fingerprint: str
    total: int
    yes: int
    maybe: int
    no: int
    review_revision: str


@dataclass(frozen=True)
class WorkflowState:
    primitive: str
    status: str
    next_action: str
    progress: StageProgress
    selection: ReviewSelection
    state_token: str


def _stage_progress(db: Db) -> StageProgress:
    worth = worth_counts(db)
    linkedin = _linkedin_progress(db)
    lookup_ready = db.query(
        WORTH_CTE
        + f"""
SELECT count(*) AS n FROM worth w
WHERE {WORTH_GATE_ACCEPTED}
  AND (
    EXISTS(SELECT 1 FROM links l WHERE l.parent_id=w.parent_id AND l.raw_import=1)
    OR (
      (
        EXISTS(SELECT 1 FROM links l WHERE l.parent_id=w.parent_id)
        OR EXISTS(
          SELECT 1 FROM artifacts a
          WHERE a.parent_id=w.parent_id AND a.kind='research'
        )
      )
      AND NOT EXISTS(
        SELECT 1 FROM people pe
        WHERE pe.parent_id=w.parent_id AND pe.person_id NOT LIKE 'candidate:%'
      )
    )
  )
"""
    )[0]["n"]
    total = db.query("SELECT count(*) AS n FROM parents")[0]["n"]
    rejected = db.query(
        WORTH_CTE
        + f""", linkedin_csv_parents AS (
  SELECT DISTINCT pe.parent_id FROM people pe JOIN person_sources ps USING(person_id)
  WHERE ps.source='linkedin_csv'
), kept_parents AS (
  SELECT DISTINCT parent_id FROM links
  WHERE decision_approved='yes' AND decision_action NOT IN ('detach', 'exclude')
)
SELECT count(DISTINCT parent_id) AS n FROM (
  SELECT w.parent_id FROM worth w
  WHERE {WORTH_GATE_REJECTED}
    AND (
      w.human_worth='no'
      OR (
        w.human_worth IS NULL
        AND w.parent_id NOT IN (SELECT parent_id FROM linkedin_csv_parents)
        AND w.parent_id NOT IN (SELECT parent_id FROM kept_parents)
      )
    )
  UNION ALL
  SELECT parent_id FROM links
  WHERE decision_action='exclude' AND decision_approved IN ('auto', 'yes')
  UNION ALL
  SELECT p.parent_id FROM parents p
  WHERE EXISTS (
      SELECT 1 FROM links rejected
      WHERE rejected.parent_id=p.parent_id AND rejected.kind='synthetic'
        AND rejected.decision_action='detach' AND rejected.decision_approved='yes'
    )
    AND NOT EXISTS (
      SELECT 1 FROM links real
      WHERE real.parent_id=p.parent_id AND real.kind!='synthetic'
    )
)
"""
    )[0]["n"]
    return StageProgress(
        total=int(total),
        worth_total=worth.total,
        worth_pending=worth.pending,
        worth_yes=worth.yes,
        worth_no=worth.no,
        lookup_ready=int(lookup_ready),
        linkedin_total=linkedin.total,
        linkedin_pending=linkedin.pending,
        linkedin_done=linkedin.done,
        rejected=int(rejected),
    )


def _review_selection(db: Db) -> ReviewSelection:
    rows = worth_rows(db)
    decisions = sorted(
        ({"person_id": row.key, "decision": row.effective} for row in rows),
        key=lambda row: row["person_id"],
    )
    revision = max(
        (row.human.updated_at for row in rows if row.human),
        default="",
    )
    return ReviewSelection(
        fingerprint=hashlib.sha256(json.dumps(decisions, separators=(",", ":")).encode()).hexdigest(),
        total=len(decisions),
        yes=sum(row["decision"] == "yes" for row in decisions),
        maybe=sum(row["decision"] == "maybe" for row in decisions),
        no=sum(row["decision"] == "no" for row in decisions),
        review_revision=revision,
    )


def workflow_state(db: Db, *, enrichment_running: bool = False) -> WorkflowState:
    """Apply the four queue predicates and return one deterministic state token."""
    progress = _stage_progress(db)
    selection = _review_selection(db)
    enrichment_pending = len(
        enrichment_queue(
            db,
            include_plausibly_absent=True,
            include_candidates=True,
        )
    )
    rules = (
        (bool(progress.worth_pending), "review_people"),
        (bool(enrichment_pending), "enrich"),
        (bool(progress.linkedin_pending), "review_linkedin"),
        (True, "realize"),
    )
    action = next(action for matched, action in rules if matched)
    token = hashlib.sha256(
        json.dumps(
            {
                "progress": asdict(progress),
                "selection": asdict(selection),
                "enrichment_pending": enrichment_pending,
                "enrichment_running": enrichment_running,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    return WorkflowState(
        primitive="deep_context_review_status",
        status="ok",
        next_action=action,
        progress=progress,
        selection=selection,
        state_token=token,
    )
