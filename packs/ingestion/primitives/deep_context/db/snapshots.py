"""Migration-proof whole-graph snapshot from canonical Deep Context SQLite."""

from __future__ import annotations

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow,
    CanonicalSnapshot,
    DossierSnapshotRow,
    ParentSnapshotRow,
    PersonIdentifierRow,
    PersonRow,
    PersonSourceRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db


def _dossiers(
    parents: tuple[ParentSnapshotRow, ...],
    people: tuple[PersonRow, ...],
    identifiers: tuple[PersonIdentifierRow, ...],
    sources: tuple[PersonSourceRow, ...],
    artifacts: tuple[ArtifactRow, ...],
) -> tuple[DossierSnapshotRow, ...]:
    values: dict[str, dict[str, list[str]]] = {}
    for row in identifiers:
        display = row.display_value or row.normalized_value
        items = values.setdefault(row.person_id, {}).setdefault(row.kind, [])
        if display not in items:
            items.append(display)
    channels: dict[str, list[str]] = {}
    for row in sources:
        items = channels.setdefault(row.person_id, [])
        if row.source not in items:
            items.append(row.source)
    person_artifacts = {
        row.person_id: row for row in artifacts if row.kind == "dossier" and row.status == "projected" and row.person_id
    }
    parent_artifacts = {
        row.parent_id: row
        for row in artifacts
        if row.kind == "dossier" and row.status == "projected" and not row.person_id and not row.candidate_key
    }

    children = []
    people_by_parent: dict[str, list[PersonRow]] = {}
    for person in people:
        people_by_parent.setdefault(person.parent_id, []).append(person)
        artifact: ArtifactRow | None = person_artifacts.get(person.person_id)
        if artifact is None or not person.child_slug:
            continue
        payload = parse_json_object(artifact.payload_json)
        row = DossierSnapshotRow(
            slug=person.child_slug,
            name=str(payload.get("name") or person.display_name or person.child_slug),
            path=str(payload.get("path") or f"dossiers/{person.child_slug}.md"),
            artifact_path=artifact.path,
            headline=str(payload.get("headline") or ""),
            full_name=str(payload.get("full_name") or person.display_name or ""),
            emails=tuple(values.get(person.person_id, {}).get("email", [])),
            phones=tuple(values.get(person.person_id, {}).get("phone", [])),
            parent_id=person.parent_id,
            person_id=person.person_id,
            body=str(payload.get("body") or ""),
            source_channels=tuple(channels.get(person.person_id, [])),
        )
        children.append(row)

    parent_rows = []
    for parent in parents:
        artifact: ArtifactRow | None = parent_artifacts.get(parent.parent_id)
        people_members = people_by_parent.get(parent.parent_id, [])
        if artifact is None or not parent.display_slug or not people_members:
            continue
        payload = parse_json_object(artifact.payload_json)
        member_ids = {row.person_id for row in people_members}
        emails = tuple(
            dict.fromkeys(
                display for person_id in sorted(member_ids) for display in values.get(person_id, {}).get("email", [])
            )
        )
        phones = tuple(
            dict.fromkeys(
                display for person_id in sorted(member_ids) for display in values.get(person_id, {}).get("phone", [])
            )
        )
        parent_rows.append(
            DossierSnapshotRow(
                slug=parent.display_slug,
                name=str(payload.get("name") or parent.display_name or parent.display_slug),
                path=str(payload.get("path") or f"parents/{parent.display_slug}.md"),
                artifact_path=artifact.path,
                headline=str(payload.get("headline") or ""),
                full_name=str(payload.get("full_name") or parent.display_name or ""),
                emails=emails,
                phones=phones,
                parent_id=parent.parent_id,
                children=tuple(row.child_slug for row in people_members if row.child_slug),
                body=str(payload.get("body") or ""),
                source_channels=tuple(
                    dict.fromkeys(
                        source
                        for row in sorted(people_members, key=lambda member: member.person_id)
                        for source in channels.get(row.person_id, [])
                    )
                ),
            )
        )
    return tuple(sorted(children, key=lambda row: row.slug)) + tuple(sorted(parent_rows, key=lambda row: row.slug))


def canonical_snapshot(db: Db) -> CanonicalSnapshot:
    """Whole graph for the migration proof; steady-state stages use narrow queries.

    Removal countdown (2026-08-06): delete with parent_identity_proof once no
    supported install predates powerpacks v1.19.0.
    """
    parent_rows = queries.parents(db)
    people_rows = queries.people(db)
    identifier_rows = queries.identifiers(db)
    source_rows = queries.sources(db)
    artifact_rows = queries.artifacts(db)
    return CanonicalSnapshot(
        owner=queries.owner_profile(db),
        owner_path=queries.owner_path(db),
        parents=parent_rows,
        people=people_rows,
        identifiers=identifier_rows,
        sources=source_rows,
        artifacts=artifact_rows,
        facts=queries.facts(db),
        dossiers=_dossiers(
            parent_rows,
            people_rows,
            identifier_rows,
            source_rows,
            artifact_rows,
        ),
        merge_verdicts=queries.merge_verdicts(db),
    )
