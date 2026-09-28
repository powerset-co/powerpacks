#!/usr/bin/env python3
"""Postgres side of the Powerset upload: every statement takes a cursor.

Flow: resolve_operator_id (credentials JWT sub -> users.id) -> the four read
helpers the plan needs (the cloud id of each of our people it already has, this
operator's own powerpacks source rows, every operator that can see those people,
this operator's private contact_tags) -> the four writes (persons upsert,
operator_person_sources upsert/delete, contact_tags put/delete). Each write
sends one multi-row statement per BATCH_ROWS rows and returns the sum of cursor
rowcount, rather than attempted rows.

PERSONS_UPSERT_SQL has the column list of the cloud pipeline's upsert
(network-search-api/data_pipeline_v2/pipelines/people/processing/
sync_persons_to_supabase.py) with the COALESCE turned around: the cloud owns a
person it already has, so an existing row keeps every non-NULL cloud value and
a laptop only fills the gaps — COALESCE(persons.col, EXCLUDED.col). A person the
cloud lacks is created from the local profile. The INSERT column list stays
pinned to that cloud pipeline's list, including five locally NULL columns.

Changelog:
  2026-09-28: a persons row whose context has no positions takes the uploaded one.
  2026-09-28: writes go in multi-row statements of 500 rows, not one per row.
  2026-09-24: created; count affected rows and omit locally NULL update clauses.
"""

from __future__ import annotations

from typing import Any, Sequence

from packs.indexing.primitives.upload_powerset.models import (
    DISCOVERY_METHOD,
    PersonProfile,
    PRIVATE_TAG,
    SourceRow,
    TagRow,
)
from packs.indexing.primitives.upload_powerset.errors import SAFE_ERRORS

# operator_person_sources.operator_id is VARCHAR; contact_tags.operator_id is uuid.

# Rows per write statement: persons' 25 columns make 12,500 bind parameters, far
# under Postgres' 65,535, and one round trip replaces 500.
BATCH_ROWS = 500

PERSONS_UPSERT_SQL = """
    INSERT INTO persons (
        id, public_identifier, public_profile_url, first_name, last_name, full_name,
        headline, summary, profile_picture_url, city, state, country, location_raw,
        enrichment_provider, provider_entity_urn, hydrated_context,
        x_twitter_handle, x_twitter_followers, linkedin_followers,
        linkedin_connections, ig_handle, ig_followers,
        inferred_birth_year, linkedin_member_id, twitter_user_id,
        created_at, updated_at
    ) VALUES {values}
    ON CONFLICT (public_identifier) DO UPDATE SET
        public_profile_url = COALESCE(persons.public_profile_url, EXCLUDED.public_profile_url),
        first_name = COALESCE(persons.first_name, EXCLUDED.first_name),
        last_name = COALESCE(persons.last_name, EXCLUDED.last_name),
        full_name = COALESCE(persons.full_name, EXCLUDED.full_name),
        headline = COALESCE(persons.headline, EXCLUDED.headline),
        summary = COALESCE(persons.summary, EXCLUDED.summary),
        profile_picture_url = COALESCE(persons.profile_picture_url, EXCLUDED.profile_picture_url),
        city = COALESCE(persons.city, EXCLUDED.city),
        state = COALESCE(persons.state, EXCLUDED.state),
        country = COALESCE(persons.country, EXCLUDED.country),
        location_raw = COALESCE(persons.location_raw, EXCLUDED.location_raw),
        -- A context without positions came from a profile-less upload: ours replaces it.
        hydrated_context = CASE
            WHEN COALESCE(persons.hydrated_context -> 'positions', '[]'::jsonb) = '[]'::jsonb
            THEN COALESCE(EXCLUDED.hydrated_context, persons.hydrated_context)
            ELSE persons.hydrated_context END,
        x_twitter_handle = COALESCE(persons.x_twitter_handle, EXCLUDED.x_twitter_handle),
        x_twitter_followers = COALESCE(persons.x_twitter_followers, EXCLUDED.x_twitter_followers),
        linkedin_followers = COALESCE(persons.linkedin_followers, EXCLUDED.linkedin_followers),
        linkedin_connections = COALESCE(persons.linkedin_connections, EXCLUDED.linkedin_connections),
        ig_followers = COALESCE(persons.ig_followers, EXCLUDED.ig_followers),
        inferred_birth_year = COALESCE(persons.inferred_birth_year, EXCLUDED.inferred_birth_year),
        updated_at = NOW()
"""
PERSONS_ROW = "(" + ", ".join(["%s"] * 25) + ", NOW(), NOW())"

