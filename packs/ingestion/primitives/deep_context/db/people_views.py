"""Projected dossier lookup, person detail, and avatar reads."""

from __future__ import annotations

from dataclasses import replace
from packs.ingestion.primitives.common.contact_fields import normalize_email
from packs.ingestion.primitives.deep_context.common import normalize_name, phone_digits
from packs.ingestion.primitives.deep_context.db._view_rows import (
    _hydrate_parents,
    _json,
)
from packs.ingestion.primitives.deep_context.db._view_sql import PARENT_SELECT, WORTH_CTE
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import (
    AvatarPayload,
    CandidateViewRow,
    ParentLookupRow,
    ParentViewRow,
    PersonLookupRow,
)

__all__ = [
    "CandidateViewRow",
    "ParentViewRow",
    "avatar_payload",
    "person_detail",
    "person_lookup",
]
def person_lookup(
    db: Db,
    *,
    name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
) -> list[PersonLookupRow | ParentLookupRow]:
    """Match live identifiers and names, then hydrate only the matched dossiers."""
    phone_key = phone_digits(phone) if phone else ""
    email_key = normalize_email(email) if email else ""
    name_key = normalize_name(name or "")
    tokens = sorted(set(name_key.split()))
    people_tokens = " AND ".join("lower(pe.display_name) LIKE ?" for _ in tokens) or "0"
    parent_tokens = " AND ".join("lower(p.display_name) LIKE ?" for _ in tokens) or "0"
    token_params = tuple(f"%{token}%" for token in tokens)
    rows = db.query(
        f"""
WITH exact_name_people AS (
  SELECT pe.person_id, pe.parent_id
  FROM people pe
  WHERE ?!='' AND lower(trim(pe.display_name))=?
), exact_name_parents AS (
  SELECT p.parent_id
  FROM parents p
  WHERE ?!='' AND lower(trim(p.display_name))=?
), phone_identifier_digits AS (
  SELECT pi.person_id, replace(pi.normalized_value, '+', '') AS digits
  FROM person_identifiers pi
  WHERE pi.kind='phone'
), phone_identifiers AS (
  SELECT person_id,
         CASE WHEN length(digits)=11 AND substr(digits, 1, 1)='1'
              THEN substr(digits, 2) ELSE digits END AS phone_key
  FROM phone_identifier_digits
), matched_people_raw AS (
  SELECT pe.person_id, pe.parent_id, 10 AS match_order
  FROM phone_identifiers pi JOIN people pe USING(person_id)
  WHERE ?!='' AND pi.phone_key=?
  UNION ALL
  SELECT pe.person_id, pe.parent_id, 20
  FROM person_identifiers pi JOIN people pe USING(person_id)
  WHERE ?!='' AND pi.kind='email' AND pi.normalized_value=?
  UNION ALL
  SELECT person_id, parent_id, 30 FROM exact_name_people
  UNION ALL
  SELECT pe.person_id, pe.parent_id, 30
  FROM people pe
  WHERE ?!=''
    AND NOT EXISTS (SELECT 1 FROM exact_name_people)
    AND NOT EXISTS (SELECT 1 FROM exact_name_parents)
    AND {people_tokens}
), matched_people AS (
  SELECT person_id, parent_id, min(match_order) AS match_order
  FROM matched_people_raw GROUP BY person_id, parent_id
), matched_parents_raw AS (
  SELECT parent_id, min(match_order) + 1 AS match_order
  FROM matched_people WHERE match_order < 30 GROUP BY parent_id
  UNION ALL
  SELECT parent_id, 30 FROM exact_name_parents
  UNION ALL
  SELECT p.parent_id, 30
  FROM parents p
  WHERE ?!=''
    AND NOT EXISTS (SELECT 1 FROM exact_name_people)
    AND NOT EXISTS (SELECT 1 FROM exact_name_parents)
    AND {parent_tokens}
), matched_parents AS (
  SELECT parent_id, min(match_order) AS match_order
  FROM matched_parents_raw GROUP BY parent_id
), results AS (
  SELECT 0 AS entity_kind, mp.match_order, pe.child_slug AS slug,
         pe.display_name AS name, a.path, a.path AS dossier_path,
         json_extract(a.payload_json, '$.body') AS dossier_body,
         json_extract(a.payload_json, '$.headline') AS headline,
         pe.display_name AS full_name,
         (SELECT json_group_array(value) FROM (
            SELECT COALESCE(pi.display_value, pi.normalized_value) AS value
            FROM person_identifiers pi
            WHERE pi.person_id=pe.person_id AND pi.kind='email'
            ORDER BY pi.normalized_value
          )) AS emails_json,
         (SELECT json_group_array(value) FROM (
            SELECT COALESCE(pi.display_value, pi.normalized_value) AS value
            FROM person_identifiers pi
            WHERE pi.person_id=pe.person_id AND pi.kind='phone'
            ORDER BY pi.normalized_value
          )) AS phones_json,
         pe.parent_id, pe.person_id, '[]' AS children_json
  FROM matched_people mp JOIN people pe USING(person_id)
  JOIN artifacts a ON a.artifact_key=(
    SELECT a2.artifact_key FROM artifacts a2
    WHERE a2.person_id=pe.person_id AND a2.kind='dossier' AND a2.status='projected'
    ORDER BY a2.projected_at DESC, a2.artifact_key LIMIT 1
  )
  WHERE pe.child_slug IS NOT NULL
  UNION ALL
  SELECT 1, mp.match_order, p.display_slug, p.display_name, a.path, a.path,
         json_extract(a.payload_json, '$.body'),
         json_extract(a.payload_json, '$.headline'), p.display_name,
         (SELECT json_group_array(value) FROM (
            SELECT DISTINCT COALESCE(pi.display_value, pi.normalized_value) AS value
            FROM people pe JOIN person_identifiers pi USING(person_id)
            WHERE pe.parent_id=p.parent_id AND pi.kind='email'
            ORDER BY value
          )),
         (SELECT json_group_array(value) FROM (
            SELECT DISTINCT COALESCE(pi.display_value, pi.normalized_value) AS value
            FROM people pe JOIN person_identifiers pi USING(person_id)
            WHERE pe.parent_id=p.parent_id AND pi.kind='phone'
            ORDER BY value
          )),
         p.parent_id, NULL,
         (SELECT json_group_array(child_slug) FROM (
            SELECT child_slug FROM people
            WHERE parent_id=p.parent_id AND child_slug IS NOT NULL ORDER BY child_slug
          ))
  FROM matched_parents mp JOIN parents p USING(parent_id)
  JOIN artifacts a ON a.artifact_key=(
    SELECT a2.artifact_key FROM artifacts a2
    WHERE a2.parent_id=p.parent_id AND a2.person_id IS NULL
      AND a2.candidate_key IS NULL AND a2.kind='dossier' AND a2.status='projected'
    ORDER BY a2.projected_at DESC, a2.artifact_key LIMIT 1
  )
  WHERE p.display_slug IS NOT NULL
)
SELECT * FROM results ORDER BY match_order, entity_kind, slug
""",
        (
            name_key,
            name_key,
            name_key,
            name_key,
            phone_key,
            phone_key,
            email_key,
            email_key,
            name_key,
            *token_params,
            name_key,
            *token_params,
        ),
    )
    result: list[PersonLookupRow | ParentLookupRow] = []
    for row in rows:
        emails = tuple(str(value) for value in _json(row["emails_json"], []))
        phones = tuple(str(value) for value in _json(row["phones_json"], []))
        if row["person_id"]:
            item = PersonLookupRow(
                slug=row["slug"],
                name=row["name"] or "",
                path=row["path"],
                dossier_path=row["dossier_path"],
                dossier_body=row["dossier_body"] or "",
                headline=row["headline"] or "",
                full_name=row["full_name"] or "",
                emails=emails,
                phones=phones,
                parent_id=row["parent_id"],
                person_id=row["person_id"],
            )
        else:
            item = ParentLookupRow(
                slug=row["slug"],
                name=row["name"] or "",
                path=row["path"],
                dossier_path=row["dossier_path"],
                dossier_body=row["dossier_body"] or "",
                headline=row["headline"] or "",
                full_name=row["full_name"] or "",
                emails=emails,
                phones=phones,
                parent_id=row["parent_id"],
                children=tuple(str(value) for value in _json(row["children_json"], [])),
            )
        result.append(item)
    return result


