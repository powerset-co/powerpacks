"""SQL relations shared by the Deep Context review projections."""

from __future__ import annotations


WORTH_CTE = """
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


WORTH_SELECT = """
SELECT * FROM worth
{where}
ORDER BY lower(COALESCE(display_name, public_identifier)), parent_id
"""


PENDING_CANDIDATE = """
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


LINKEDIN_CTE = (
    WORTH_CTE
    + """, candidate_policy AS (
  SELECT l.*,
         """
    + PENDING_CANDIDATE
    + """ AS is_pending
  FROM links l
), identity_scope AS (
  SELECT p.parent_id
  FROM parents p
  LEFT JOIN worth w USING(parent_id)
  WHERE (
      COALESCE(w.effective_worth, p.human_worth, p.machine_worth, 'maybe')!='no'
      OR (
        p.human_worth IS NULL
        AND (
          EXISTS (
            SELECT 1 FROM links kept
            WHERE kept.parent_id=p.parent_id
              AND kept.decision_approved='yes'
              AND kept.decision_action NOT IN ('detach', 'exclude')
          )
          OR EXISTS (
            SELECT 1 FROM people connected
            JOIN person_sources ps USING(person_id)
            WHERE connected.parent_id=p.parent_id AND ps.source='linkedin_csv'
          )
        )
      )
    )
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


PARENT_SELECT = """
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
       a.path AS dossier_path,
       COALESCE(json_extract(a.payload_json, '$.body'), '') AS dossier_body
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


CANDIDATE_SELECT = """
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
