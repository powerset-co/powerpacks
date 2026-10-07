"""Parse the imported people boundary once and project it into canonical SQLite.

The input boundary converts source rows to frozen values, then get-or-creates
stable parent ownership before message collection starts. Old profile aliases
cannot join contacts; only existing SQLite parent assignments group these rows.
Everything downstream reads the SQLite roster, including the headline used by
the notable-title rule.

Changelog:
  2026-10-01: `read_imported_people` drops shared mailboxes (every email a
      role address, no phone, not a LinkedIn connection). People already in
      the store are carried forward unchanged.
  2026-09-26: the profile cells a person list renders (LinkedIn URL, avatar,
      title, company, location) ride the row; the share UI reads them.
  2026-09-25: `headline` (the imported LinkedIn headline) rides the row; the
      worth stage's notable-title rule reads it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from packs.ingestion.primitives.common.contact_fields import (
    emails_from_row,
    is_shared_mailbox,
    normalize_email,
    normalize_phone,
    phones_from_row,
)
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.shared.common import slugify
from packs.ingestion.primitives.deep_context.db.models import (
    CandidatePeopleProjection,
    CandidatePersonRow,
    IdentifierKind,
    LinkRow,
    RowKind,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
    PersonSourceRow,
    PersonSourcesProjection,
    SourceChannel,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.identity_queries import links, review_rows
from packs.ingestion.primitives.deep_context.db.queries import (
    identifiers as identifier_rows,
    parents as parent_rows,
    people as person_rows,
    sources as source_rows,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.projectors import project_owner_people
from packs.ingestion.primitives.deep_context.db.queries import imported_people as stored_people_rows
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.primitives.imports.merge_people import merge_group
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import mint_parent_id
from packs.ingestion.schemas.people_schema import (
    CONTACT_CARRY_COLUMNS,
    normalize_linkedin_url,
    parse_interaction_counts,
    parse_jsonish,
    row_public_identifier,
)
from packs.shared.csv_io import CsvIO


@dataclass(frozen=True)
class ImportedPerson:
    """The small part of one fan-in row Deep Context is allowed to consume.

    The roster metadata that needs no message bodies rides here too: the share
    stage reads cadence and recency from this one boundary rather than opening
    people.csv a second time.
    """

    person_id: str
    display_name: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    source_channels: tuple[str, ...]
    superseded_person_ids: tuple[str, ...]
    index_row: PeopleRow = field(compare=False)
    public_identifier: str = ""
    interaction_counts: dict[str, int] = field(default_factory=dict)
    last_interaction: str = ""
    headline: str = ""
    linkedin_url: str = ""
    avatar_url: str = ""
    title: str = ""
    company: str = ""
    location: str = ""


def _text(value: object) -> str:
    return str(value or "").strip()


def _superseded(value: object) -> tuple[str, ...]:
    parsed = parse_jsonish(value, [])
    values = parsed if isinstance(parsed, list) else []
    return tuple(
        dict.fromkeys(item for raw in values if (item := _text(raw).lower()) and "/" not in item and "\\" not in item)
    )


def _public_identifier(raw: dict[str, str]) -> str:
    """The row's LinkedIn slug, normalized by the same rules every reader uses."""
    return row_public_identifier(raw).lower()


def _location(raw: dict[str, str]) -> str:
    parts = [part for part in (_text(raw.get("city")), _text(raw.get("state")), _text(raw.get("country"))) if part]
    return ", ".join(dict.fromkeys(parts)) or _text(raw.get("location_raw"))


def _channels(value: object) -> tuple[str, ...]:
    parsed = parse_jsonish(value, None)
    values = parsed if isinstance(parsed, list) else _text(value).split(",")
    return tuple(dict.fromkeys(item for raw in values if (item := _text(raw))))


