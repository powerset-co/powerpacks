"""Projected dossier lookup, person detail, and avatar reads."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
from packs.ingestion.primitives.common.contact_fields import normalize_email
from packs.ingestion.primitives.deep_context.shared.common import normalize_name, phone_digits
from packs.ingestion.primitives.deep_context.db._view_rows import (
    _hydrate_parents,
    _json,
)
from packs.ingestion.primitives.deep_context.db._view_sql import PARENT_SELECT, WORTH_CTE
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import (
    ParentLookupRow,
    ParentViewRow,
)


def person_lookup(
    db: Path,
    *,
    name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    parent_id: str | None = None,
) -> list[ParentLookupRow]:
    """Resolve to parents; read all saved parent/child dossiers after selection."""
    name_key = normalize_name(name or "")
    tokens = sorted(set(name_key.split()))
    token_sql = " AND ".join(f"instr(name, :token{i})>0" for i in range(len(tokens))) or "0"
    params = dict(name=name_key, phone=phone_digits(phone or ""),
                  email=normalize_email(email or ""), parent_id=parent_id or "")
    params.update({f"token{i}": token for i, token in enumerate(tokens)})
    try:
        with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.create_function("normalize_name", 1, normalize_name)
            rows = conn.execute(
                f"""
WITH names AS (
  SELECT parent_id, normalize_name(display_name) AS name FROM parents
  UNION ALL
  SELECT parent_id, normalize_name(display_name) FROM people
), exact_names AS (
  SELECT parent_id FROM names WHERE :name!='' AND name=:name
), phone_digits AS (
  SELECT person_id, replace(normalized_value, '+', '') AS digits
  FROM person_identifiers WHERE kind='phone'
), matched_raw AS (
  SELECT parent_id, 0 AS match_order FROM parents
  WHERE :parent_id!='' AND parent_id=:parent_id
  UNION ALL
  SELECT pe.parent_id, 10 FROM phone_digits pi JOIN people pe USING(person_id)
  WHERE :parent_id='' AND :phone!=''
    AND CASE WHEN length(digits)=11 AND substr(digits, 1, 1)='1'
             THEN substr(digits, 2) ELSE digits END=:phone
  UNION ALL
  SELECT pe.parent_id, 20 FROM person_identifiers pi JOIN people pe USING(person_id)
  WHERE :parent_id='' AND :email!='' AND pi.kind='email' AND pi.normalized_value=:email
  UNION ALL
  SELECT parent_id, 30 FROM exact_names WHERE :parent_id=''
  UNION ALL
  SELECT parent_id, 30 FROM names
  WHERE :parent_id='' AND :name!='' AND NOT EXISTS (SELECT 1 FROM exact_names)
    AND {token_sql}
), matched AS (
  SELECT parent_id, min(match_order) AS match_order FROM matched_raw GROUP BY parent_id
)
SELECT p.parent_id, p.display_name AS name, p.display_slug AS slug, a.path,
       CASE WHEN (SELECT count(*) FROM matched)=1
            THEN (SELECT group_concat(body, char(10)||char(10)) FROM (
              SELECT json_extract(d.payload_json, '$.body') AS body
              FROM artifacts d
              WHERE d.parent_id=p.parent_id AND d.kind='dossier' AND d.status='projected'
                AND d.candidate_key IS NULL AND json_extract(d.payload_json, '$.body')!=''
              GROUP BY body
              ORDER BY min(d.person_id IS NOT NULL), min(d.artifact_key)
            )) ELSE '' END AS body,
       COALESCE(json_extract(a.payload_json, '$.headline'),
         (SELECT json_extract(i.row_json, '$.headline')
          FROM imported_people i JOIN people pe USING(person_id)
          WHERE pe.parent_id=p.parent_id AND json_extract(i.row_json, '$.headline')!=''
          ORDER BY pe.person_id LIMIT 1), '') AS headline,
       (SELECT json_group_array(value) FROM (
          SELECT DISTINCT COALESCE(pi.display_value, pi.normalized_value) AS value
          FROM people pe JOIN person_identifiers pi USING(person_id)
          WHERE pe.parent_id=p.parent_id AND pi.kind='email' ORDER BY value
        )) AS emails_json,
       (SELECT json_group_array(value) FROM (
          SELECT DISTINCT COALESCE(pi.display_value, pi.normalized_value) AS value
          FROM people pe JOIN person_identifiers pi USING(person_id)
          WHERE pe.parent_id=p.parent_id AND pi.kind='phone' ORDER BY value
        )) AS phones_json,
       (SELECT json_group_array(url) FROM (
          SELECT json_extract(i.row_json, '$.linkedin_url') AS url
          FROM imported_people i JOIN people pe USING(person_id)
          WHERE pe.parent_id=p.parent_id
          UNION
          SELECT pi.normalized_value FROM people pe JOIN person_identifiers pi USING(person_id)
          WHERE pe.parent_id=p.parent_id AND pi.kind='linkedin'
        ) WHERE url IS NOT NULL AND url!='' ORDER BY url) AS linkedin_urls_json
FROM matched mp JOIN parents p USING(parent_id)
LEFT JOIN artifacts a ON a.artifact_key='dossier:'||p.parent_id
  AND a.kind='dossier' AND a.status='projected'
  AND a.person_id IS NULL AND a.candidate_key IS NULL
ORDER BY mp.match_order, p.display_name, p.parent_id
""",
                params,
            ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError(f"Cannot read Deep Context database: {db}: {exc}") from exc
    return [
        ParentLookupRow(
            parent_id=row["parent_id"], name=row["name"] or "", slug=row["slug"] or "",
            dossier_path=row["path"] or "", dossier_body=row["body"] or "",
            headline=row["headline"],
            emails=tuple(_json(row["emails_json"], [])),
            phones=tuple(_json(row["phones_json"], [])),
            linkedin_urls=tuple(_json(row["linkedin_urls_json"], [])),
        )
        for row in rows
    ]


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
