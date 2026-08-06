"""Named SQLite reads for the Deep Context review product.

The web application hydrates these query results.  It never scans the legacy
CSV or enrichment directories to decide what is pending.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from packs.ingestion.primitives.deep_context.db.models import PARENT_WORTH_PREFIX
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError


_EMPTY_COUNTS = {"total": 0, "completed": 0, "pending": 0, "failed": 0}
_STAGES = ("worth", "enrich", "linkedin")


_WORTH_CTE = """
WITH ranked_facts AS (
  SELECT f.*,
         row_number() OVER (
           PARTITION BY f.parent_id
           ORDER BY CASE f.machine_worth WHEN 'yes' THEN 2 WHEN 'no' THEN 0 ELSE 1 END DESC,
                    COALESCE(f.person_id, f.subject_key)
         ) AS worth_rank
  FROM facts f
), worth AS (
  SELECT p.parent_id, p.public_identifier, p.display_name, p.display_slug,
         p.human_worth, p.human_worth_note, p.human_worth_source, p.human_worth_at,
         COALESCE(r.machine_worth, 'maybe') AS machine_worth,
         COALESCE(r.machine_worth_reason, '') AS machine_worth_reason,
         CASE WHEN r.machine_worth IS NULL THEN 'default' ELSE 'llm' END AS machine_source,
         COALESCE(p.human_worth, r.machine_worth, 'maybe') AS effective_worth,
         (SELECT json_group_array(person_id) FROM (
            SELECT person_id FROM people WHERE parent_id=p.parent_id ORDER BY person_id
          )) AS person_ids_json,
         EXISTS(SELECT 1 FROM links l WHERE l.parent_id=p.parent_id AND l.kind='synthetic')
           AS has_synthetic
  FROM parents p
  JOIN ranked_facts r ON r.parent_id=p.parent_id AND r.worth_rank=1
  WHERE NOT EXISTS (
          SELECT 1 FROM facts f WHERE f.parent_id=p.parent_id AND f.is_owner=1
        )
    AND NOT EXISTS (
          SELECT 1 FROM people pe WHERE pe.parent_id=p.parent_id AND pe.is_owner=1
        )
    AND EXISTS (
          SELECT 1 FROM people pe WHERE pe.parent_id=p.parent_id AND pe.is_ghost=0
        )
)
"""


_WORTH_SELECT = """
SELECT * FROM worth
{where}
ORDER BY lower(COALESCE(display_name, public_identifier)), parent_id
"""


def _json(value: object, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return fallback
    return parsed


def _worth_dict(row: Any) -> dict[str, Any]:
    human = None
    if row["human_worth"]:
        human = {
            "decision": row["human_worth"],
            "updated_at": row["human_worth_at"] or "",
            "note": row["human_worth_note"] or "",
        }
    return {
        "key": f"{PARENT_WORTH_PREFIX}{row['parent_id']}",
        "parent_id": row["parent_id"],
        "parent_slug": row["display_slug"] or row["public_identifier"],
        "person_ids": _json(row["person_ids_json"], []),
        "name": row["display_name"] or row["public_identifier"],
        "machine": {
            "decision": row["machine_worth"],
            "reason": row["machine_worth_reason"],
            "source": row["machine_source"],
        },
        "human": human,
        "effective": row["effective_worth"],
        "source": "user" if human else row["machine_source"],
    }


def _worth_rows(db: Db) -> list[dict[str, Any]]:
    """All facts-backed, non-owner, non-ghost canonical people."""
    rows = db._query(_WORTH_CTE + _WORTH_SELECT.format(where=""))
    return [_worth_dict(row) for row in rows]


def _worth_queue(db: Db) -> list[dict[str, Any]]:
    """The effective-Maybe queue; researched synthetic people do not re-enter."""
    rows = db._query(
        _WORTH_CTE + _WORTH_SELECT.format(
            where="WHERE effective_worth='maybe' AND has_synthetic=0"
        )
    )
    return [_worth_dict(row) for row in rows]


def _worth_counts(db: Db) -> dict[str, int]:
    """Worth-stage counts from the same relation and queue predicate as the cards."""
    row = db._query(
        _WORTH_CTE
        + """
SELECT count(*) AS total,
       sum(effective_worth='maybe' AND has_synthetic=0) AS pending,
       sum(effective_worth='yes') AS yes,
       sum(effective_worth='no') AS no