SOURCES_UPSERT_SQL = """
    INSERT INTO operator_person_sources (
        operator_id, person_id, source_channel, source_identifier, discovery_method,
        total_interactions, last_interaction_at, discovered_at, created_at, updated_at
    ) VALUES {values}
    ON CONFLICT (operator_id, person_id, source_channel, source_identifier) DO UPDATE SET
        total_interactions = EXCLUDED.total_interactions,
        last_interaction_at = EXCLUDED.last_interaction_at,
        updated_at = NOW()
    WHERE operator_person_sources.discovery_method = EXCLUDED.discovery_method
"""
SOURCES_ROW = "(%s, %s::uuid, %s, %s, %s, %s, %s, NOW(), NOW(), NOW())"

SOURCES_DELETE_SQL = """
    DELETE FROM operator_person_sources s
    USING (VALUES {values}) AS d(person_id, source_channel, source_identifier)
    WHERE s.operator_id = %s
      AND s.discovery_method = %s
      AND s.person_id = d.person_id
      AND s.source_channel = d.source_channel
      AND s.source_identifier = d.source_identifier
"""
SOURCES_DELETE_ROW = "(%s::uuid, %s, %s)"

TAG_PUT_SQL = """
    INSERT INTO contact_tags (operator_id, group_key, tag, person_id)
    VALUES {values}
    ON CONFLICT (operator_id, group_key, tag)
    DO UPDATE SET person_id = COALESCE(EXCLUDED.person_id, contact_tags.person_id)
"""
TAG_PUT_ROW = "(%s::uuid, %s, %s, %s::uuid)"

TAG_DELETE_SQL = """
    DELETE FROM contact_tags c
    USING (VALUES {values}) AS d(group_key, tag)
    WHERE c.operator_id::text = %s
      AND c.group_key = d.group_key
      AND c.tag = d.tag
"""
TAG_DELETE_ROW = "(%s, %s)"


def _write_batches(cur: Any, sql: str, row: str, rows: Sequence[tuple[Any, ...]],
                   trailing: tuple[Any, ...] = ()) -> int:
    """One statement per BATCH_ROWS rows; `trailing` fills the placeholders after VALUES."""
    count = 0
    for start in range(0, len(rows), BATCH_ROWS):
        batch = rows[start:start + BATCH_ROWS]
        cur.execute(sql.format(values=", ".join([row] * len(batch))),
                    tuple(value for values in batch for value in values) + trailing)
        count += cur.rowcount
    return count


