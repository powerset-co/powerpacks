"""LinkedIn review, enrichment, and identity receipt projections.

Changelog:
- 2026-10-01: the pending enrichment work is read as keys (`lookups_pending`,
  `workflow_identity_progress`, `unassembled_research`), so a run can record what it left unfinished.
- 2026-09-30: `enrichment_queue` reads research once and identifiers through the person;
  it runs on every review page load and status poll.
- 2026-10-01: `linkedin_parent_pending` tells the server's queue whether a decided parent has left.
- 2026-09-30: `linkedin_queue_order` + `linkedin_queue_parent` load one review card;
  `linkedin_queue` stays for callers that want every card.
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
    _linkedin_candidate_shown,
    _linkedin_parent_pending,
    _linkedin_queue_order,
    _linkedin_queue_parent,
)
from packs.ingestion.primitives.deep_context.db._view_sql import (
    LINKEDIN_CTE,
    WORTH_CTE,
    WORTH_GATE_ACCEPTED,
)
from packs.ingestion.primitives.deep_context.db.identity_policy import (
    AFFIRMATIVE_HUMAN_DECISION_SQL,
    AFFIRMATIVE_MACHINE_ACTIONS,
    AFFIRMATIVE_MACHINE_APPROVALS,
    AFFIRMATIVE_MACHINE_DECISION_SQL,
)
from packs.ingestion.primitives.deep_context.db.models import (
    IdentifierKind,
    LinkSnapshotRow,
    ReviewAction,
    RowKind,
    ResearchHandle,
)
from packs.ingestion.primitives.deep_context.db.identity_queries import links, review_rows, stored_judgments
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judgment_policy import VERDICTS
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.primitives.deep_context.db.schema import ID_SET, id_set
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.view_models import (
    ApprovedIdentityRow,
    EnrichmentQueueRow,
    LinkedInProgress,
    LinkedInQueueRow,
    ParentViewRow,
    SyntheticFallbackRow,
)


_JUDGE_CANDIDATE_SELECT = """
SELECT l.row_key FROM eligible_links l JOIN identity_scope s USING(parent_id)
WHERE l.kind!='synthetic' AND l.decision_action IS NULL
  AND COALESCE(l.machine_approved, '') NOT IN ('auto', 'yes', 'no')
  AND (COALESCE(l.linkedin_url, '')!='' OR COALESCE(l.machine_proposed_url, '')!=''
       OR EXISTS (SELECT 1 FROM research r WHERE r.candidate_key=l.row_key AND r.status='complete'))
"""

_REVIEW_QUESTIONS_PENDING_FROM = """
FROM pending_parents p WHERE EXISTS (
  SELECT 1 FROM eligible_links candidate WHERE candidate.parent_id=p.parent_id
    AND candidate.kind!='synthetic'
    AND (COALESCE(candidate.linkedin_url, '')!='' OR COALESCE(candidate.machine_proposed_url, '')!='')
) AND NOT EXISTS (
  SELECT 1 FROM links l WHERE l.parent_id=p.parent_id
    AND json_extract(l.judgment_payload_json, '$.relationship_decision') IS NOT NULL
)
"""

_ENRICHMENT_QUEUE_FROM = f"""
FROM worth w
LEFT JOIN eligible_links l ON l.row_key=(
  SELECT choice.row_key FROM eligible_links choice
  WHERE choice.parent_id=w.parent_id AND choice.kind!='synthetic'
  ORDER BY choice.candidate_origin DESC, choice.row_key LIMIT 1
)
WHERE {WORTH_GATE_ACCEPTED}
  AND EXISTS (SELECT 1 FROM facts f WHERE f.parent_id=w.parent_id)
  AND NOT EXISTS (
    SELECT 1 FROM eligible_links known
    WHERE known.parent_id=w.parent_id AND known.kind!='synthetic'
      AND (COALESCE(known.linkedin_url, '')!=''
           OR COALESCE(known.machine_proposed_url, '')!=''
           OR COALESCE(known.replacement_url, '')!='')
  )
  AND w.parent_id NOT IN (
    SELECT done.parent_id FROM research done
    WHERE done.status IN ('complete', 'no_match')
  )
  AND NOT EXISTS (
    SELECT 1 FROM eligible_links decided WHERE decided.parent_id=w.parent_id
      AND decided.decision_approved IN ('yes', 'no')
  )
