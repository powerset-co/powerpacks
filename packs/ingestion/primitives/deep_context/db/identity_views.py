"""LinkedIn review, enrichment, and identity receipt projections.

Changelog:
- 2026-09-25: approved families read through `_family_rows`, one JSON-bound id set.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from packs.ingestion.primitives.deep_context.db._view_rows import (
    _all_parents,
    _decision_page,
    _json,
    _linkedin_progress,
    _linkedin_queue,
)
from packs.ingestion.primitives.deep_context.db._view_sql import (
    WORTH_CTE,
    WORTH_GATE_ACCEPTED,
)
from packs.ingestion.primitives.deep_context.db.identity_policy import (
    AFFIRMATIVE_MACHINE_ACTIONS,
    AFFIRMATIVE_MACHINE_APPROVALS,
)
from packs.ingestion.primitives.deep_context.db.models import (
    IdentifierKind,
    RESEARCH_CONFIRM_THRESHOLD,
    ReviewAction,
    RowKind,
    ResearchHandle,
)
from packs.ingestion.primitives.deep_context.db.identity_queries import links, review_rows
from packs.ingestion.primitives.deep_context.db.schema import ID_SET, id_set
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.view_models import (
    ApprovedIdentityRow,
    EnrichmentQueueRow,
    LinkedInProgress,
    ParentViewRow,
    SyntheticFallbackRow,
)


def resolve_identity_key(db: Db, value: str) -> tuple[str, str] | None:
    """Resolve one external row key or public identifier to row key and parent."""
    value = value.strip().lower()
    if not value:
        return None
    exact = db.query("SELECT row_key, parent_id FROM links WHERE lower(row_key)=?", (value,))
    if exact:
        return str(exact[0]["row_key"]), str(exact[0]["parent_id"])
    matches = db.query(
        "SELECT row_key, parent_id FROM links WHERE lower(public_identifier)=? ORDER BY row_key",
        (value,),
    )
    if len(matches) > 1:
        raise StoreError(f"ambiguous identity candidate: {value}")
    if not matches:
        return None
    return str(matches[0]["row_key"]), str(matches[0]["parent_id"])


# WORTH_GATE_NOT_REJECTED / WORTH_GATE_ACCEPTED / WORTH_GATE_REJECTED are
# defined in _view_sql.py (see the comment there) so identity_scope in
# LINKEDIN_CTE and the workflow_views.py rollups can share them too, without
# an import cycle back into this module.


def _family_rows(db: Db, parent_ids: Sequence[str]) -> list[sqlite3.Row]:
    """Members and identifiers of the given families; the ids bind as one JSON array."""
    return db.query(
        f"""
SELECT p.parent_id, p.display_name, pe.person_id, pe.is_ghost,
       pi.kind, pi.normalized_value, pi.display_value
