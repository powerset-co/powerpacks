"""Preserve original source contacts before the fan-in's LinkedIn grouping."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.models import (
    IdentifierKind,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
    PersonSourceRow,
    PersonSourcesProjection,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import mint_parent_id
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import (
    ImportedPerson,
    _imported_people,
    _is_shared_mailbox,
    _superseded,
)
from packs.ingestion.primitives.deep_context.shared.common import slugify
from packs.ingestion.primitives.imports.merge_people import MergePeopleManifest
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.shared.csv_io import CsvIO


def read_source_people(people_csv: Path) -> tuple[ImportedPerson, ...]:
    """Read only actual inputs named by the corresponding typed fan-in manifest."""
    manifest_path = people_csv.parent / "manifest.json"
    if not manifest_path.is_file():
        return ()
    payload = json.loads(manifest_path.read_text())
    if payload.get("stage") != "merge_people":
        return ()
    manifest = MergePeopleManifest.model_validate(payload, extra="ignore")
    rows = []
    for source in manifest.input.people_csvs:
        if source not in manifest.stats.input_rows:
            continue
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"fan-in source people CSV missing: {path}")
        for raw in CsvIO.read_dict_rows(path):
            row = PeopleRow.model_validate(raw)
            if row.id.startswith("candidate:"):
                rows.append(row)
    return tuple(person for person in _imported_people(tuple(rows)) if not _is_shared_mailbox(person))


def project_source_people(db: Db, people: tuple[ImportedPerson, ...], imported: tuple[ImportedPerson, ...]) -> None:
    """Keep source ownership under the original contact id on cold and warm imports."""
    if not people:
        return
    existing = {row.person_id: row for row in queries.people(db)}
    parents = {row.parent_id: row for row in queries.parents(db)}
    source_parent = {}
    for row in imported:
        aliases = (row.person_id, *row.superseded_person_ids)
        parent_id = next((existing[value].parent_id for value in aliases if value in existing),
                         mint_parent_id((row.person_id,)))
        source_parent.update((value, parent_id) for value in aliases)
    identifiers: dict[str, dict[tuple[str, str], PersonIdentifierRow]] = {}
    for row in queries.identifiers(db):
        identifiers.setdefault(row.person_id, {})[(row.kind, row.normalized_value)] = row
    sources: dict[str, dict[str, PersonSourceRow]] = {}
    for row in queries.sources(db):
        sources.setdefault(row.person_id, {})[row.source] = row
    projections = []
    for person in people:
        prior = existing.get(person.person_id)
        parent_id = prior.parent_id if prior else source_parent.get(person.person_id, mint_parent_id((person.person_id,)))
        if parent_id not in parents:
            parent = ParentRow(parent_id, f"parent-worth:{parent_id}", person.display_name,
                               slugify(person.display_name, parent_id),
                               source=WriterSource.PARENT_WORTH.value, updated_at=now_iso())
            parents[parent_id] = parent
            projections.append(parent)
        projection = (
            replace(prior, display_name=person.display_name or prior.display_name, updated_at=now_iso())
            if prior else PersonRow(person.person_id, parent_id, slugify(person.display_name, person.person_id),
                                     parents[parent_id].display_slug, person.display_name, updated_at=now_iso())
        )
        projections.append(projection)
        owned = identifiers.setdefault(person.person_id, {})
        for kind, values in ((IdentifierKind.EMAIL.value, person.emails), (IdentifierKind.PHONE.value, person.phones)):
            for value in values:
                owned[(kind, value)] = PersonIdentifierRow(person.person_id, kind, value, value)
        projections.append(PersonIdentifiersProjection(person.person_id, tuple(owned.values())))
        channels = sources.setdefault(person.person_id, {})
        for channel in person.source_channels:
            channels[channel] = PersonSourceRow(person.person_id, channel)
        projections.append(PersonSourcesProjection(person.person_id, tuple(channels.values())))
    db.project_rows(tuple(projections))


def retain_source_identifiers(db: Db, people: tuple[ImportedPerson, ...]) -> None:
    """Remove copied lookup keys from an aggregate when its source contact owns them."""
    if not people:
        return
    source_ids = {person.person_id for person in people}
    aggregate_ids = set()
    for row in queries.imported_people(db):
        aliases = _superseded(row.superseded_person_ids)
        if not source_ids.intersection(aliases):
            continue
        aggregate_ids.update(value for value in (row.id, *aliases)
                             if value not in source_ids and not value.startswith("candidate:"))
    parent_of = {row.person_id: row.parent_id for row in queries.people(db)}
    identifiers = queries.identifiers(db)
    represented = {(parent_of[row.person_id], row.kind, row.normalized_value)
                   for row in identifiers if row.person_id in source_ids}
    by_person: dict[str, list[PersonIdentifierRow]] = {}
    for row in identifiers:
        if row.person_id not in aggregate_ids:
            continue
        by_person.setdefault(row.person_id, [])
        if (parent_of[row.person_id], row.kind, row.normalized_value) not in represented:
            by_person[row.person_id].append(row)
    db.project_rows(tuple(PersonIdentifiersProjection(person_id, tuple(rows))
                          for person_id, rows in by_person.items()))
