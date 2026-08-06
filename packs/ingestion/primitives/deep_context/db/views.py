"""Named SQLite reads for the Deep Context review product.

The web application hydrates these query results.  It never scans the legacy
CSV or enrichment directories to decide what is pending.
"""
from __future__ import annotations

import json
from typing import Any

from packs.ingestion.primitives.deep_context.db.models import PARENT_WORTH_PREFIX
from packs.ingestion.primitives.deep_context.db.store import Db


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


def worth_rows(db: Db) -> list[dict[str, Any]]:
    """All facts-backed, non-owner, non-ghost canonical people."""
    rows = db.query(_WORTH_CTE + _WORTH_SELECT.format(where=""))
    return [_worth_dict(row) for row in rows]


def worth_queue(db: Db) -> list[dict[str, Any]]:
    """The effective-Maybe queue; researched synthetic people do not re-enter."""
    rows = db.query(
        _WORTH_CTE + _WORTH_SELECT.format(
            where="WHERE effective_worth='maybe' AND has_synthetic=0"
        )
    )
    return [_worth_dict(row) for row in rows]


def worth_counts(db: Db) -> dict[str, int]:
    """Worth-stage counts from the same relation and queue predicate as the cards."""
    row = db.query(
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
    for row in db.query(sql, tuple(by_id)):
        by_id[row["parent_id"]]["candidates"].append(_candidate_dict(row))
    return parents


def linkedin_parents(db: Db) -> list[dict[str, Any]]:
    """Every parent in the identity-review progress scope, including completed ones."""
    rows = db.query(
        _LINKEDIN_CTE
        + _PARENT_SELECT.format(where="WHERE p.parent_id IN (SELECT parent_id FROM identity_scope)")
    )
    return _hydrate_parents(db, rows, pending_only=False)


def all_parents(db: Db) -> list[dict[str, Any]]:
    """Web-ready review/directory model, including completed and rejected parents."""
    rows = db.query(
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


def linkedin_queue(db: Db) -> list[dict[str, Any]]:
    """One stable card per pending parent, carrying all of its pending candidates."""
    rows = db.query(
        _LINKEDIN_CTE
        + _PARENT_SELECT.format(where="WHERE p.parent_id IN (SELECT parent_id FROM pending_parents)")
    )
    return _hydrate_parents(db, rows, pending_only=True)


def linkedin_progress(db: Db) -> dict[str, int]:
    row = db.query(
        _LINKEDIN_CTE
        + """
SELECT (SELECT count(*) FROM identity_scope) AS total,
       (SELECT count(*) FROM pending_parents) AS pending
"""
    )[0]
    total, pending = int(row["total"]), int(row["pending"])
    return {"total": total, "pending": pending, "done": total - pending}


def stage_progress(db: Db) -> dict[str, int]:
    worth = worth_counts(db)
    linkedin = linkedin_progress(db)
    # A stale child mentioned by a synthetic can belong to a second canonical
    # parent. Its fact-only fallback is not another lookup; a materialized link
    # or research result is the durable subject marker.
    lookup_ready = db.query(
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
    total = db.query("SELECT count(*) AS n FROM parents")[0]["n"]
    rejected = db.query(
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


def stage_states(db: Db) -> dict[str, dict[str, Any]]:
    """Every typed stage row, keyed by its stable stage name."""
    return {row["stage"]: dict(row) for row in db.query(
        "SELECT * FROM stage_state ORDER BY stage"
    )}


def review_state(db: Db) -> dict[str, Any]:
    """Small compatibility snapshot for the fixed review-stage manifest response."""
    states = stage_states(db)
    current = states.get("review") or states.get("linkedin") or states.get("worth")
    completed = sorted(
        stage for stage, row in states.items() if row["status"] == "complete"
    )
    return {
        "stage": str((current or {}).get("stage") or ""),
        "status": str((current or {}).get("status") or "pending"),
        "completed_stages": completed,
        "updated_at": (current or {}).get("updated_at"),
        "selection_fingerprint": (current or {}).get("selection_fingerprint"),
    }


def enrichment_state(db: Db) -> dict[str, Any]:
    """Typed enrichment stage, job, approval, and projected result in one read model."""
    stages = stage_states(db)
    stage = stages.get("enrich") or stages.get("enrichment")
    jobs = db.query(
        "SELECT * FROM jobs WHERE kind='enrichment' ORDER BY finished_at DESC, name LIMIT 1"
    )
    job = dict(jobs[0]) if jobs else None
    approval_rows = db.query(
        "SELECT * FROM spend_approvals WHERE stage IN ('enrich', 'enrichment') "
        "ORDER BY approved_at DESC LIMIT 1"
    )
    approval = dict(approval_rows[0]) if approval_rows else None
    selection = (stage or {}).get("selection_fingerprint") or (
        job or {}
    ).get("selection_fingerprint")
    result = _json((job or {}).get("result_json"), {})
    return {
        "status": (stage or {}).get("status") or (job or {}).get("status") or "pending",
        "selection_fingerprint": selection,
        "current": bool(stage and (not job or not job.get("selection_fingerprint")
                                    or job["selection_fingerprint"] == selection)),
        "approval_current": bool(
            approval and approval.get("selection_fingerprint") == selection
        ),
        "stage": stage,
        "job": job,
        "approval": approval,
        "result": result if isinstance(result, dict) else {},
    }


def retarget_snapshot(db: Db) -> dict[str, list[dict[str, Any]]]:
    """Guided-retarget requests and their durable job rows."""
    guidance = []
    for row in db.query("SELECT * FROM guidance ORDER BY submitted_at, handle"):
        item = dict(row)
        item["detail"] = _json(item.pop("detail_json"), {})
        guidance.append(item)
    jobs = []
    for row in db.query(
        "SELECT * FROM jobs WHERE kind='guided_retarget' ORDER BY started_at, name"
    ):
        item = dict(row)
        item["result"] = _json(item.pop("result_json"), {})
        jobs.append(item)
    return {"guidance": guidance, "jobs": jobs}


def artifact_path(
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
    rows = db.query(
        f"SELECT path FROM artifacts WHERE {' AND '.join(clauses)} "
        "ORDER BY projected_at DESC, artifact_key LIMIT 1",
        tuple(params),
    )
    return str(rows[0]["path"]) if rows else None


def dossier_path(db: Db, slug_or_parent_id: str) -> str | None:
    rows = db.query(
        "SELECT parent_id FROM parents WHERE parent_id=? OR display_slug=? "
        "OR public_identifier=? LIMIT 1",
        (slug_or_parent_id, slug_or_parent_id, slug_or_parent_id),
    )
    return artifact_path(db, "dossier", parent_id=rows[0]["parent_id"]) if rows else None


def avatar_path(db: Db, public_identifier: str) -> str | None:
    """The explicitly projected cached image; transport sniffs its content type."""
    rows = db.query(
        "SELECT row_key FROM links WHERE public_identifier=? "
        "OR machine_proposed_public_identifier=? OR replacement_public_identifier=? LIMIT 1",
        (public_identifier, public_identifier, public_identifier),
    )
    if not rows:
        return None
    return artifact_path(db, "avatar", candidate_key=rows[0]["row_key"])


def siblings_of(db: Db, candidate_key: str) -> list[str]:
    """All real, synthetic, and ghost candidate keys for the clicked parent."""
    rows = db.query(
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
        for row in worth_rows(db)
    ]


def person_detail(db: Db, slug_or_parent_id: str) -> dict[str, Any] | None:
    """One SQL-hydrated parent; artifact paths may be opened by the response adapter."""
    rows = db.query(
        _WORTH_CTE
        + _PARENT_SELECT.format(
            where="WHERE p.parent_id=? OR p.display_slug=? OR p.public_identifier=?"
        ),
        (slug_or_parent_id, slug_or_parent_id, slug_or_parent_id),
    )
    hydrated = _hydrate_parents(db, rows[:1], pending_only=False)
    return hydrated[0] if hydrated else None


def set_worth(db: Db, parent_or_key: str, value: str, *, note: str | None = None) -> None:
    db.set_worth(parent_or_key.removeprefix(PARENT_WORTH_PREFIX), value, note=note)


def reset_worth(db: Db, parent_or_key: str) -> None:
    db.reset_worth(parent_or_key.removeprefix(PARENT_WORTH_PREFIX))


def settle_identity(db: Db, candidate_key: str, action: str, **replacement: Any) -> list[str]:
    return db.settle_identity(candidate_key, action, **replacement)


def reset_identity(db: Db, candidate_key: str) -> list[str]:
    return db.reset_identity(candidate_key)
