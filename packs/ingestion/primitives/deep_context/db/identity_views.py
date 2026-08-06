"""LinkedIn review, enrichment, and identity receipt projections."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from packs.ingestion.primitives.deep_context.db._view_rows import (
    _all_parents,
    _json,
    _linkedin_progress,
    _linkedin_queue,
)
from packs.ingestion.primitives.deep_context.db._view_sql import WORTH_CTE
from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
    IdentifierKind,
    RESEARCH_CONFIRM_THRESHOLD,
    ReviewAction,
    RowKind,
    ResearchHandle,
)
from packs.ingestion.primitives.deep_context.db.snapshots import (
    canonical_snapshot,
    identity_snapshot,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError


@dataclass(frozen=True)
class AttachedIdentityQueueRow:
    """One worth-gated attached identity ready for dossier/profile shaping."""

    parent_id: str
    parent_slug: str
    name: str
    candidate_key: str
    public_identifier: str
    linkedin_url: str
    person_ids: tuple[str, ...]
    conflict: bool
    from_connections: bool


@dataclass(frozen=True)
class HealIdentityQueueRow:
    """One worth-gated stale attached identity selected entirely by SQL."""

    parent_id: str
    parent_slug: str
    name: str
    candidate_key: str
    public_identifier: str
    linkedin_url: str
    selection: Literal["candidate", "pending_retarget"]


_ATTACHED_IDENTITY_CTE = (
    WORTH_CTE
    + """, attached_identity_queue AS (
  SELECT l.*,
         COALESCE(w.display_slug, p.display_slug) AS parent_display_slug,
         COALESCE(NULLIF(w.display_name, ''), NULLIF(p.display_name, ''),
                  NULLIF(l.display_name, ''), p.public_identifier) AS parent_name,
         count(*) OVER (PARTITION BY l.parent_id) AS sibling_count
  FROM eligible_links l JOIN parents p USING(parent_id)
  JOIN worth w USING(parent_id)
  WHERE w.effective_worth!='no'
    AND NULLIF(trim(l.linkedin_url), '') IS NOT NULL
    AND l.kind NOT IN ('synthetic', 'research')
    AND EXISTS (
      SELECT 1 FROM people member
      WHERE member.parent_id=l.parent_id AND member.is_owner=0 AND member.is_ghost=0
    )
)
"""
)


def _attached_identity_queue(db: Db) -> list[AttachedIdentityQueueRow]:
    """Return the attached-link judge queue after the single upstream worth gate."""
    rows = db.query(
        _ATTACHED_IDENTITY_CTE
        + """, selected_people AS (
  SELECT q.row_key, cp.person_id
  FROM attached_identity_queue q
  JOIN candidate_people cp ON cp.row_key=q.row_key
  JOIN people pe ON pe.person_id=cp.person_id
  WHERE pe.is_owner=0 AND pe.is_ghost=0
  UNION ALL
  SELECT q.row_key, pe.person_id
  FROM attached_identity_queue q
  JOIN people pe ON pe.parent_id=q.parent_id
  WHERE pe.is_owner=0 AND pe.is_ghost=0
    AND NOT EXISTS (
      SELECT 1 FROM candidate_people cp
      JOIN people member ON member.person_id=cp.person_id
      WHERE cp.row_key=q.row_key AND member.is_owner=0 AND member.is_ghost=0
    )
)
SELECT q.parent_id, q.parent_display_slug, q.parent_name, q.row_key,
       q.public_identifier, q.linkedin_url, q.sibling_count,
       (SELECT json_group_array(person_id) FROM (
          SELECT person_id FROM selected_people sp
          WHERE sp.row_key=q.row_key ORDER BY person_id
        )) AS person_ids_json,
       EXISTS (
         SELECT 1 FROM selected_people sp JOIN person_sources ps USING(person_id)
         WHERE sp.row_key=q.row_key AND ps.source='linkedin_csv'
       ) AS from_connections
