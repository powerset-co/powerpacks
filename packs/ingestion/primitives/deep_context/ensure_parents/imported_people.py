"""Parse the imported people boundary once and project it into canonical SQLite.

``people.csv`` is the one live input owned by the import fan-in. This module is
its only Deep Context reader. It converts rows to frozen values at the boundary,
then get-or-creates stable parent ownership before message collection starts.
Everything downstream reads the SQLite projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from packs.ingestion.primitives.common.contact_fields import (
    emails_from_row,
    normalize_email,
    normalize_phone,
    phones_from_row,
)
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.shared.common import slugify
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
from packs.ingestion.primitives.deep_context.db.queries import (
    identifiers as identifier_rows,
    parents as parent_rows,
    people as person_rows,
    sources as source_rows,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import load_assignment
from packs.ingestion.schemas.people_schema import parse_jsonish
from packs.shared.csv_io import CsvIO


@dataclass(frozen=True)
class ImportedPerson:
    """The small part of one fan-in row Deep Context is allowed to consume."""

    person_id: str
    display_name: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    source_channels: tuple[str, ...]
    superseded_person_ids: tuple[str, ...]


def _text(value: object) -> str:
    return str(value or "").strip()


def _superseded(value: object) -> tuple[str, ...]:
    parsed = parse_jsonish(value, [])
    values = parsed if isinstance(parsed, list) else []
    return tuple(
        dict.fromkeys(item for raw in values if (item := _text(raw).lower()) and "/" not in item and "\\" not in item)
    )


def _channels(value: object) -> tuple[str, ...]:
    parsed = parse_jsonish(value, None)
    values = parsed if isinstance(parsed, list) else _text(value).split(",")
    return tuple(dict.fromkeys(item for raw in values if (item := _text(raw))))


def read_imported_people(path: Path) -> tuple[ImportedPerson, ...]:
    """Read the canonical fan-in CSV into one deduplicated typed row per id."""
    if not path.is_file():
        return ()
    combined: dict[str, ImportedPerson] = {}
    for raw in CsvIO.read_dict_rows(path):
        person_id = _text(raw.get("id")).lower()
        if not person_id or "/" in person_id or "\\" in person_id:
            continue
        display_name = _text(raw.get("full_name")) or " ".join(
            filter(None, (_text(raw.get("first_name")), _text(raw.get("last_name"))))
        )
        incoming = ImportedPerson(
            person_id,
            display_name,
            tuple(emails_from_row(raw)),
            tuple(phones_from_row(raw)),
            _channels(raw.get("source_channels")),
            _superseded(raw.get("superseded_person_ids")),
        )
        prior: ImportedPerson | None = combined.get(person_id)
        if prior is None:
            combined[person_id] = incoming
            continue
        combined[person_id] = ImportedPerson(
            person_id,
            incoming.display_name or prior.display_name,
            tuple(dict.fromkeys((*prior.emails, *incoming.emails))),
            tuple(dict.fromkeys((*prior.phones, *incoming.phones))),
            tuple(dict.fromkeys((*prior.source_channels, *incoming.source_channels))),
            tuple(dict.fromkeys((*prior.superseded_person_ids, *incoming.superseded_person_ids))),
        )
    return tuple(combined[key] for key in sorted(combined))


def _components(
    people: tuple[ImportedPerson, ...],
    parent_by_person: dict[str, str],
) -> tuple[tuple[ImportedPerson, ...], ...]:
    """Group input rows that already touch the same identity or parent."""
    owner_by_token: dict[str, int] = {}
    parent = list(range(len(people)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = root(left), root(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for index, person in enumerate(people):
        aliases = (person.person_id, *person.superseded_person_ids)
        tokens = [f"person:{value}" for value in aliases]
        tokens.extend(f"parent:{parent_id}" for value in aliases if (parent_id := parent_by_person.get(value)))
        for token in tokens:
            owner = owner_by_token.setdefault(token, index)
            union(index, owner)
    grouped: dict[int, list[ImportedPerson]] = {}
    for index, person in enumerate(people):
        grouped.setdefault(root(index), []).append(person)
    return tuple(tuple(grouped[key]) for key in sorted(grouped))


def project_imported_people(db: Db, imported: tuple[ImportedPerson, ...]) -> int:
    """Get or create imported people, incrementally joining prior families."""
    if not imported:
        return 0
    existing_people = {row.person_id: row for row in person_rows(db)}
    parent_by_person = {row.person_id: row.parent_id for row in existing_people.values()}
    parent_slugs = {row.parent_id: row.display_slug for row in parent_rows(db)}
    assignment = load_assignment(db)
    target_by_input: dict[str, str] = {}
    component_targets: list[tuple[tuple[ImportedPerson, ...], str, tuple[str, ...]]] = []
    new_parents: list[ParentRow] = []

    for component in _components(imported, parent_by_person):
        aliases = tuple(
            dict.fromkeys(value for person in component for value in (person.person_id, *person.superseded_person_ids))
        )
        touched_parents = tuple(
            dict.fromkeys(parent_by_person[value] for value in aliases if value in parent_by_person)
        )
        child_slugs = tuple(existing_people[value].child_slug for value in aliases if value in existing_people)
        target = assignment.resolve(child_slugs, tuple(person.person_id for person in component))
        if target not in parent_slugs:
            representative = component[0]
            parent = ParentRow(
                target,
                f"parent-worth:{target}",
                representative.display_name,
                slugify(representative.display_name, target),
                source=WriterSource.PARENT_WORTH.value,
                updated_at=now_iso(),
            )
            new_parents.append(parent)
            parent_slugs[target] = parent.display_slug
        component_targets.append((component, target, touched_parents))

    # One projection avoids a full foreign-key audit per new parent on large imports.
    if new_parents:
        db.project_rows(tuple(new_parents))
    for component, target, touched_parents in component_targets:
        for old_parent in touched_parents:
            if old_parent != target:
                db.merge_parents(target, old_parent)
        for person in component:
            target_by_input[person.person_id] = target

    identifiers_by_person: dict[str, dict[tuple[str, str], PersonIdentifierRow]] = {}
    for row in identifier_rows(db):
        identifiers_by_person.setdefault(row.person_id, {})[(row.kind, row.normalized_value)] = row
    sources_by_person: dict[str, dict[str, PersonSourceRow]] = {}
    for row in source_rows(db):
        sources_by_person.setdefault(row.person_id, {})[row.source] = row
    projection_rows: list[PersonRow | PersonIdentifiersProjection | PersonSourcesProjection] = []
    for person in imported:
        prior: PersonRow | None = existing_people.get(person.person_id)
        parent_id = target_by_input[person.person_id]
        child_slug = (
            prior.child_slug
            if prior and prior.child_slug
            else slugify(
                person.display_name,
                person.person_id,
            )
        )
        parent_slug = parent_slugs[parent_id]
        projection_rows.append(
            PersonRow(
                person.person_id,
                parent_id,
                child_slug,
                parent_slug,
                (prior.display_name if prior else "") or person.display_name,
                prior.is_owner if prior else False,
                prior.is_ghost if prior else False,
                prior.facts_json if prior else None,
                prior.confidence if prior else None,
                now_iso(),
            )
        )
        identifiers = identifiers_by_person.setdefault(person.person_id, {})
        for kind, values, normalize in (
            (IdentifierKind.EMAIL.value, person.emails, normalize_email),
            (IdentifierKind.PHONE.value, person.phones, normalize_phone),
        ):
            for display in values:
                normalized = normalize(display)
                if normalized:
                    identifiers[(kind, normalized)] = PersonIdentifierRow(
                        person.person_id,
                        kind,
                        normalized,
                        display,
                    )
        projection_rows.append(
            PersonIdentifiersProjection(
                person.person_id,
                tuple(identifiers[key] for key in sorted(identifiers)),
            )
        )
        sources = sources_by_person.setdefault(person.person_id, {})
        for source in person.source_channels:
            sources[source] = PersonSourceRow(person.person_id, source)
        projection_rows.append(
            PersonSourcesProjection(
                person.person_id,
                tuple(sources[key] for key in sorted(sources)),
            )
        )
    db.project_rows(tuple(projection_rows))
    return len(imported)