def resolve_operator_id(cur: Any, subject: str) -> str:
    cur.execute("SELECT id::text FROM users WHERE user_id = %s", (subject,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError(SAFE_ERRORS["operator"])
    return str(row[0])


def use_v3_schema(cur: Any) -> None:
    """Point this session at the shared schema and prove it took: the standard login works."""
    cur.execute("SET search_path TO powerset_v2, pg_catalog")
    cur.execute("SELECT current_schema(), current_setting('search_path')")
    schema, search_path = cur.fetchone()
    if schema != "powerset_v2" or search_path != "powerset_v2, pg_catalog":
        raise RuntimeError(SAFE_ERRORS["postgres_login"])


def fetch_cloud_ids_by_slug(cur: Any, slugs: Sequence[str]) -> dict[str, str]:
    """persons.id per public_identifier — the cloud's unique key, so a person the
    cloud minted under its own id is still found."""
    cur.execute("SELECT public_identifier, id::text FROM persons WHERE public_identifier = ANY(%s)", (list(slugs),))
    return {str(row[0]).lower(): str(row[1]) for row in cur.fetchall()}


def fetch_ids_without_positions(cur: Any, person_ids: Sequence[str]) -> frozenset[str]:
    """The persons whose hydrated_context has no positions (uploaded before the profile fix)."""
    if not person_ids:
        return frozenset()
    cur.execute("""
        SELECT id::text FROM persons
        WHERE id = ANY(%s::uuid[])
          AND COALESCE(hydrated_context -> 'positions', '[]'::jsonb) = '[]'::jsonb
    """, (list(person_ids),))
    return frozenset(row[0] for row in cur.fetchall())


def fetch_operator_sources(cur: Any, operator_id: str) -> tuple[SourceRow, ...]:
    cur.execute(
        """
        SELECT person_id::text, source_channel, source_identifier,
               total_interactions, last_interaction_at::text
        FROM operator_person_sources
        WHERE operator_id = %s AND discovery_method = %s
        """,
        (operator_id, DISCOVERY_METHOD),
    )
    return tuple(
        SourceRow(str(row[0]), str(row[1]), str(row[2] or ""), int(row[3] or 0), str(row[4] or ""))
        for row in cur.fetchall()
    )


def fetch_operator_source_keys(cur: Any, operator_id: str) -> frozenset[tuple[str, str, str]]:
    cur.execute("""
        SELECT person_id::text, source_channel, source_identifier
        FROM operator_person_sources WHERE operator_id = %s
    """, (operator_id,))
    return frozenset((str(person_id), str(channel), str(identifier))
                     for person_id, channel, identifier in cur.fetchall())


def fetch_operator_ids_by_person(cur: Any, person_ids: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """allowed_operator_ids as the cloud writer derives it: every operator with a
    source row for the person (operator_id is VARCHAR in this table)."""
    cur.execute(
        "SELECT person_id::text, operator_id FROM operator_person_sources WHERE person_id = ANY(%s::uuid[])",
        (list(person_ids),),
    )
    by_person: dict[str, set[str]] = {}
    for person_id, operator_id in cur.fetchall():
        by_person.setdefault(str(person_id), set()).add(str(operator_id))
    return {person_id: tuple(sorted(operators)) for person_id, operators in by_person.items()}


def fetch_private_tag_keys(cur: Any, operator_id: str) -> frozenset[str]:
    cur.execute(
        "SELECT group_key FROM contact_tags WHERE operator_id::text = %s AND tag = %s",
        (operator_id, PRIVATE_TAG),
    )
    return frozenset(str(row[0]) for row in cur.fetchall())


def upsert_persons(cur: Any, profiles: Sequence[PersonProfile]) -> int:
    return _write_batches(cur, PERSONS_UPSERT_SQL, PERSONS_ROW, [(
        profile.id,
        profile.public_identifier,
        profile.public_profile_url,
        profile.first_name,
        profile.last_name,
        profile.full_name,
        profile.headline,
        profile.summary,
        profile.profile_picture_url,
        profile.city,
        profile.state,
        profile.country,
        profile.location_raw,
        None,  # enrichment_provider
        None,  # provider_entity_urn
        profile.hydrated_context,
        profile.x_twitter_handle,
        profile.x_twitter_followers,
        profile.linkedin_followers,
        profile.linkedin_connections,
        None,  # ig_handle
        profile.ig_followers,
        profile.inferred_birth_year,
        None,  # linkedin_member_id
        None,  # twitter_user_id
    ) for profile in profiles])


def upsert_sources(cur: Any, operator_id: str, rows: Sequence[SourceRow]) -> int:
    return _write_batches(cur, SOURCES_UPSERT_SQL, SOURCES_ROW, [(
        operator_id,
        row.person_id,
        row.source_channel,
        row.source_identifier,
        DISCOVERY_METHOD,
        row.total_interactions,
        row.last_interaction_at or None,
    ) for row in rows])


def delete_sources(cur: Any, operator_id: str, rows: Sequence[SourceRow]) -> int:
    return _write_batches(cur, SOURCES_DELETE_SQL, SOURCES_DELETE_ROW, [
        (row.person_id, row.source_channel, row.source_identifier) for row in rows
    ], trailing=(operator_id, DISCOVERY_METHOD))


def put_tags(cur: Any, operator_id: str, rows: Sequence[TagRow]) -> int:
    return _write_batches(cur, TAG_PUT_SQL, TAG_PUT_ROW, [
        (operator_id, row.group_key, row.tag, row.person_id or None) for row in rows
    ])


def delete_tags(cur: Any, operator_id: str, rows: Sequence[TagRow]) -> int:
    return _write_batches(cur, TAG_DELETE_SQL, TAG_DELETE_ROW, [
        (row.group_key, row.tag) for row in rows
    ], trailing=(operator_id,))
