#!/usr/bin/env python3
"""Pure reconcile: eligible people + cloud state -> UploadPlan.

Eligible rows with a LinkedIn slug become persons candidates. Desired source rows replace this
operator's stale powerpacks rows; namespace writes use the resulting allowed_operator_ids.
No IO. Every bucket is sorted so two runs on the same state emit the same plan.
"""

from __future__ import annotations

from packs.indexing.primitives.upload_powerset.models import (
    CloudState,
    LocalPerson,
    NamespacePlan,
    SourceRow,
    UploadPlan,
)
from packs.indexing.primitives.upload_powerset.turbopuffer_writer import NAMESPACES
from packs.ingestion.primitives.share.models import PersonLabelRow


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
    share_rows: tuple[PersonLabelRow, ...],
    people: dict[str, LocalPerson],
    cloud: CloudState,
    namespace_names: dict[str, str],
    company_ids_by_person: dict[str, tuple[str, ...]],
    school_ids_by_person: dict[str, tuple[str, ...]],
) -> UploadPlan:
    persons_upsert = sorted(row.person_id for row in share_rows if row.public_identifier)
    skipped_no_linkedin = sorted(row.person_id for row in share_rows if not row.public_identifier)
    # Everything the cloud keys by person (source rows, documents) uses the
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
    existing_sources = {row.key: row for row in cloud.operator_sources}
    sources_insert = tuple(sorted(
        (row for row in desired if (row.key in existing_sources or row.key not in cloud.operator_source_keys)
         and existing_sources.get(row.key) != row),
        key=lambda row: row.key,
    ))

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
        referenced = {entity_id for person_id in persons_upsert for entity_id in by_person.get(person_id, ())}
        missing = referenced - cloud.present_entity_ids.get(logical, frozenset())
        namespaces.append(NamespacePlan(logical, namespace_names[logical], tuple(sorted(missing)), ()))

    return UploadPlan(
        operator_id=operator_id,
        persons_upsert=tuple(persons_upsert),
        skipped_no_linkedin=tuple(skipped_no_linkedin),
        sources_insert=sources_insert,
        sources_delete=sources_delete,
        namespaces=tuple(namespaces),
        allowed_operator_ids=allowed,
        cloud_id_by_person=cloud_id,
    )