FROM attached_identity_queue q
ORDER BY q.row_key
"""
    )
    return [
        AttachedIdentityQueueRow(
            parent_id=row["parent_id"],
            parent_slug=ResearchHandle.for_parent(
                row["parent_id"], row["parent_display_slug"],
            ),
            name=row["parent_name"],
            candidate_key=row["row_key"],
            public_identifier=str(row["public_identifier"] or "").lower(),
            linkedin_url=row["linkedin_url"],
            person_ids=tuple(_json(row["person_ids_json"], [])),
            conflict=int(row["sibling_count"]) > 1,
            from_connections=bool(row["from_connections"]),
        )
        for row in rows
    ]


def _heal_identity_queue(db: Db, no_profile_reason: str) -> list[HealIdentityQueueRow]:
    """Return stale attached links and retarget skips from the worth-gated SQL queue."""
    rows = db.query(
        _ATTACHED_IDENTITY_CTE
        + """, heal_queue AS (
  SELECT q.*,
         CASE
           WHEN COALESCE(q.decision_action, q.machine_action, '')='retarget'
             AND NULLIF(COALESCE(q.replacement_public_identifier,
                                 q.machine_proposed_public_identifier, ''), '') IS NOT NULL
             AND lower(COALESCE(q.replacement_public_identifier,
                                q.machine_proposed_public_identifier, ''))
                 != lower(q.public_identifier)
           THEN 'pending_retarget'
           ELSE 'candidate'
         END AS selection
  FROM attached_identity_queue q
  WHERE q.machine_judgment='needs_review'
    AND COALESCE(q.machine_confidence, 0)=0
    AND q.machine_reason=?
    AND lower(COALESCE(q.decision_approved, q.machine_approved, ''))
        NOT IN ('yes', 'no', 'auto')
)
SELECT parent_id, parent_display_slug, parent_name, row_key,
       public_identifier, linkedin_url, selection