def _imported_people(rows: tuple[PeopleRow, ...]) -> tuple[ImportedPerson, ...]:
    """Project typed full rows to the fields used within Deep Context."""
    grouped: dict[str, list[PeopleRow]] = {}
    for row in rows:
        person_id = _text(row.id).lower()
        if person_id and "/" not in person_id and "\\" not in person_id:
            grouped.setdefault(person_id, []).append(row.model_copy(update={
                "id": person_id,
                "source_channels": ",".join(_channels(row.source_channels)),
            }))
    combined: dict[str, ImportedPerson] = {}
    for person_id, members in grouped.items():
        row = members[0] if len(members) == 1 else PeopleRow.model_validate(merge_group(person_id, members))
        raw = row.to_row()
        display_name = _text(raw.get("full_name")) or " ".join(
            filter(None, (_text(raw.get("first_name")), _text(raw.get("last_name"))))
        )
        combined[person_id] = ImportedPerson(
            person_id=person_id,
            display_name=display_name,
            emails=tuple(emails_from_row(raw)),
            phones=tuple(phones_from_row(raw)),
            source_channels=_channels(raw.get("source_channels")),
            superseded_person_ids=_superseded(raw.get("superseded_person_ids")),
            public_identifier=_public_identifier(raw),
            interaction_counts=parse_interaction_counts(raw.get("interaction_counts")),
            last_interaction=_text(raw.get("last_interaction")),
            headline=_text(raw.get("headline")),
            linkedin_url=normalize_linkedin_url(_text(raw.get("linkedin_url"))),
            avatar_url=_text(raw.get("profile_picture_url")),
            title=_text(raw.get("current_title")),
            company=_text(raw.get("current_company")),
            location=_location(raw),
            index_row=row,
        )
    return tuple(combined[key] for key in sorted(combined))


def _is_shared_mailbox(person: ImportedPerson) -> bool:
    """Only role addresses, no phone, and not one of the owner's LinkedIn connections.

    A LinkedIn found for the address by a lookup does not make it a person.
    """
    return (
        is_shared_mailbox(person.emails, person.phones)
        and SourceChannel.LINKEDIN not in person.source_channels
    )


def read_imported_people(path: Path) -> tuple[ImportedPerson, ...]:
    """Read the canonical fan-in CSV only at the import boundary.

    Shared mailboxes (`ir@`, `billing@`) are dropped here: a human replies from
    them, so the Gmail import keeps them, but they are not people.
    """
    if not path.is_file():
        return ()
    rows = []
    for raw in CsvIO.read_dict_rows(path):
        if not raw.get("primary_phone"):
            raw["primary_phone"] = raw.get("phone") or raw.get("phone_e164") or ""
        rows.append(PeopleRow.model_validate(raw))
    return tuple(person for person in _imported_people(tuple(rows)) if not _is_shared_mailbox(person))


def stored_imported_people(db: Db) -> tuple[ImportedPerson, ...]:
    """Read the current roster from SQLite for all downstream stages."""
    return _imported_people(stored_people_rows(db))


def _components(
    people: tuple[ImportedPerson, ...],
    parent_by_person: dict[str, str],
) -> tuple[tuple[ImportedPerson, ...], ...]:
    """Group only people already assigned to the same SQLite parent."""
    grouped: dict[str, list[ImportedPerson]] = {}
    for person in people:
        key = parent_by_person.get(person.person_id, person.person_id)
        grouped.setdefault(key, []).append(person)
    return tuple(tuple(group) for group in grouped.values())