FROM worth
"""
    )[0]
    return {key: int(row[key] or 0) for key in ("total", "pending", "yes", "no")}


_PENDING_CANDIDATE = """
(
  l.raw_import=0
  AND (
  (l.kind='synthetic' AND COALESCE(l.decision_approved, '') NOT IN ('yes', 'no'))
  OR
  (l.kind!='synthetic'
   AND (l.paid_profile=1 OR l.candidate_origin=1)
   AND l.decision_action IS NULL
   AND COALESCE(l.machine_approved, '') NOT IN ('auto', 'yes', 'no')
   AND l.authoritative_detach=0
   AND NOT (
     l.candidate_origin=1
     AND l.machine_action='retarget'
     AND l.machine_proposed_url IS NOT NULL
     AND lower(COALESCE(l.machine_reject, '')) NOT IN ('1', 'true', 'yes')
   ))
  )
)
"""


_LINKEDIN_CTE = (
    _WORTH_CTE
    + """, candidate_policy AS (
  SELECT l.*,
         """
    + _PENDING_CANDIDATE
    + """ AS is_pending
  FROM links l
), identity_scope AS (
  SELECT p.parent_id
  FROM parents p
  LEFT JOIN worth w USING(parent_id)
  WHERE COALESCE(w.effective_worth, p.human_worth, p.machine_worth, 'maybe')!='no'
    AND NOT EXISTS (
      SELECT 1 FROM links raw WHERE raw.parent_id=p.parent_id AND raw.raw_import=1
    )
    AND NOT (
      NOT EXISTS (
        SELECT 1 FROM links real WHERE real.parent_id=p.parent_id AND real.kind!='synthetic'
      )
      AND EXISTS (
        SELECT 1 FROM links rejected
        WHERE rejected.parent_id=p.parent_id AND rejected.kind='synthetic'
          AND rejected.decision_action='detach'
          AND rejected.decision_approved IN ('yes', 'no')
      )
    )
    AND EXISTS (
      SELECT 1 FROM candidate_policy c
      WHERE c.parent_id=p.parent_id
        AND c.raw_import=0
        AND (c.paid_profile=1 OR c.candidate_origin=1 OR c.kind='synthetic')
        AND (c.candidate_origin=1 OR c.kind='synthetic' OR c.is_pending=1
             OR c.decision_action IS NOT NULL
             OR COALESCE(c.decision_approved, '') IN ('yes', 'no')
             OR EXISTS (
               SELECT 1 FROM people origin
               WHERE origin.parent_id=p.parent_id
                 AND origin.person_id LIKE 'candidate:%'
             ))
    )
), pending_parents AS (
  SELECT DISTINCT c.parent_id
  FROM candidate_policy c JOIN identity_scope s USING(parent_id)
  WHERE c.is_pending=1
)
"""
)


_PARENT_SELECT = """
SELECT p.parent_id, p.public_identifier, p.display_name, p.display_slug,
       COALESCE(w.machine_worth, p.machine_worth, 'maybe') AS machine_worth,
       COALESCE(w.machine_worth_reason, p.machine_worth_reason, '') AS machine_worth_reason,
       CASE WHEN w.machine_source IS NOT NULL THEN w.machine_source
            WHEN p.machine_worth IS NOT NULL THEN 'llm' ELSE 'default' END AS machine_source,
       COALESCE(w.effective_worth, p.human_worth, p.machine_worth, 'maybe') AS effective_worth,
       p.human_worth, p.human_worth_note, p.human_worth_at,
       COALESCE(w.person_ids_json, (SELECT json_group_array(person_id) FROM (
         SELECT person_id FROM people WHERE parent_id=p.parent_id ORDER BY person_id
       ))) AS person_ids_json,
       (SELECT json_group_array(source) FROM (
         SELECT DISTINCT ps.source FROM people pe JOIN person_sources ps USING(person_id)
         WHERE pe.parent_id=p.parent_id ORDER BY ps.source
       )) AS sources_json,
       a.path AS dossier_path