FROM heal_queue
ORDER BY COALESCE(NULLIF(parent_display_slug, ''), parent_id), row_key
""",
        (no_profile_reason,),
    )
    return [
        HealIdentityQueueRow(
            parent_id=row["parent_id"],
            parent_slug=ResearchHandle.for_parent(
                row["parent_id"], row["parent_display_slug"],
            ),
            name=row["parent_name"],
            candidate_key=row["row_key"],
            public_identifier=str(row["public_identifier"] or "").lower(),
            linkedin_url=row["linkedin_url"],
            selection=row["selection"],
        )
        for row in rows
    ]


def _approved_identities(db: Db) -> list[dict[str, Any]]:
    canonical, identity = canonical_snapshot(db), identity_snapshot(db)
    links = {row.row_key: row for row in identity.links}
    parents = {row.parent_id: row for row in canonical.parents}
    people: dict[str, list[Any]] = {}
    for person in canonical.people:
        people.setdefault(person.parent_id, []).append(person)
    identifiers: dict[str, list[Any]] = {}
    for identifier in canonical.identifiers:
        identifiers.setdefault(identifier.person_id, []).append(identifier)

    result = []
    for review in identity.review_rows:
        link = links.get(review.key)
        if (
            link is None
            or link.kind == RowKind.SYNTHETIC.value
            or review.action not in {ReviewAction.VERIFY.value, ReviewAction.RETARGET.value}
            or review.approved not in {ApprovedState.AUTO.value, ApprovedState.YES.value}
        ):
            continue
        members = sorted(people.get(link.parent_id, []), key=lambda row: row.person_id)
        real_members = [row for row in members if not row.is_ghost]
        by_kind = {
            kind: sorted({
                identifier.display_value or identifier.normalized_value
                for person in members
                for identifier in identifiers.get(person.person_id, [])
                if identifier.kind == kind
            })
            for kind in (IdentifierKind.EMAIL.value, IdentifierKind.PHONE.value)
        }
        result.append({
            "row_key": review.key,
            "name": parents[link.parent_id].display_name or "",
            "action": review.action,
            "linkedin_url": (
                review.new_linkedin_url
                if review.action == ReviewAction.RETARGET.value
                else review.linkedin_url
            ),
            "person_id": real_members[0].person_id if real_members else "",
            "emails": by_kind[IdentifierKind.EMAIL.value],
            "phones": by_kind[IdentifierKind.PHONE.value],
        })
    return result


def _enrichment_queue(
    db: Db, *, include_plausibly_absent: bool = False,
    include_candidates: bool = False,
    confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
) -> list[dict[str, Any]]:
    rows = db.query(
        WORTH_CTE
        + """
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
            confirm_threshold,
            int(include_candidates),
            confirm_threshold,
            int(include_plausibly_absent),
        ),
    )
    return [
        {
            "parent_id": row["parent_id"],
            "parent_slug": ResearchHandle.for_parent(row["parent_id"], row["display_slug"]),
            "name": row["display_name"] or row["row_key"],
            "person_ids": _json(row["person_ids_json"], []),
            "candidate_key": row["row_key"],
            "candidate_exists": True,
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


def _synthetic_fallback(db: Db) -> list[dict[str, Any]]:
    rows = db.query(
        WORTH_CTE
        + """, research_people AS (
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
SELECT r.handle, r.parent_id, r.candidate_key, r.result_json,
       p.display_name, p.display_slug,
       w.effective_worth,
       COALESCE(l.machine_reject, '') AS machine_reject,
       (SELECT json_group_array(person_id) FROM (
          SELECT person_id FROM research_people rp
          WHERE rp.handle=r.handle AND rp.candidate_key=r.candidate_key
          ORDER BY person_id
        )) AS person_ids_json,
       (SELECT COALESCE(i.display_value, i.normalized_value)
        FROM research_people rp JOIN person_identifiers i USING(person_id)
        WHERE rp.handle=r.handle AND rp.candidate_key=r.candidate_key AND i.kind='email'
        ORDER BY rp.person_id, i.normalized_value LIMIT 1) AS primary_email,
       (SELECT COALESCE(i.display_value, i.normalized_value)
        FROM research_people rp JOIN person_identifiers i USING(person_id)
        WHERE rp.handle=r.handle AND rp.candidate_key=r.candidate_key AND i.kind='phone'
        ORDER BY rp.person_id, i.normalized_value LIMIT 1) AS phone_e164,
       (SELECT json_group_array(json_object(
          'public_identifier', sp.public_identifier,
          'profile_json', sp.profile_json,
          'action', COALESCE(sl.decision_action, sl.machine_action, ''),
          'approved', CASE
            WHEN sl.decision_action IN ('detach', 'exclude') AND sl.decision_approved IS NOT NULL
              THEN 'no'
            ELSE COALESCE(sl.decision_approved, sl.machine_approved, '')
          END
        ))
        FROM synthetic_profiles sp
        JOIN eligible_links sl ON sl.row_key=sp.candidate_key
        WHERE sl.parent_id=r.parent_id) AS existing_synthetics_json
FROM research r
JOIN parents p ON p.parent_id=r.parent_id
JOIN worth w USING(parent_id)
LEFT JOIN links l ON l.row_key=r.candidate_key
LEFT JOIN eligible_links scoped ON scoped.row_key=r.candidate_key
WHERE EXISTS (
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
        {
            **dict(row),
            "person_ids": _json(row["person_ids_json"], []),
            "existing_synthetics": _json(row["existing_synthetics_json"], []),
        }
        for row in rows
    ]


def linkedin_review(
    db: Db,
    scope: Literal[
        "parents", "queue", "progress", "enrichment", "approved", "synthetic",
        "latest_job", "attached", "heal",
    ],
    *,
    include_plausibly_absent: bool = False,
    include_candidates: bool = False,
    confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
    job_kind: str = "",
    no_profile_reason: str = "",
) -> (
    list[dict[str, Any]]
    | list[AttachedIdentityQueueRow]
    | list[HealIdentityQueueRow]
    | dict[str, Any]
    | None
):
    """Read one scope from the single canonical identity-review policy."""
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
    if scope == "approved":
        return _approved_identities(db)
    if scope == "synthetic":
        return _synthetic_fallback(db)
    if scope == "latest_job":
        rows = db.query(
            "SELECT * FROM jobs WHERE kind=? "
            "ORDER BY COALESCE(finished_at, started_at) DESC, name LIMIT 1",
            (job_kind,),
        )
        return dict(rows[0]) if rows else None
    if scope == "attached":
        return _attached_identity_queue(db)
    if scope == "heal":
        return _heal_identity_queue(db, no_profile_reason)
    raise StoreError(f"unknown LinkedIn review scope: {scope}")
