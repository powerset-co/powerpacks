"""Queue-derived Deep Context workflow state.

Changelog:
- 2026-10-01: synthesis runs straight into enrichment without a worth-review stop;
  the progress names what enrichment still has to do, step by step.
- 2026-10-01: the enrich command's run record decides when enrichment is finished: what a
  completed run could not finish no longer holds the flow, and an unfinished run does.
- 2026-10-01: the same for synthesis: parents its latest run could not write facts for no longer
  hold the flow on `synthesize`.
- 2026-09-25: a parent with a collected source bundle and no facts queues
  `synthesize`, ahead of every review queue.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from packs.ingestion.primitives.deep_context.db._view_sql import (
    WORTH_CTE,
    WORTH_GATE_ACCEPTED,
    WORTH_GATE_REJECTED,
)
from packs.ingestion.primitives.deep_context.db.identity_views import (
    lookups_pending,
    unassembled_research,
    workflow_identity_counts,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import LinkedInProgress, WorthCounts
from packs.ingestion.primitives.deep_context.db.models import (
    ENRICH_RUN_KEY,
    PARENT_WORTH_PREFIX,
    SYNTHESIS_RUN_KEY,
    EnrichmentWork,
    EnrichRun,
    EnrichRunStatus,
    SynthesisRun,
)


@dataclass(frozen=True)
class StageProgress:
    total: int
    synthesize_pending: int
    worth_total: int
    worth_pending: int
    worth_yes: int
    worth_no: int
    lookup_ready: int
    linkedin_total: int
    linkedin_pending: int
    linkedin_done: int
    rejected: int
    # What enrichment still has to do, step by step.
    lookups_pending: int
    judgments_pending: int
    questions_pending: int
    synthetic_pending: int
    # The part of it that the latest completed run has not already tried.
    enrichment_pending: int
    # The step an unfinished run is on; "" when no run is unfinished.
    enrichment_step: str


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


def synthesis_pending(db: Db) -> tuple[str, ...]:
    """Parents with collected messages and no facts yet."""
    return tuple(row["parent_id"] for row in db.query(
        """
SELECT DISTINCT a.parent_id FROM artifacts a
WHERE a.kind='source_bundle' AND a.status='projected'
  AND NOT EXISTS(SELECT 1 FROM facts f WHERE f.parent_id=a.parent_id)
ORDER BY a.parent_id
"""
    ))


def _stage_progress(db: Db, *, worth: WorthCounts) -> StageProgress:
    # What the latest synthesis run tried and could not finish waits for the next run.
    runs = db.query("SELECT value FROM meta WHERE key=?", (SYNTHESIS_RUN_KEY,))
    tried = set(SynthesisRun.from_json(runs[0]["value"]).unfinished) if runs else set()
    synthesize_pending = len(set(synthesis_pending(db)) - tried)
    linkedin, work = _enrichment_work(db)
    run = _enrich_run(db)
    completed = run is not None and run.status == EnrichRunStatus.COMPLETED
    untried = work.without(run.unfinished) if completed else work
    total = db.query("SELECT count(*) AS n FROM parents")[0]["n"]
    counts = db.query(
        WORTH_CTE
        + f""", linkedin_csv_parents AS (
  SELECT DISTINCT pe.parent_id FROM people pe JOIN person_sources ps USING(person_id)
  WHERE ps.source='linkedin_csv'
), kept_parents AS (
  SELECT DISTINCT parent_id FROM links
  WHERE decision_approved='yes' AND decision_action NOT IN ('detach', 'exclude')
)
SELECT (
SELECT count(*) FROM worth w
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
) AS lookup_ready, (
SELECT count(DISTINCT parent_id) FROM (
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
) AS rejected
"""
    )[0]
    return StageProgress(
        total=int(total),
        synthesize_pending=int(synthesize_pending),
        worth_total=worth.total,
        worth_pending=worth.pending,
        worth_yes=worth.yes,
        worth_no=worth.no,
        lookup_ready=int(counts["lookup_ready"]),
        linkedin_total=linkedin.total,
        linkedin_pending=linkedin.pending,
        linkedin_done=linkedin.done,
        rejected=int(counts["rejected"]),
        lookups_pending=len(work.lookups),
        judgments_pending=len(work.judgments),
        questions_pending=len(work.questions),
        synthetic_pending=len(work.synthetic),
        enrichment_pending=untried.count(),
        enrichment_step="" if run is None or completed else run.step,
    )


def _enrichment_work(db: Db) -> tuple[LinkedInProgress, EnrichmentWork]:
    linkedin, questions, judgments = workflow_identity_counts(db)
    return linkedin, EnrichmentWork(lookups_pending(db), judgments, questions, unassembled_research(db))


def enrichment_work(db: Db) -> EnrichmentWork:
    """What enrichment has left to do, by step."""
    return _enrichment_work(db)[1]


def _enrich_run(db: Db) -> EnrichRun | None:
    rows = db.query("SELECT value FROM meta WHERE key=?", (ENRICH_RUN_KEY,))
    return EnrichRun.from_json(rows[0]["value"]) if rows else None


def _review_selection(db: Db) -> tuple[ReviewSelection, WorthCounts]:
    rows = db.query(WORTH_CTE + """
SELECT w.parent_id, w.effective_worth, w.human_worth, w.human_worth_at, w.has_synthetic
FROM worth w
""")
    decisions = sorted(
        ({"person_id": f"{PARENT_WORTH_PREFIX}{row['parent_id']}", "decision": row["effective_worth"]} for row in rows),
        key=lambda row: row["person_id"],
    )
    revision = max(
        (row["human_worth_at"] or "" for row in rows if row["human_worth"]),
        default="",
    )
    selection = ReviewSelection(
        fingerprint=hashlib.sha256(json.dumps(decisions, separators=(",", ":")).encode()).hexdigest(),
        total=len(decisions),
        yes=sum(row["decision"] == "yes" for row in decisions),
        maybe=sum(row["decision"] == "maybe" for row in decisions),
        no=sum(row["decision"] == "no" for row in decisions),
        review_revision=revision,
    )

    return selection, WorthCounts(
        selection.total,
        sum(row["effective_worth"] == "maybe" and not row["has_synthetic"] for row in rows),
        selection.yes,
        selection.no,
    )


def workflow_state(db: Db, *, enrichment_running: bool = False) -> WorkflowState:
    """Apply the ordered queue predicates and return one deterministic state token."""
    selection, worth = _review_selection(db)
    progress = _stage_progress(db, worth=worth)
    enrichment_pending = progress.enrichment_pending
    rules = (
        (bool(progress.synthesize_pending), "synthesize"),
        (bool(enrichment_pending or progress.enrichment_step), "enrich"),
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
