#!/usr/bin/env python3
"""Postgres side of the Powerset upload: every statement takes a cursor.

Flow: resolve_operator_id (credentials JWT sub -> users.id) -> the four read
helpers the plan needs (the cloud id of each of our people it already has, this
operator's own powerpacks source rows, every operator that can see those people,
this operator's private contact_tags) -> the four writes (persons upsert,
operator_person_sources upsert/delete, contact_tags put/delete).

PERSONS_UPSERT_SQL has the column list of the cloud pipeline's upsert
(network-search-api/data_pipeline_v2/pipelines/people/processing/
sync_persons_to_supabase.py) with the COALESCE turned around: the cloud owns a
person it already has, so an existing row keeps every non-NULL cloud value and
a laptop only fills the gaps — COALESCE(persons.col, EXCLUDED.col). A person the
cloud lacks is created from the local profile.

Changelog:
  2026-09-24: created.
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

PERSONS_UPSERT_SQL = """
    INSERT INTO persons (
        id, public_identifier, public_profile_url, first_name, last_name, full_name,
        headline, summary, profile_picture_url, city, state, country, location_raw,
        enrichment_provider, provider_entity_urn, hydrated_context,
        x_twitter_handle, x_twitter_followers, linkedin_followers,
        linkedin_connections, ig_handle, ig_followers,
        inferred_birth_year, linkedin_member_id, twitter_user_id,
        created_at, updated_at
    ) VALUES (
        %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s,
        %s, %s, %s,
        %s, %s, %s,
        %s, %s, %s,
        NOW(), NOW()
    )
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
        enrichment_provider = COALESCE(persons.enrichment_provider, EXCLUDED.enrichment_provider),
        provider_entity_urn = COALESCE(persons.provider_entity_urn, EXCLUDED.provider_entity_urn),
        hydrated_context = COALESCE(persons.hydrated_context, EXCLUDED.hydrated_context),
        x_twitter_handle = COALESCE(persons.x_twitter_handle, EXCLUDED.x_twitter_handle),
        x_twitter_followers = COALESCE(persons.x_twitter_followers, EXCLUDED.x_twitter_followers),
        linkedin_followers = COALESCE(persons.linkedin_followers, EXCLUDED.linkedin_followers),
        linkedin_connections = COALESCE(persons.linkedin_connections, EXCLUDED.linkedin_connections),
        ig_handle = COALESCE(persons.ig_handle, EXCLUDED.ig_handle),
        ig_followers = COALESCE(persons.ig_followers, EXCLUDED.ig_followers),
        inferred_birth_year = COALESCE(persons.inferred_birth_year, EXCLUDED.inferred_birth_year),
        linkedin_member_id = COALESCE(persons.linkedin_member_id, EXCLUDED.linkedin_member_id),
        twitter_user_id = COALESCE(persons.twitter_user_id, EXCLUDED.twitter_user_id),
        updated_at = NOW()
"""

SOURCES_UPSERT_SQL = """
    INSERT INTO operator_person_sources (
        operator_id, person_id, source_channel, source_identifier, discovery_method,
        total_interactions, last_interaction_at, discovered_at, created_at, updated_at
    ) VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, NOW(), NOW(), NOW())
    ON CONFLICT (operator_id, person_id, source_channel, source_identifier) DO UPDATE SET
        total_interactions = EXCLUDED.total_interactions,
        last_interaction_at = EXCLUDED.last_interaction_at,
        updated_at = NOW()
    WHERE operator_person_sources.discovery_method = EXCLUDED.discovery_method
"""

SOURCES_DELETE_SQL = """
    DELETE FROM operator_person_sources
    WHERE operator_id = %s
      AND discovery_method = %s
      AND person_id = %s::uuid
      AND source_channel = %s
      AND source_identifier = %s
"""

TAG_PUT_SQL = """
    INSERT INTO contact_tags (operator_id, group_key, tag, person_id)
    VALUES (%s::uuid, %s, %s, %s::uuid)
    ON CONFLICT (operator_id, group_key, tag)
    DO UPDATE SET person_id = COALESCE(EXCLUDED.person_id, contact_tags.person_id)
"""

TAG_DELETE_SQL = """
    DELETE FROM contact_tags
    WHERE operator_id::text = %s
      AND group_key = %s
      AND tag = %s
"""


def resolve_operator_id(cur: Any, subject: str) -> str:
    cur.execute("SELECT id::text FROM users WHERE user_id = %s", (subject,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError("no users row for the current Powerset credentials; run `$powerset login`")
    return str(row[0])


def fetch_cloud_ids_by_slug(cur: Any, slugs: Sequence[str]) -> dict[str, str]:
    """persons.id per public_identifier — the cloud's unique key, so a person the
    cloud minted under its own id is still found."""
    cur.execute("SELECT public_identifier, id::text FROM persons WHERE public_identifier = ANY(%s)", (list(slugs),))
    return {str(row[0]).lower(): str(row[1]) for row in cur.fetchall()}


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
    for profile in profiles:
        cur.execute(PERSONS_UPSERT_SQL, (
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
        ))
    return len(profiles)


def upsert_sources(cur: Any, operator_id: str, rows: Sequence[SourceRow]) -> int:
    for row in rows:
        cur.execute(SOURCES_UPSERT_SQL, (
            operator_id,
            row.person_id,
            row.source_channel,
            row.source_identifier,
            DISCOVERY_METHOD,
            row.total_interactions,
            row.last_interaction_at or None,
        ))
    return len(rows)


def delete_sources(cur: Any, operator_id: str, rows: Sequence[SourceRow]) -> int:
    for row in rows:
        cur.execute(SOURCES_DELETE_SQL, (
            operator_id, DISCOVERY_METHOD, row.person_id, row.source_channel, row.source_identifier,
        ))
    return len(rows)


def put_tags(cur: Any, operator_id: str, rows: Sequence[TagRow]) -> int:
    for row in rows:
        cur.execute(TAG_PUT_SQL, (operator_id, row.group_key, row.tag, row.person_id or None))
    return len(rows)


def delete_tags(cur: Any, operator_id: str, rows: Sequence[TagRow]) -> int:
    for row in rows:
        cur.execute(TAG_DELETE_SQL, (operator_id, row.group_key, row.tag))
    return len(rows)
