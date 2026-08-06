"""Queue-derived Deep Context workflow state."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from packs.ingestion.primitives.deep_context.db._view_rows import (
    _linkedin_progress,
    _worth_review,
)
from packs.ingestion.primitives.deep_context.db._view_sql import WORTH_CTE
from packs.ingestion.primitives.deep_context.db.identity_views import _enrichment_queue
from packs.ingestion.primitives.deep_context.db.store import Db


def _stage_progress(db: Db) -> dict[str, int]:
    worth = _worth_review(db, "counts")
    assert isinstance(worth, dict)
    linkedin = _linkedin_progress(db)
    lookup_ready = db.query(
        WORTH_CTE
        + """
SELECT count(*) AS n FROM worth w
WHERE w.effective_worth='yes'
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
        + """, linkedin_csv_parents AS (
  SELECT DISTINCT pe.parent_id FROM people pe JOIN person_sources ps USING(person_id)
  WHERE ps.source='linkedin_csv'
), kept_parents AS (
  SELECT DISTINCT parent_id FROM links
  WHERE decision_approved='yes' AND decision_action NOT IN ('detach', 'exclude')
)
SELECT count(DISTINCT parent_id) AS n FROM (
  SELECT w.parent_id FROM worth w
  WHERE w.effective_worth='no'
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
    return {
        "total": int(total),
        "worth_total": worth["total"],
        "worth_pending": worth["pending"],
        "worth_yes": worth["yes"],
        "worth_no": worth["no"],
        "lookup_ready": int(lookup_ready),
        "linkedin_total": linkedin["total"],
        "linkedin_pending": linkedin["pending"],
        "linkedin_done": linkedin["done"],
        "rejected": int(rejected),
    }


def _review_selection(db: Db) -> dict[str, Any]:
    rows = _worth_review(db, "rows")
    assert isinstance(rows, list)
    decisions = sorted(
        ({"person_id": row["key"], "decision": row["effective"]} for row in rows),
        key=lambda row: row["person_id"],
    )
    revision = max(
        (str((row.get("human") or {}).get("updated_at") or "") for row in rows),
        default="",
    )
    return {
        "fingerprint": hashlib.sha256(
            json.dumps(decisions, separators=(",", ":")).encode()
        ).hexdigest(),
        "total": len(decisions),
        **{
            value: sum(row["decision"] == value for row in decisions)
            for value in ("yes", "maybe", "no")
        },
        "review_revision": revision,
    }


def workflow_state(db: Db, *, job_running: bool = False) -> dict[str, Any]:
    """Apply the four queue predicates and return one deterministic state token."""
    progress = _stage_progress(db)
    selection = _review_selection(db)
    enrichment_pending = len(_enrichment_queue(
        db, include_plausibly_absent=True, include_candidates=True,
    ))
    rules = (
        (bool(progress["worth_pending"]), "review_people"),
        (bool(enrichment_pending), "enrich"),
        (bool(progress["linkedin_pending"]), "review_linkedin"),
        (True, "realize"),
    )
    action = next(action for matched, action in rules if matched)
    token = hashlib.sha256(json.dumps(
        {
            "progress": progress,
            "selection": selection,
            "enrichment_pending": enrichment_pending,
            "job_running": job_running,
        },
        sort_keys=True,
        default=str,
    ).encode()).hexdigest()
    return {
        "primitive": "deep_context_review_status",
        "status": "ok",
        "next_action": action,
        "progress": progress,
        "selection": selection,
        "state_token": token,
    }