FROM parents p
LEFT JOIN worth w USING(parent_id)
LEFT JOIN artifacts a ON a.artifact_key=(
  SELECT a2.artifact_key FROM artifacts a2
  WHERE a2.parent_id=p.parent_id AND a2.kind='dossier' AND a2.status='projected'
  ORDER BY a2.projected_at DESC, a2.artifact_key LIMIT 1
)
{where}
ORDER BY lower(COALESCE(p.display_name, p.public_identifier)), p.parent_id
"""


_CANDIDATE_SELECT = """
SELECT c.*,
       sp.profile_json AS synthetic_profile_json,
       r.result_json AS research_json,
       (SELECT json_group_array(value) FROM (
          SELECT DISTINCT COALESCE(pi.display_value, pi.normalized_value) AS value
          FROM candidate_people cp JOIN person_identifiers pi USING(person_id)
          WHERE cp.row_key=c.row_key AND pi.kind='email' ORDER BY value
        )) AS emails_json,
       (SELECT json_group_array(value) FROM (
          SELECT DISTINCT COALESCE(pi.display_value, pi.normalized_value) AS value
          FROM candidate_people cp JOIN person_identifiers pi USING(person_id)
          WHERE cp.row_key=c.row_key AND pi.kind='phone' ORDER BY value
        )) AS phones_json