def person_detail(db: Db, slug_or_parent_id: str) -> ParentViewRow | None:
    """One SQL-hydrated parent with the requested projected dossier body."""
    rows = db.query(
        WORTH_CTE
        + PARENT_SELECT.format(
            where=(
                "WHERE p.parent_id=? OR p.display_slug=? OR p.public_identifier=? "
                "OR EXISTS (SELECT 1 FROM people pe WHERE pe.parent_id=p.parent_id "
                "AND (pe.person_id=? OR pe.child_slug=?))"
            )
        ),
        (slug_or_parent_id,) * 5,
    )
    hydrated = _hydrate_parents(db, rows[:1], pending_only=False)
    if not hydrated:
        return None
    child = db.query(
        "SELECT a.path, a.payload_json FROM people pe JOIN artifacts a ON a.person_id=pe.person_id "
        "WHERE a.kind='dossier' AND a.status='projected' "
        "AND (pe.person_id=? OR pe.child_slug=?) "
        "ORDER BY a.projected_at DESC, a.artifact_key LIMIT 1",
        (slug_or_parent_id, slug_or_parent_id),
    )
    if child:
        payload = _json(child[0]["payload_json"], {})
        hydrated[0] = replace(
            hydrated[0],
            dossier_path=child[0]["path"],
            dossier_body=(str(payload.get("body") or "") if isinstance(payload, dict) else ""),
        )
    return hydrated[0]


def avatar_payload(db: Db, row_key: str) -> AvatarPayload | None:
    """Projected image bytes and content type for one LinkedIn candidate."""
    rows = db.query(
        "SELECT a.payload_json FROM links l JOIN artifacts a ON a.candidate_key=l.row_key "
        "WHERE a.kind='avatar' AND a.status='projected' AND l.row_key=? "
        "ORDER BY a.projected_at DESC, a.artifact_key LIMIT 1",
        (row_key,),
    )
    payload = _json(rows[0]["payload_json"], {}) if rows else {}
    if not isinstance(payload, dict) or not payload.get("base64"):
        return None
    return AvatarPayload(
        base64=str(payload["base64"]),
        content_type=str(payload.get("content_type") or "application/octet-stream"),
    )
