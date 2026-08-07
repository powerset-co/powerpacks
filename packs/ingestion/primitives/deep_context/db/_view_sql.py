"""SQL relations shared by the Deep Context review projections."""

from __future__ import annotations


WORTH_CTE = """
WITH eligible_links AS (
  SELECT l.* FROM links l
  -- Owner-only candidates are not review targets, while a mixed family stays
  -- visible through its non-owner member.
  WHERE NOT EXISTS (
    SELECT 1 FROM candidate_people cp WHERE cp.row_key=l.row_key
  ) OR EXISTS (
    SELECT 1 FROM candidate_people cp JOIN people pe USING(person_id)
    WHERE cp.row_key=l.row_key AND pe.is_owner=0
  )
), ranked_facts AS (
  SELECT f.*,
         row_number() OVER (
           PARTITION BY f.parent_id
           -- Merges can leave several child facts on one parent. The most
           -- promising verdict keeps the family reviewable; the subject key
           -- makes equal verdicts deterministic across runs.
           ORDER BY CASE f.machine_worth WHEN 'yes' THEN 2 WHEN 'no' THEN 0 ELSE 1 END DESC,
                    COALESCE(f.person_id, f.subject_key)
         ) AS worth_rank
  FROM facts f
  -- Owner facts describe the reviewer, not a contact, and cannot classify a
  -- different member of an owner-touching family.
  WHERE NOT EXISTS (
      SELECT 1 FROM people fact_person
      WHERE fact_person.person_id=f.person_id AND fact_person.is_owner=1
    )
), worth AS (
  SELECT p.parent_id, p.public_identifier, p.display_name, p.display_slug,
         p.human_worth, p.human_worth_note, p.human_worth_source, p.human_worth_at,
         COALESCE(r.machine_worth, 'maybe') AS machine_worth,
         COALESCE(r.machine_worth_reason, '') AS machine_worth_reason,
         CASE WHEN r.machine_worth IS NULL THEN 'default' ELSE 'llm' END AS machine_source,
         COALESCE(p.human_worth, r.machine_worth, 'maybe') AS effective_worth,
         (SELECT json_group_array(person_id) FROM (
            SELECT person_id FROM people
            WHERE parent_id=p.parent_id AND is_owner=0
            ORDER BY person_id
          )) AS person_ids_json,
         EXISTS(
           SELECT 1 FROM eligible_links l
           WHERE l.parent_id=p.parent_id AND l.kind='synthetic'
         ) AS has_synthetic
  FROM parents p
  JOIN ranked_facts r ON r.parent_id=p.parent_id AND r.worth_rank=1
  -- Empty, ghost-only, and owner-only families cannot enter review; an owner
  -- person never hides a real non-owner member of the same family.
  WHERE EXISTS (
    SELECT 1 FROM people pe
    WHERE pe.parent_id=p.parent_id AND pe.is_owner=0 AND pe.is_ghost=0
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
     AND l.machine_reject!='yes'
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
  FROM eligible_links l
), identity_scope AS (
  SELECT p.parent_id
  FROM parents p
  JOIN worth w USING(parent_id)
  -- A machine No normally suppresses identity work, but an imported LinkedIn
  -- identity or prior human keep remains reviewable until a human says No.
  WHERE (
      w.effective_worth!='no'
      OR (
        p.human_worth IS NULL
        AND (
          EXISTS (
            SELECT 1 FROM candidate_policy kept
            WHERE kept.parent_id=p.parent_id
              AND kept.decision_approved='yes'
              AND kept.decision_action NOT IN ('detach', 'exclude')
          )
          OR EXISTS (
            SELECT 1 FROM people connected
            JOIN person_sources ps USING(person_id)
            WHERE connected.parent_id=p.parent_id
              AND connected.is_owner=0
              AND ps.source='linkedin_csv'
          )
        )
      )
    )
    -- Raw imports are source prerequisites, never review cards; wait until the
    -- candidate projection has normalized them.
    AND NOT EXISTS (
      SELECT 1 FROM candidate_policy raw
      WHERE raw.parent_id=p.parent_id AND raw.raw_import=1
    )
    -- A rejected synthetic-only family has no alternate identity to review;
    -- serving it again would create an endless pending card.
    AND NOT (
      NOT EXISTS (
        SELECT 1 FROM candidate_policy real
        WHERE real.parent_id=p.parent_id AND real.kind!='synthetic'
      )
      AND EXISTS (
        SELECT 1 FROM candidate_policy rejected
        WHERE rejected.parent_id=p.parent_id AND rejected.kind='synthetic'
          AND rejected.decision_action='detach'
          AND rejected.decision_approved IN ('yes', 'no')
      )
    )
    -- Parent existence alone cannot invent a card: require an actionable
    -- candidate, recorded decision, or observed candidate-person origin.
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
                 AND origin.is_owner=0
                 AND origin.person_id LIKE 'candidate:%'
             ))
    )
    -- Ghost and owner membership are bookkeeping; at least one real non-owner
    -- child must remain reachable from this parent.
    AND EXISTS (
      SELECT 1 FROM people member
      WHERE member.parent_id=p.parent_id
        AND member.is_owner=0
        AND member.is_ghost=0
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
       w.machine_worth, w.machine_worth_reason, w.machine_source, w.effective_worth,
       p.human_worth, p.human_worth_note, p.human_worth_at,
       w.person_ids_json,
       (SELECT json_group_array(source) FROM (
         SELECT DISTINCT ps.source FROM people pe JOIN person_sources ps USING(person_id)
         WHERE pe.parent_id=p.parent_id AND pe.is_owner=0 ORDER BY ps.source
       )) AS sources_json,
       a.path AS dossier_path,
       COALESCE(json_extract(a.payload_json, '$.body'), '') AS dossier_body
FROM parents p
JOIN worth w USING(parent_id)
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
       CASE WHEN c.kind='synthetic' THEN 'synthetic'
            WHEN r.candidate_key IS NOT NULL THEN 'research'
            ELSE 'attached' END AS profile_source,
       sp.profile_json AS synthetic_profile_json,
       r.result_json AS research_json,
       (SELECT json_group_array(value) FROM (
          SELECT DISTINCT COALESCE(pi.display_value, pi.normalized_value) AS value
          FROM candidate_people cp JOIN people pe USING(person_id)
          JOIN person_identifiers pi USING(person_id)
          WHERE cp.row_key=c.row_key AND pe.is_owner=0 AND pi.kind='email' ORDER BY value
        )) AS emails_json,
       (SELECT json_group_array(value) FROM (
          SELECT DISTINCT COALESCE(pi.display_value, pi.normalized_value) AS value
          FROM candidate_people cp JOIN people pe USING(person_id)
          JOIN person_identifiers pi USING(person_id)
          WHERE cp.row_key=c.row_key AND pe.is_owner=0 AND pi.kind='phone' ORDER BY value
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