FROM candidate_policy c
LEFT JOIN synthetic_profiles sp ON sp.candidate_key=c.row_key
LEFT JOIN research r ON r.candidate_key=c.row_key AND r.handle=(
  SELECT r2.handle FROM research r2 WHERE r2.candidate_key=c.row_key
  ORDER BY r2.updated_at DESC, r2.handle LIMIT 1
)
WHERE c.parent_id IN ({parent_placeholders})
{pending}
ORDER BY c.parent_id, c.is_pending DESC, c.row_key
"""


def _profile_view(*payloads: object) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for candidate in payloads:
        parsed = _json(candidate, {})
        if isinstance(parsed, dict) and parsed:
            payload = parsed
            break
    linkedin = payload.get("linkedin") if isinstance(payload.get("linkedin"), dict) else {}
    person = payload.get("person") if isinstance(payload.get("person"), dict) else {}
    social = payload.get("social") if isinstance(payload.get("social"), dict) else {}
    verdict = payload.get("verdict") if isinstance(payload.get("verdict"), dict) else {}
    full_name = (
        payload.get("full_name")
        or linkedin.get("full_name")
        or person.get("full_name")
        or " ".join(filter(None, (payload.get("first_name"), payload.get("last_name"))))
    )
    return {
        "full_name": str(full_name or ""),
        "headline": str(payload.get("headline") or linkedin.get("headline") or ""),
        "profile_pic_url": str(
            payload.get("profile_pic_url") or linkedin.get("profile_pic_url") or ""
        ),
        "experiences": payload.get("experiences") or linkedin.get("experiences") or [],
        "education": payload.get("education") or linkedin.get("education") or [],
        "location": payload.get("location") or linkedin.get("location") or "",
        "supporting": verdict.get("supporting_evidence") or [],
        "contradicting": verdict.get("contradicting_evidence") or [],
        "plausibly_absent": bool(verdict.get("linkedin_plausibly_absent")),
        "recommend_dr": bool(verdict.get("recommend_deep_research")),
        "linkedin_url": str(
            payload.get("linkedin_url") or linkedin.get("linkedin_url")
            or social.get("linkedin_url") or ""
        ),
    }


def _candidate_dict(row: Any) -> dict[str, Any]:
    profile = _profile_view(
        row["synthetic_profile_json"], row["research_json"], row["judgment_payload_json"]
    )
    proposed = row["machine_action"] == "retarget" and row["machine_proposed_url"]
    decided_retarget = row["decision_action"] == "retarget" and row["replacement_url"]
    url = row["replacement_url"] if decided_retarget else (
        row["machine_proposed_url"] if proposed else row["linkedin_url"]
    )
    pub = row["replacement_public_identifier"] if decided_retarget else (
        row["machine_proposed_public_identifier"] if proposed else row["public_identifier"]
    )
    action = row["decision_action"] or row["machine_action"] or ""
    approved = row["decision_approved"] or row["machine_approved"] or ""
    return {
        "pub": row["public_identifier"],
        "row_key": row["row_key"],
        "profile_pub": pub or row["public_identifier"],
        "url": url or profile["linkedin_url"],
        "full_name": row["display_name"] or profile["full_name"],
        "headline": profile["headline"],
        "profile_pic_url": profile["profile_pic_url"],
        "experiences": profile["experiences"],
        "education": profile["education"],
        "location": profile["location"],
        "has_profile": bool(url or profile["linkedin_url"]),
        "verdict": row["machine_judgment"] or "",
        "confidence": float(row["machine_confidence"] or 0.0),
        "supporting": profile["supporting"],
        "contradicting": profile["contradicting"],
        "reason": row["machine_reason"] or "",
        "plausibly_absent": profile["plausibly_absent"],
        "recommend_dr": profile["recommend_dr"],
        "match_emails": _json(row["emails_json"], []),
        "match_phones": _json(row["phones_json"], []),
        "conflict": False,
        "import_candidate": bool(row["raw_import"]),
        "candidate_origin": bool(row["candidate_origin"]),
        "synthetic": row["kind"] == "synthetic",
        "action": action,
        "approved": approved,
        "new_url": row["replacement_url"] or row["machine_proposed_url"] or "",
        "new_public_identifier": (
            row["replacement_public_identifier"]
            or row["machine_proposed_public_identifier"]
            or ""
        ),
        "llm_reject": row["machine_reject"] or "",
        "llm_reject_confidence": row["machine_reject_confidence"],
        "llm_reject_reason": row["machine_reject_reason"] or "",
        "pending": bool(row["is_pending"]),
    }


def _parent_dict(row: Any) -> dict[str, Any]:
    worth = {
        "key": f"{PARENT_WORTH_PREFIX}{row['parent_id']}",
        "parent_id": row["parent_id"],
        "parent_slug": row["display_slug"] or row["public_identifier"],
        "person_ids": _json(row["person_ids_json"], []),
        "name": row["display_name"] or row["public_identifier"],
        "machine": {
            "decision": row["machine_worth"],
            "reason": row["machine_worth_reason"],
            "source": row["machine_source"],
        },
        "human": ({"decision": row["human_worth"], "updated_at": row["human_worth_at"] or ""}
                  if row["human_worth"] else None),
        "effective": row["effective_worth"],
        "source": "user" if row["human_worth"] else row["machine_source"],
    }
    slug = row["display_slug"] or row["public_identifier"]
    source_channels = _json(row["sources_json"], [])
    labels = {"gmail_msgvault": "gmail", "imessage": "imessage", "whatsapp": "whatsapp"}
    sources = [labels[value] for value in source_channels if value in labels]
    return {
        "parent_id": row["parent_id"],
        "slug": slug,
        "dossier_slug": slug,
        "dossier_path": row["dossier_path"],
        "name": row["display_name"] or row["public_identifier"],
        "person_ids": worth["person_ids"],
        "sources": sources,
        "source_channels": source_channels,
        "worth_row": worth,
        "worth": {"decision": worth["effective"], "source": worth["source"]},
        "machine_worth": worth["machine"],
        "candidates": [],
    }


def _hydrate_parents(db: Db, parent_rows: list[Any], *, pending_only: bool) -> list[dict[str, Any]]:
    parents = [_parent_dict(row) for row in parent_rows]
    if not parents:
        return []
    by_id = {parent["parent_id"]: parent for parent in parents}
    placeholders = ",".join("?" for _ in parents)
    sql = _LINKEDIN_CTE + _CANDIDATE_SELECT.format(
        parent_placeholders=placeholders,
        pending="AND c.is_pending=1" if pending_only else "",
    )
    for row in db._query(sql, tuple(by_id)):
        by_id[row["parent_id"]]["candidates"].append(_candidate_dict(row))
    return parents


def _all_parents(db: Db) -> list[dict[str, Any]]:
    """Web-ready review/directory model, including completed and rejected parents."""
    rows = db._query(
        _LINKEDIN_CTE
        + _PARENT_SELECT.format(
            where="""WHERE NOT EXISTS (
              SELECT 1 FROM people pe WHERE pe.parent_id=p.parent_id AND pe.is_owner=1
            ) AND EXISTS (
              SELECT 1 FROM links l WHERE l.parent_id=p.parent_id
                AND (l.paid_profile=1 OR l.candidate_origin=1 OR l.kind='synthetic')
            )"""
        )
    )
    return _hydrate_parents(db, rows, pending_only=False)


def _linkedin_queue(db: Db) -> list[dict[str, Any]]:
    """One stable card per pending parent, carrying all of its pending candidates."""
    rows = db._query(
        _LINKEDIN_CTE
        + _PARENT_SELECT.format(where="WHERE p.parent_id IN (SELECT parent_id FROM pending_parents)")
    )
    return _hydrate_parents(db, rows, pending_only=True)


def _linkedin_progress(db: Db) -> dict[str, int]:
    row = db._query(
        _LINKEDIN_CTE
        + """
SELECT (SELECT count(*) FROM identity_scope) AS total,
       (SELECT count(*) FROM pending_parents) AS pending