"""

_SYNTHETIC_FALLBACK_FROM = f"""
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
  AND NOT EXISTS (
    SELECT 1 FROM eligible_links real
    WHERE real.parent_id=r.parent_id AND real.kind!='synthetic'
      AND CASE WHEN real.decision_action IS NOT NULL THEN
        {AFFIRMATIVE_HUMAN_DECISION_SQL} AND real.decision_approved='yes'
      ELSE {AFFIRMATIVE_MACHINE_DECISION_SQL.format(prefix='real.')} END
  )
"""


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
        and (link.kind != RowKind.SYNTHETIC.value or review.action == ReviewAction.RETARGET.value)
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


def enrichment_queue(db: Db) -> list[EnrichmentQueueRow]:
    """Return worth-Yes parents with no known LinkedIn or completed research."""
    rows = db.query(
        WORTH_CTE
        + f"""
SELECT l.row_key, w.parent_id, w.display_slug, w.display_name,
       l.candidate_origin,
       -- CROSS JOIN pins the identifier lookups to the family's people; left to
       -- itself the planner walks identifiers_by_value(kind) for every row.
       (SELECT json_group_array(person_id) FROM (
          SELECT person_id FROM people
          WHERE parent_id=w.parent_id AND is_owner=0 AND is_ghost=0
          ORDER BY person_id
        )) AS person_ids_json,
       (SELECT json_group_array(value) FROM (
          SELECT DISTINCT COALESCE(i.display_value, i.normalized_value) AS value
          FROM people pe CROSS JOIN person_identifiers i ON i.person_id=pe.person_id
          WHERE pe.parent_id=w.parent_id AND pe.is_owner=0 AND i.kind='email'
          ORDER BY value
        )) AS emails_json,
       (SELECT json_group_array(value) FROM (
          SELECT DISTINCT COALESCE(i.display_value, i.normalized_value) AS value
          FROM people pe CROSS JOIN person_identifiers i ON i.person_id=pe.person_id
          WHERE pe.parent_id=w.parent_id AND pe.is_owner=0 AND i.kind='phone'
          ORDER BY value
        )) AS phones_json
"""
        + _ENRICHMENT_QUEUE_FROM
        + """
ORDER BY lower(COALESCE(w.display_name, w.public_identifier)), w.parent_id
""",
    )
    return [
        EnrichmentQueueRow(
            parent_id=row["parent_id"],
            parent_slug=ResearchHandle.for_parent(row["parent_id"], row["display_slug"]),
            name=row["display_name"] or row["row_key"] or row["parent_id"],
            person_ids=tuple(_json(row["person_ids_json"], [])),
            row_key=row["row_key"] or f"research:{row['parent_id']}",
            candidate_exists=bool(row["row_key"]),
            linkedin_url="",
            verdict="no_linkedin_candidate",
            verdict_reason="",
            match_emails=tuple(_json(row["emails_json"], [])),
            match_phones=tuple(_json(row["phones_json"], [])),
            candidate_origin=bool(row["candidate_origin"]),
        )
        for row in rows
    ]


def lookups_pending(db: Db) -> tuple[str, ...]:
    """The parents `enrichment_queue` would send to research."""
    return tuple(row["parent_id"] for row in db.query(
        WORTH_CTE + "SELECT w.parent_id " + _ENRICHMENT_QUEUE_FROM + " ORDER BY w.parent_id"
    ))


def workflow_identity_progress(db: Db) -> tuple[LinkedInProgress, tuple[str, ...], tuple[str, ...]]:
    """LinkedIn review progress, the parents with an unsettled question, and the unjudged LinkedIns."""
    row = db.query(
        LINKEDIN_CTE + ", judge_candidates AS (" + _JUDGE_CANDIDATE_SELECT + ")" + """
SELECT (SELECT count(*) FROM identity_scope) AS total,
       (SELECT count(*) FROM pending_parents) AS pending,
       (SELECT json_group_array(p.parent_id) """ + _REVIEW_QUESTIONS_PENDING_FROM + """) AS questions,
       (SELECT json_group_array(row_key) FROM judge_candidates) AS candidate_keys