def project_imported_people(db: Db, imported: tuple[ImportedPerson, ...]) -> int:
    """Update each imported person without treating old aliases as a merge."""
    if not imported:
        return 0
    incoming_rows = tuple(person.index_row for person in imported)
    current = {row.id: row for row in stored_people_rows(db)}
    incoming_ids = {person.person_id for person in imported}
    combined_rows: list[PeopleRow] = []
    for person in imported:
        source = person.index_row.model_copy(update={
            "id": person.person_id, "full_name": person.display_name,
            "public_identifier": person.public_identifier, "linkedin_url": person.linkedin_url,
            "superseded_person_ids": json.dumps(person.superseded_person_ids),
        })
        canonical_id = person.person_id
        prior = current.get(canonical_id)
        previous = [prior] if prior is not None else []
        if previous:
            previous = [
                PeopleRow.model_validate({
                    column: getattr(row, column)
                    for column in ("id", "superseded_person_ids", "source_artifacts", *CONTACT_CARRY_COLUMNS)
                })
                if source.public_identifier and row.public_identifier and source.public_identifier != row.public_identifier
                else row
                for row in previous
            ]
            merged = merge_group(canonical_id, [source, *previous])
            merged["id"] = canonical_id
            for column in ("full_name", "first_name", "last_name"):
                merged[column] = getattr(source, column)
            merged["superseded_person_ids"] = json.dumps(
                [value for value in _superseded(merged["superseded_person_ids"])
                 if value != canonical_id]
            )
            source = PeopleRow.model_validate(merged)
        combined_rows.append(source)
    combined_rows.extend(row for key, row in current.items()
                         if key not in incoming_ids and not incoming_ids.intersection(_superseded(row.superseded_person_ids)))
    imported = _imported_people(tuple(combined_rows))
    existing_people = {row.person_id: row for row in person_rows(db)}
    parent_by_person = {row.person_id: row.parent_id for row in existing_people.values()}
    parent_slugs = {row.parent_id: row.display_slug for row in parent_rows(db)}
    target_by_input: dict[str, str] = {}
    component_targets: list[tuple[tuple[ImportedPerson, ...], str]] = []
    new_parents: list[ParentRow] = []

    for component in _components(imported, parent_by_person):
        target = parent_by_person.get(component[0].person_id) or mint_parent_id(
            tuple(person.person_id for person in component))
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
        component_targets.append((component, target))

    # One projection avoids a full foreign-key audit per new parent on large imports.
    if new_parents:
        db.project_rows(tuple(new_parents))
    for component, target in component_targets:
        for person in component:
            target_by_input[person.person_id] = target

    identifiers_by_person: dict[str, dict[tuple[str, str], PersonIdentifierRow]] = {}
    for row in identifier_rows(db):
        identifiers_by_person.setdefault(row.person_id, {})[(row.kind, row.normalized_value)] = row
    sources_by_person: dict[str, dict[str, PersonSourceRow]] = {}
    for row in source_rows(db):
        sources_by_person.setdefault(row.person_id, {})[row.source] = row
    existing_links = {row.row_key: row for row in links(db)}
    represented_slugs = {
        (existing_links[row.key].parent_id, row.new_public_identifier or row.public_identifier)
        for row in review_rows(db, include_worth=False)
    }
    projection_rows: list[
        PersonRow | PersonIdentifiersProjection | PersonSourcesProjection | LinkRow | CandidatePeopleProjection
    ] = []
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
                person.display_name,
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
        # Imported assignments are candidates, not approvals. Existing review
        # rows own their verdicts, including retargets realized under a new id.
        slug = person.public_identifier
        url = person.linkedin_url or (f"https://www.linkedin.com/in/{slug}" if slug else "")
        if slug and (parent_id, slug) not in represented_slugs:
            row_key = slug if slug not in existing_links else f"{slug}:{person.person_id}"
            projection_rows.extend((
                LinkRow(row_key, parent_id, slug, RowKind.PUB.value, url, person.display_name,
                        source=WriterSource.RECONCILE.value, updated_at=now_iso()),
                CandidatePeopleProjection(row_key, (CandidatePersonRow(row_key, person.person_id, parent_id),)),
            ))
            represented_slugs.add((parent_id, slug))
            existing_links[row_key] = projection_rows[-2]
        if not slug and person.person_id.startswith("candidate:") and person.person_id not in existing_links:
            kind = RowKind.CANDIDATE_EMAIL if ":email:" in person.person_id else RowKind.CANDIDATE_PHONE
            projection_rows.extend((
                LinkRow(person.person_id, parent_id, "", kind.value,
                        display_name=person.display_name, candidate_origin=True, raw_import=True,
                        source=WriterSource.RECONCILE.value, updated_at=now_iso()),
                CandidatePeopleProjection(
                    person.person_id,
                    (CandidatePersonRow(person.person_id, person.person_id, parent_id),),
                ),
            ))
    db.project_rows(tuple(projection_rows))
    db.replace_imported_people(tuple(person.index_row for person in imported))
    project_owner_people(db, incoming_rows)
    return len(imported)