"""
    )[0]
    total, pending = int(row["total"]), int(row["pending"])
    return {"total": total, "pending": pending, "done": total - pending}


def _stage_progress(db: Db) -> dict[str, int]:
    worth = _worth_counts(db)
    linkedin = _linkedin_progress(db)
    # A stale child mentioned by a synthetic can belong to a second canonical
    # parent. Its fact-only fallback is not another lookup; a materialized link
    # or research result is the durable subject marker.
    lookup_ready = db._query(
        _WORTH_CTE
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
    total = db._query("SELECT count(*) AS n FROM parents")[0]["n"]
    rejected = db._query(
        _WORTH_CTE
        + """
SELECT count(DISTINCT parent_id) AS n FROM (
  SELECT w.parent_id FROM worth w
  WHERE w.effective_worth='no'
    AND (
      w.human_worth='no'
      OR (
        w.human_worth IS NULL
        AND NOT EXISTS (
          SELECT 1 FROM people pe JOIN person_sources ps USING(person_id)
          WHERE pe.parent_id=w.parent_id AND ps.source='linkedin_csv'
        )
        AND NOT EXISTS (
          SELECT 1 FROM links kept
          WHERE kept.parent_id=w.parent_id AND kept.decision_approved='yes'
            AND kept.decision_action NOT IN ('detach', 'exclude')
        )
      )
    )
  UNION ALL
  SELECT parent_id FROM links
  WHERE decision_action='exclude' AND decision_approved IN ('auto', 'yes')
  UNION ALL
  SELECT p.parent_id FROM parents p
  WHERE p.machine_worth='no'
    AND NOT EXISTS(SELECT 1 FROM worth w WHERE w.parent_id=p.parent_id)
    AND NOT EXISTS (
      SELECT 1 FROM people pe JOIN person_sources ps USING(person_id)
      WHERE pe.parent_id=p.parent_id AND ps.source='linkedin_csv'
    )
    AND NOT EXISTS (
      SELECT 1 FROM links kept
      WHERE kept.parent_id=p.parent_id AND kept.decision_approved='yes'
        AND kept.decision_action NOT IN ('detach', 'exclude')
    )
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


def _stage_states(db: Db) -> dict[str, dict[str, Any]]:
    return {row["stage"]: dict(row) for row in db._query(
        "SELECT * FROM stage_state ORDER BY stage"
    )}


def _review_selection(db: Db) -> dict[str, Any]:
    """Frozen worth decision selection consumed by enrichment and workflow state."""
    decisions = sorted(
        ({"person_id": row["key"], "decision": row["effective"]} for row in _worth_rows(db)),
        key=lambda row: row["person_id"],
    )
    revision = str((_stage_states(db).get("worth") or {}).get("updated_at") or "")
    return {
        "sha256": hashlib.sha256(
            json.dumps(decisions, separators=(",", ":")).encode()
        ).hexdigest(),
        "total": len(decisions),
        **{
            value: sum(row["decision"] == value for row in decisions)
            for value in ("yes", "maybe", "no")
        },
        "review_revision": revision,
    }


def _stage_counts(progress: dict[str, int], stage: str) -> dict[str, int]:
    fields = {
        "worth": {
            "total": "worth_total",
            "yes": "worth_yes",
            "no": "worth_no",
            "pending": "worth_pending",
            "ready_for_lookup": "lookup_ready",
        },
        "linkedin": {
            "total": "linkedin_total",
            "yes_or_no": "linkedin_done",
            "pending": "linkedin_pending",
        },
    }
    if stage == "enrich":
        return dict(_EMPTY_COUNTS)
    if stage not in fields:
        raise StoreError(f"unknown review stage: {stage}")
    return {name: progress[source] for name, source in fields[stage].items()}


def _review_state(db: Db, preferred_stage: str | None = None) -> dict[str, Any]:
    """Pathless compatibility manifest derived only from typed stage rows."""
    progress, states = _stage_progress(db), _stage_states(db)
    completed = [
        stage for stage in _STAGES
        if (states.get(stage) or {}).get("status") == "complete"
    ]
    stage = preferred_stage or next(
        (name for name in reversed(_STAGES) if name in states), ""
    )
    row = states.get(stage) or {}
    payload = {
        "stage": stage,
        "status": "completed" if row.get("status") == "complete" else "awaiting_user",
        "counts": _stage_counts(progress, stage) if stage else {},
        "completed_stages": completed,
        "people_revision": str((states.get("worth") or {}).get("updated_at") or ""),
        "updated_at": row.get("updated_at"),
    }
    if row.get("completed_at"):
        payload["completed_at"] = row["completed_at"]
    return payload


def _enrichment_state(db: Db) -> dict[str, Any]:
    """Compatibility-ready enrichment state with one freshness policy."""
    stages = _stage_states(db)
    stage = stages.get("enrich") or stages.get("enrichment")
    jobs = db._query(
        "SELECT * FROM jobs WHERE kind='enrichment' ORDER BY finished_at DESC, name LIMIT 1"
    )
    job = dict(jobs[0]) if jobs else None
    approval_rows = db._query(
        "SELECT * FROM spend_approvals WHERE stage IN ('enrich', 'enrichment') "
        "ORDER BY approved_at DESC LIMIT 1"
    )
    approval = dict(approval_rows[0]) if approval_rows else None
    stored_fingerprint = (stage or {}).get("selection_fingerprint") or (
        job or {}
    ).get("selection_fingerprint")
    result = _json((job or {}).get("result_json"), {})
    result = result if isinstance(result, dict) else {}
    current_selection = _review_selection(db)
    recorded = result.get("selection") if isinstance(result.get("selection"), dict) else {}
    fingerprint = str(recorded.get("sha256") or stored_fingerprint or "")
    revision = str(recorded.get("review_revision") or "")

    if not stage and not job:
        return {
            "stage": "enrich",
            "status": "not_started",
            "counts": dict(_EMPTY_COUNTS),
            "selection": {"sha256": fingerprint, "review_revision": revision},
            "current": False,
            "approval_current": False,
            "state": "free_pending",
        }

    current = fingerprint == current_selection["sha256"] and (
        not revision or revision == current_selection["review_revision"]
    )
    status = str(
        result.get("status") or (stage or {}).get("status") or (job or {}).get("status") or "pending"
    )
    status = {
        "pending": "not_started",
        "complete": "completed",
        "reused": "completed",
        "applied": "completed",
    }.get(status, status)
    if not current:
        status = "stale"
    approval_current = bool(
        current
        and approval
        and approval.get("selection_fingerprint") == fingerprint
        and status == "needs_approval"
    )
    payload = {
        **result,
        "stage": "enrich",
        "status": status,
        "counts": dict(result.get("counts") or _EMPTY_COUNTS),
        "selection": recorded or {"sha256": fingerprint, "review_revision": revision},
        "current": current,
        "approval_current": approval_current,
    }
    if approval:
        payload["approval"] = {
            "status": "approved",
            "approved_at": approval.get("approved_at"),
            "approved_budget_usd": approval.get("approved_amount"),
            "estimated_usd": result.get("estimated_usd"),
            "would_submit": approval.get("approved_count"),
            "selection_sha256": fingerprint,
            "review_revision": revision,
        }
    payload["state"] = (
        "running"
        if status in {"running", "submitted", "research_complete"}
        else "needs_approval"
        if status == "needs_approval"
        else "done"
        if status == "completed" and current
        else "free_pending"
    )
    if status == "needs_approval":
        payload["approvable"] = current and int(payload.get("would_submit") or 0) > 0
    return payload


def workflow_state(
    db: Db, *, job_running: bool = False, preferred_stage: str | None = None,
) -> dict[str, Any]:
    """Full pathless workflow status and its deterministic browser state token."""
    progress = _stage_progress(db)
    selection = _review_selection(db)
    enrichment = _enrichment_state(db)
    manifest = _review_state(db, preferred_stage)
    complete, status = set(manifest["completed_stages"]), enrichment["status"]
    rules = (
        ("worth" not in complete, "review_people"),
        (status in {"not_started", "stale"}, "preview_enrichment"),
        (
            status == "needs_approval" and not int(enrichment.get("would_submit") or 0),
            "run_enrichment_from_cache",
        ),
        (
            status == "needs_approval" and enrichment.get("approval_current"),
            "run_approved_enrichment",
        ),
        (status == "needs_approval", "await_enrichment_approval"),
        (status in {"running", "submitted"}, "wait_for_enrichment"),
        (status in {"failed", "completed_with_errors"}, "retry_enrichment"),
        (status == "research_complete", "assemble_synthetic"),
        (status != "completed", "wait_for_enrichment"),
        ("enrich" not in complete, "continue_enrichment"),
        (bool(progress["linkedin_pending"]), "review_linkedin"),
        ("linkedin" not in complete, "finish_linkedin"),
        (True, "realize"),
    )
    action = next(action for matched, action in rules if matched)
    token = hashlib.sha256(json.dumps(
        {
            "progress": progress,
            "selection": selection,
            "enrichment": enrichment,
            "review": manifest,
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
        "review_manifest": manifest,
        "enrichment": enrichment,
        "state_token": token,
    }


def retarget_snapshot(db: Db) -> dict[str, list[dict[str, Any]]]:
    """Guided-retarget requests and their durable job rows."""
    guidance = []
    for row in db._query("SELECT * FROM guidance ORDER BY submitted_at, handle"):
        item = dict(row)
        item["detail"] = _json(item.pop("detail_json"), {})
        guidance.append(item)
    jobs = []
    for row in db._query(
        "SELECT * FROM jobs WHERE kind='guided_retarget' ORDER BY started_at, name"
    ):
        item = dict(row)
        item["result"] = _json(item.pop("result_json"), {})
        jobs.append(item)
    return {"guidance": guidance, "jobs": jobs}


def _artifact_path(
    db: Db, kind: str, *, parent_id: str | None = None,
    person_id: str | None = None, candidate_key: str | None = None,
) -> str | None:
    """Latest exact projected artifact path for one typed owner."""
    clauses, params = ["kind=?", "status='projected'"], [kind]
    for column, value in (
        ("parent_id", parent_id), ("person_id", person_id), ("candidate_key", candidate_key)
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    rows = db._query(
        f"SELECT path FROM artifacts WHERE {' AND '.join(clauses)} "
        "ORDER BY projected_at DESC, artifact_key LIMIT 1",
        tuple(params),
    )
    return str(rows[0]["path"]) if rows else None


def dossier_path(db: Db, slug_or_parent_id: str) -> str | None:
    rows = db._query(
        "SELECT parent_id FROM parents WHERE parent_id=? OR display_slug=? "
        "OR public_identifier=? LIMIT 1",
        (slug_or_parent_id, slug_or_parent_id, slug_or_parent_id),
    )
    return _artifact_path(db, "dossier", parent_id=rows[0]["parent_id"]) if rows else None


def avatar_path(db: Db, public_identifier: str) -> str | None:
    """The explicitly projected cached image; transport sniffs its content type."""
    rows = db._query(
        "SELECT row_key FROM links WHERE public_identifier=? "
        "OR machine_proposed_public_identifier=? OR replacement_public_identifier=? LIMIT 1",
        (public_identifier, public_identifier, public_identifier),
    )
    if not rows:
        return None
    return _artifact_path(db, "avatar", candidate_key=rows[0]["row_key"])


def _siblings_of(db: Db, candidate_key: str) -> list[str]:
    """All real, synthetic, and ghost candidate keys for the clicked parent."""
    rows = db._query(
        "SELECT row_key FROM links WHERE parent_id=("
        "SELECT parent_id FROM links WHERE row_key=?"
        ") ORDER BY row_key",
        (candidate_key,),
    )
    return [row["row_key"] for row in rows]


def directory(db: Db) -> list[dict[str, str]]:
    """The directory sidebar projection, sorted A-Z with effective worth."""
    return [
        {
            "slug": row["parent_slug"],
            "name": row["name"],
            "worth": row["effective"],
        }
        for row in _worth_rows(db)
    ]


def person_detail(db: Db, slug_or_parent_id: str) -> dict[str, Any] | None:
    """One SQL-hydrated parent; artifact paths may be opened by the response adapter."""
    rows = db._query(
        _WORTH_CTE
        + _PARENT_SELECT.format(
            where="WHERE p.parent_id=? OR p.display_slug=? OR p.public_identifier=?"
        ),
        (slug_or_parent_id, slug_or_parent_id, slug_or_parent_id),
    )
    hydrated = _hydrate_parents(db, rows[:1], pending_only=False)
    return hydrated[0] if hydrated else None


def worth_review(
    db: Db, scope: Literal["rows", "queue", "counts"],
) -> list[dict[str, Any]] | dict[str, int]:
    """One explicitly scoped worth-review read from the canonical policy."""
    if scope == "rows":
        return _worth_rows(db)
    if scope == "queue":
        return _worth_queue(db)
    if scope == "counts":
        return _worth_counts(db)
    raise StoreError(f"unknown worth review scope: {scope}")


def _enrichment_queue(
    db: Db, *, include_plausibly_absent: bool = False,
    include_candidates: bool = False, confirm_threshold: float = 0.8,
) -> list[dict[str, Any]]:
    """Effective-Yes parents whose identity still needs one paid lookup."""
    rows = db._query(
        _WORTH_CTE
        + """
SELECT l.row_key, l.parent_id, w.display_slug, w.display_name, l.linkedin_url,
       l.machine_reason, l.machine_judgment, l.candidate_origin,
       (SELECT json_group_array(person_id) FROM (
          SELECT person_id FROM people WHERE parent_id=l.parent_id AND is_ghost=0
          ORDER BY person_id
        )) AS person_ids_json,
       (SELECT json_group_array(value) FROM (
          SELECT DISTINCT COALESCE(i.display_value, i.normalized_value) AS value
          FROM people pe JOIN person_identifiers i USING(person_id)
          WHERE pe.parent_id=l.parent_id AND i.kind='email' ORDER BY value
        )) AS emails_json,
       (SELECT json_group_array(value) FROM (
          SELECT DISTINCT COALESCE(i.display_value, i.normalized_value) AS value
          FROM people pe JOIN person_identifiers i USING(person_id)
          WHERE pe.parent_id=l.parent_id AND i.kind='phone' ORDER BY value
        )) AS phones_json
FROM links l JOIN worth w USING(parent_id)
WHERE w.effective_worth='yes'
  AND EXISTS (SELECT 1 FROM facts f WHERE f.parent_id=l.parent_id)
  AND COALESCE(l.decision_approved, '') NOT IN ('yes', 'no')
  AND COALESCE(l.decision_action, '')!='exclude'
  AND NOT (
    l.machine_action='retarget'
    AND l.machine_proposed_url IS NOT NULL
    AND lower(COALESCE(l.machine_reject, '')) NOT IN ('1', 'true', 'yes')
  )
  AND NOT EXISTS (
    SELECT 1 FROM links kept
    WHERE kept.parent_id=l.parent_id AND kept.row_key!=l.row_key
      AND (
        (kept.machine_judgment='confirmed'
         AND COALESCE(kept.machine_confidence, 0)>=?)
        OR (kept.machine_action='verify'
            AND COALESCE(kept.machine_approved, '') IN ('auto', 'yes'))
        OR (kept.decision_action='verify' AND kept.decision_approved='yes')
      )
  )
  AND (
    (? AND l.candidate_origin=1 AND l.raw_import=1)
    OR (
      l.machine_judgment='wrong_person'
      AND COALESCE(l.machine_confidence, 0)>=?
      AND COALESCE(json_extract(l.judgment_payload_json,
                               '$.recommend_deep_research'), 0)=1
    )
    OR (
      ? AND COALESCE(json_extract(l.judgment_payload_json,
                                  '$.linkedin_plausibly_absent'), 0)=1
    )
  )
ORDER BY lower(COALESCE(w.display_name, w.public_identifier)), l.row_key
""",
        (
            confirm_threshold, int(include_candidates), confirm_threshold,
            int(include_plausibly_absent),
        ),
    )
    return [
        {
            "parent_id": row["parent_id"],
            "parent_slug": row["display_slug"] or row["parent_id"],
            "name": row["display_name"] or row["row_key"],
            "person_ids": _json(row["person_ids_json"], []),
            "candidate_key": row["row_key"],
            "linkedin": {"linkedin_url": row["linkedin_url"] or ""},
            "verdict": {
                "verdict": row["machine_judgment"] or "no_linkedin_candidate",
                "reason": row["machine_reason"] or "",
            },
            "match_emails": _json(row["emails_json"], []),
            "match_phones": _json(row["phones_json"], []),
            "candidate_origin": bool(row["candidate_origin"]),
        }
        for row in rows
    ]


def linkedin_review(
    db: Db, scope: Literal["parents", "queue", "progress", "enrichment"], *,
    include_plausibly_absent: bool = False, include_candidates: bool = False,
    confirm_threshold: float = 0.8,
) -> list[dict[str, Any]] | dict[str, int]:
    """One explicitly scoped LinkedIn-review read from the canonical policy."""
    if scope == "parents":
        return _all_parents(db)
    if scope == "queue":
        return _linkedin_queue(db)
    if scope == "progress":
        return _linkedin_progress(db)
    if scope == "enrichment":
        return _enrichment_queue(
            db,
            include_plausibly_absent=include_plausibly_absent,
            include_candidates=include_candidates,
            confirm_threshold=confirm_threshold,
        )
    raise StoreError(f"unknown LinkedIn review scope: {scope}")
