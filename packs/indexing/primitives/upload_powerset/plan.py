#!/usr/bin/env python3
"""Pure reconcile: local share list + cloud state -> UploadPlan.

Flow: share rows split into shared/private -> shared people with a LinkedIn slug
become the persons upsert (the rest are skipped_no_linkedin, a cloud NOT NULL
constraint, not a bug) -> desired operator_person_sources rows are one per
(person, cloud channel) -> the operator's stale powerpacks rows become deletes ->
allowed_operator_ids is recomputed for every person the run touches -> people new
to the cloud get full TurboPuffer upserts, people already there get an
allowed_operator_ids patch only -> private-reason people in the cloud get a
contact_tags row; a cloud tag is dropped only where the human tagged `share`.

No IO. Every bucket is sorted so two runs on the same state emit the same plan.

Changelog:
  2026-09-24: created; namespace grain and share reasons use shared contracts.
"""

from __future__ import annotations

from packs.indexing.primitives.upload_powerset.models import (
    CloudState,
    LocalPerson,
    NamespacePlan,
    PRIVATE_TAG,
    SourceRow,
    TagRow,
    UploadPlan,
)
from packs.indexing.primitives.upload_powerset.turbopuffer_writer import NAMESPACES
from packs.ingestion.schemas.share_schema import HUMAN_SHARE, PRIVATE_REASONS, ShareRow


def _desired_sources(person: LocalPerson, cloud_id: str) -> list[SourceRow]:
    rows = []
    for channel in person.channels:
        identifier = person.source_identifier(channel)
        if not identifier:
            continue
        rows.append(SourceRow(
            person_id=cloud_id,
            source_channel=channel,
            source_identifier=identifier,
            # people_schema.parse_interaction_counts writes cloud channel keys
            # (gmail, imessage, whatsapp), so no second mapping belongs here.
            total_interactions=person.interaction_counts.get(channel, 0),
            last_interaction_at=person.last_interaction,
        ))
    return rows


def build_plan(
    *,
    operator_id: str,
    share_rows: tuple[ShareRow, ...],
    people: dict[str, LocalPerson],
    cloud: CloudState,
    namespace_names: dict[str, str],
    company_ids_by_person: dict[str, tuple[str, ...]],
    school_ids_by_person: dict[str, tuple[str, ...]],
) -> UploadPlan:
    shared = [row for row in share_rows if row.share]
    persons_upsert = sorted(row.person_id for row in shared if row.public_identifier)
    skipped_no_linkedin = sorted(row.person_id for row in shared if not row.public_identifier)
    # Everything the cloud keys by person (source rows, tags, documents) uses the
    # cloud's id; a person the cloud lacks is created under the local id.
    cloud_id = {person_id: cloud.cloud_id_by_person.get(person_id, person_id) for person_id in persons_upsert}

    desired: list[SourceRow] = []
    for person_id in persons_upsert:
        desired.extend(_desired_sources(people[person_id], cloud_id[person_id]))
    desired_keys = {row.key for row in desired}
    sources_delete = tuple(sorted(
        (row for row in cloud.operator_sources if row.key not in desired_keys),
        key=lambda row: row.key,
    ))
    sources_insert = tuple(sorted(desired, key=lambda row: row.key))

    shared_cloud_ids = set(cloud_id.values())
    unshared = sorted({row.person_id for row in sources_delete} - shared_cloud_ids)
    allowed: dict[str, tuple[str, ...]] = {}
    for person_id in shared_cloud_ids:
        allowed[person_id] = tuple(sorted(set(cloud.operator_ids_by_person.get(person_id, ())) | {operator_id}))
    for person_id in unshared:
        allowed[person_id] = tuple(sorted(set(cloud.operator_ids_by_person.get(person_id, ())) - {operator_id}))

    new_to_cloud = sorted(set(persons_upsert) - set(cloud.cloud_id_by_person))
    already_in_cloud = sorted(
        {cloud_id[person_id] for person_id in persons_upsert if person_id in cloud.cloud_id_by_person} | set(unshared)
    )
    entity_ids_by_person = {"companies": company_ids_by_person, "schools": school_ids_by_person}
    namespaces = []
    for namespace in NAMESPACES:
        logical = namespace.logical
        if namespace.person_grain:
            namespaces.append(NamespacePlan(logical, namespace_names[logical],
                                            tuple(new_to_cloud), tuple(already_in_cloud)))
            continue
        by_person = entity_ids_by_person[logical]
        referenced = {entity_id for person_id in new_to_cloud for entity_id in by_person.get(person_id, ())}
        missing = referenced - cloud.present_entity_ids.get(logical, frozenset())
        namespaces.append(NamespacePlan(logical, namespace_names[logical], tuple(sorted(missing)), ()))

    private_rows = [row for row in share_rows if row.reason in PRIVATE_REASONS and row.public_identifier]
    tags_put = tuple(sorted(
        (TagRow(cloud.cloud_id_by_person[row.person_id], row.public_identifier, PRIVATE_TAG)
         for row in private_rows if row.person_id in cloud.cloud_id_by_person),
        key=lambda row: row.group_key,
    ))
    # A cloud private tag is a human decision (the Powerset UI); it yields only
    # to the human's local word, never to a machine default.
    human_shared_keys = {row.public_identifier for row in share_rows if row.reason == HUMAN_SHARE}
    tags_delete = tuple(sorted(
        (TagRow("", group_key, PRIVATE_TAG) for group_key in cloud.private_tag_keys & human_shared_keys),
        key=lambda row: row.group_key,
    ))

    return UploadPlan(
        operator_id=operator_id,
        persons_upsert=tuple(persons_upsert),
        skipped_no_linkedin=tuple(skipped_no_linkedin),
        sources_insert=sources_insert,
        sources_delete=sources_delete,
        namespaces=tuple(namespaces),
        allowed_operator_ids=allowed,
        tags_put=tags_put,
        tags_delete=tags_delete,
    )