FROM parents p
JOIN people pe USING(parent_id)
LEFT JOIN person_identifiers pi USING(person_id)
WHERE p.parent_id IN {ID_SET}
ORDER BY p.parent_id, pe.person_id, pi.kind, pi.normalized_value
""",
        (id_set(parent_ids),),
    )


def approved_identities(db: Db) -> list[ApprovedIdentityRow]:
    links_by_key = {row.row_key: row for row in links(db)}
    approved = [
        (review, link)
        for review in review_rows(db, include_worth=False)
        if (link := links_by_key.get(review.key)) is not None
        and link.kind != RowKind.SYNTHETIC.value
        and review.action in AFFIRMATIVE_MACHINE_ACTIONS
        and review.approved in AFFIRMATIVE_MACHINE_APPROVALS
    ]
    if not approved:
        return []

    rows = _family_rows(db, sorted({link.parent_id for _, link in approved}))
    names: dict[str, str] = {}
    real_members: dict[str, list[str]] = {}
    identifiers: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        parent_id = str(row["parent_id"])
        names[parent_id] = str(row["display_name"] or "")
        if not row["is_ghost"]:
            members = real_members.setdefault(parent_id, [])
            person_id = str(row["person_id"])
            if person_id not in members:
                members.append(person_id)
        kind = str(row["kind"] or "")
        if kind in {IdentifierKind.EMAIL.value, IdentifierKind.PHONE.value}:
            identifiers.setdefault(parent_id, {}).setdefault(kind, set()).add(
                str(row["display_value"] or row["normalized_value"])
            )

    return [
        ApprovedIdentityRow(
            row_key=review.key,
            name=names[link.parent_id],
            action=review.action or "",
            linkedin_url=(
                review.new_linkedin_url if review.action == ReviewAction.RETARGET.value else review.linkedin_url
            )
            or "",
            person_id=next(iter(real_members.get(link.parent_id, ())), ""),
            emails=tuple(sorted(identifiers.get(link.parent_id, {}).get(IdentifierKind.EMAIL.value, set()))),
            phones=tuple(sorted(identifiers.get(link.parent_id, {}).get(IdentifierKind.PHONE.value, set()))),
        )
        for review, link in approved
    ]


def enrichment_queue(
    db: Db,
    *,
    include_plausibly_absent: bool = False,
    include_applied_retargets: bool = False,
    confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
) -> list[EnrichmentQueueRow]:
    """Return worth='yes' families eligible for paid research.

    ``confirm_threshold`` binds twice below: once to decide whether an
    existing confirmed sibling link is trusted enough to suppress research on
    this row, and again to decide whether this row's own wrong_person verdict
    is confident enough to warrant deep research. Same number, two gates.
    """
    rows = db.query(
        WORTH_CTE
        + f"""
SELECT l.row_key, l.parent_id, w.display_slug, w.display_name, l.linkedin_url,
       l.machine_reason, l.machine_judgment, l.candidate_origin,
       (SELECT json_group_array(person_id) FROM (
          SELECT person_id FROM people
          WHERE parent_id=l.parent_id AND is_owner=0 AND is_ghost=0
          ORDER BY person_id
        )) AS person_ids_json,
       (SELECT json_group_array(value) FROM (
          SELECT DISTINCT COALESCE(i.display_value, i.normalized_value) AS value
          FROM people pe JOIN person_identifiers i USING(person_id)
          WHERE pe.parent_id=l.parent_id AND pe.is_owner=0 AND i.kind='email'
          ORDER BY value
        )) AS emails_json,
       (SELECT json_group_array(value) FROM (
          SELECT DISTINCT COALESCE(i.display_value, i.normalized_value) AS value
          FROM people pe JOIN person_identifiers i USING(person_id)
          WHERE pe.parent_id=l.parent_id AND pe.is_owner=0 AND i.kind='phone'
          ORDER BY value
        )) AS phones_json
FROM eligible_links l JOIN worth w USING(parent_id)
WHERE {WORTH_GATE_ACCEPTED}
  AND EXISTS (SELECT 1 FROM facts f WHERE f.parent_id=l.parent_id)
  AND COALESCE(l.decision_approved, '') NOT IN ('yes', 'no')
  AND COALESCE(l.decision_action, '')!='exclude'
  AND (
    ? OR NOT (
      l.machine_action='retarget'
      AND l.machine_proposed_url IS NOT NULL
      AND COALESCE(l.machine_approved, '') IN ('auto', 'yes')
    )
  )
  AND NOT EXISTS (
    SELECT 1 FROM eligible_links kept
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
    (l.candidate_origin=1 AND l.raw_import=1)
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
            int(include_applied_retargets),
            confirm_threshold,
            confirm_threshold,
            int(include_plausibly_absent),
        ),
    )
    return [
        EnrichmentQueueRow(
            parent_id=row["parent_id"],
            parent_slug=ResearchHandle.for_parent(row["parent_id"], row["display_slug"]),
            name=row["display_name"] or row["row_key"],
            person_ids=tuple(_json(row["person_ids_json"], [])),
            row_key=row["row_key"],
            candidate_exists=True,
            linkedin_url=row["linkedin_url"] or "",
            verdict=row["machine_judgment"] or "no_linkedin_candidate",
            verdict_reason=row["machine_reason"] or "",
            match_emails=tuple(_json(row["emails_json"], [])),
            match_phones=tuple(_json(row["phones_json"], [])),
            candidate_origin=bool(row["candidate_origin"]),
        )
        for row in rows
    ]