"""
    )[0]
    total, pending = int(row["total"]), int(row["pending"])
    return (
        LinkedInProgress(total, pending, total - pending),
        tuple(sorted(_json(row["questions"], []))),
        _unjudged(db, _json(row["candidate_keys"], [])),
    )


def _unjudged(db: Db, keys: list[str]) -> tuple[str, ...]:
    """The LinkedIns among `keys` with no stored verdict the judge stands by."""
    judged = {key for key, stored in stored_judgments(db, row_keys=tuple(keys)).items()
              if stored.verdict.value in VERDICTS}
    return tuple(sorted(set(keys) - judged))


def _judge_candidate_keys(db: Db) -> tuple[str, ...]:
    """Real mapped LinkedIns without human or valid machine decisions."""
    return _unjudged(db, [row["row_key"] for row in db.query(LINKEDIN_CTE + _JUDGE_CANDIDATE_SELECT)])


def judge_candidates(db: Db) -> list[LinkSnapshotRow]:
    """Real mapped LinkedIns without human or valid machine decisions."""
    return list(links(db, row_keys=_judge_candidate_keys(db)))


def research_candidate_urls(db: Db) -> dict[str, str]:
    return {row["candidate_key"]: row["linkedin_url"] for row in db.query(
        "SELECT candidate_key, json_extract(result_json, '$.content.linkedin_url') AS linkedin_url "
        "FROM research WHERE status='complete' AND candidate_key IS NOT NULL "
        # A provider answer whose LinkedIn is not text is no LinkedIn.
        "AND json_type(result_json, '$.content.linkedin_url')='text'"
    )}


def unassembled_research(db: Db) -> tuple[str, ...]:
    """Usable, unambiguous no-match parents without synthetic review cards."""
    rows = db.query(
        WORTH_CTE + """
SELECT r.parent_id, r.result_json,
       (l.machine_action='retarget'
        AND COALESCE(l.machine_judgment, '')!='confirmed') AS research_link_rejected
""" + _SYNTHETIC_FALLBACK_FROM + """
  AND r.parent_id NOT IN (SELECT public_identifier FROM synthetic_profiles)
"""
    )
    counts: dict[str, int] = {}
    for row in rows:
        result = ResearchResult.from_json(row["result_json"])
        if result and result.usable and (not result.linkedin_url or row["research_link_rejected"]):
            counts[row["parent_id"]] = counts.get(row["parent_id"], 0) + 1
    return tuple(sorted(parent_id for parent_id, count in counts.items() if count == 1))


def synthetic_fallback(db: Db) -> list[SyntheticFallbackRow]:
    """Return completed research rows still needing a synthetic-profile decision.

    ``existing_approved`` reads back 'no' for a detach/exclude decision even
    when ``decision_approved`` says 'yes' because the action wins.
    """
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
"""
        + _SYNTHETIC_FALLBACK_FROM
        + """
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


def linkedin_parents(db: Db, *, parent_ids: Sequence[str] | None = None) -> list[ParentViewRow]:
    return _all_parents(db, parent_ids=parent_ids)


def decision_parents(db: Db, decision: str, *, offset: int = 0, limit: int = 10) -> list[ParentViewRow]:
    """One LIMIT/OFFSET page of one worth pile (yes/no) for the review tables."""
    return _decision_page(db, decision, offset, limit)


def linkedin_queue(db: Db) -> list[ParentViewRow]:
    return _linkedin_queue(db)


def linkedin_queue_order(db: Db) -> list[LinkedInQueueRow]:
    """The queue's parents in card order — ids and slugs only."""
    return _linkedin_queue_order(db)


def linkedin_candidate_shown(db: Db, row_key: str) -> bool:
    """Whether an identity row is a candidate some card shows (an owner-only row is not)."""
    return _linkedin_candidate_shown(db, row_key)


def linkedin_parent_pending(db: Db, parent_id: str) -> bool:
    """Whether a queued parent still has a candidate to check."""
    return _linkedin_parent_pending(db, parent_id)


def linkedin_queue_parent(db: Db, parent_id: str) -> ParentViewRow:
    """One queued parent with its pending candidates: the card `linkedin_queue` would hold."""
    return _linkedin_queue_parent(db, parent_id)


def linkedin_progress(db: Db) -> LinkedInProgress:
    return _linkedin_progress(db)


def pending_parent_ids(db: Db) -> frozenset[str]:
    """Parents with unresolved LinkedIn candidates eligible for human review."""
    return frozenset(row["parent_id"] for row in db.query(
        LINKEDIN_CTE + "SELECT parent_id FROM pending_parents"
    ))


def review_questions_pending(db: Db) -> int:
    """Unresolved parents whose review questions have not been selected yet."""
    return int(db.query(LINKEDIN_CTE + "SELECT count(*) " + _REVIEW_QUESTIONS_PENDING_FROM)[0][0])