def synthetic_fallback(db: Db) -> list[SyntheticFallbackRow]:
    """Return completed research rows still needing a synthetic-profile decision.

    ``existing_approved`` reads back 'no' for a detach/exclude decision even
    when ``decision_approved`` says 'yes' because the action wins.
    """
    rows = db.query(
        WORTH_CTE
        + f""", research_people AS (
  SELECT r.handle, r.candidate_key, cp.person_id
  FROM research r JOIN candidate_people cp ON cp.row_key=r.candidate_key
  JOIN people pe ON pe.person_id=cp.person_id
  WHERE pe.is_owner=0
  UNION ALL
  SELECT r.handle, r.candidate_key, pe.person_id
  FROM research r JOIN people pe ON pe.parent_id=r.parent_id
  WHERE pe.is_owner=0 AND NOT EXISTS (
    SELECT 1 FROM candidate_people cp WHERE cp.row_key=r.candidate_key
  )
)
SELECT r.parent_id, r.artifact_key, r.result_json, p.display_name,
       (l.machine_action='retarget'
        AND COALESCE(l.machine_judgment, '')!='confirmed') AS research_link_rejected,
       (SELECT json_group_array(person_id) FROM (
          SELECT person_id FROM research_people rp
          WHERE rp.handle=r.handle AND rp.candidate_key=r.candidate_key
          ORDER BY person_id
        )) AS person_ids_json,
       (SELECT CASE
            WHEN sl.decision_action IN ('detach', 'exclude') AND sl.decision_approved IS NOT NULL
              THEN 'no'
            ELSE COALESCE(sl.decision_approved, sl.machine_approved, '')
          END
        FROM synthetic_profiles sp
        JOIN eligible_links sl ON sl.row_key=sp.candidate_key
        WHERE sp.public_identifier=r.parent_id
        LIMIT 1) AS existing_approved
FROM research r
JOIN parents p ON p.parent_id=r.parent_id
JOIN worth w USING(parent_id)
LEFT JOIN links l ON l.row_key=r.candidate_key
LEFT JOIN eligible_links scoped ON scoped.row_key=r.candidate_key
WHERE {WORTH_GATE_ACCEPTED}
  AND EXISTS (
  SELECT 1 FROM people member
  WHERE member.parent_id=r.parent_id
    AND member.is_owner=0
    AND member.is_ghost=0
)
  AND (l.row_key IS NULL OR scoped.row_key IS NOT NULL)
ORDER BY r.parent_id, r.handle, r.candidate_key
"""
    )
    return [
        SyntheticFallbackRow(
            parent_id=row["parent_id"],
            artifact_key=row["artifact_key"],
            result_json=row["result_json"] or "",
            display_name=row["display_name"] or "",
            research_link_rejected=bool(row["research_link_rejected"]),
            person_ids=tuple(_json(row["person_ids_json"], [])),
            existing_approved=row["existing_approved"] or "",
        )
        for row in rows
    ]


def linkedin_parents(db: Db) -> list[ParentViewRow]:
    return _all_parents(db)


def decision_parents(db: Db, decision: str, *, offset: int = 0, limit: int = 100) -> list[ParentViewRow]:
    """One LIMIT/OFFSET page of one worth pile (yes/no) for the review tables."""
    return _decision_page(db, decision, offset, limit)


def linkedin_queue(db: Db) -> list[ParentViewRow]:
    return _linkedin_queue(db)


def linkedin_progress(db: Db) -> LinkedInProgress:
    return _linkedin_progress(db)
