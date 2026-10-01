"""Parse the imported people boundary once and project it into canonical SQLite.

``people.csv`` is the one live input owned by the import fan-in. This module is
its only Deep Context reader. It converts rows to frozen values at the boundary,
then get-or-creates stable parent ownership before message collection starts.
Everything downstream reads the SQLite roster, including the headline used by
the notable-title rule.

Changelog:
  2026-09-26: the profile cells a person list renders (LinkedIn URL, avatar,
      title, company, location) ride the row; the share UI reads them.
  2026-09-25: `headline` (the imported LinkedIn headline) rides the row; the
      worth stage's notable-title rule reads it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import sys
from pathlib import Path

from packs.ingestion.primitives.common.contact_fields import (
    emails_from_row,
    normalize_email,
    normalize_phone,
    phones_from_row,
)
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.common.legacy import scrub_harmonic_profiles
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
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.identity_queries import links, review_rows
from packs.ingestion.primitives.deep_context.db.identity_policy import (
    AFFIRMATIVE_MACHINE_ACTIONS,
    AFFIRMATIVE_MACHINE_APPROVALS,
)
from packs.ingestion.primitives.deep_context.db.queries import (
    identifiers as identifier_rows,
    parents as parent_rows,
    people as person_rows,
    sources as source_rows,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.merge_repair import repair_merged_parents
from packs.ingestion.primitives.deep_context.db.queries import imported_people as stored_people_rows
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.primitives.imports.merge_people import merge_group
from packs.ingestion.primitives.deep_context.ensure_parents.assignment import load_assignment
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
    combined: dict[str, ImportedPerson] = {}
    for row in rows:
        raw = row.to_row()
        person_id = _text(raw.get("id")).lower()
        if not person_id or "/" in person_id or "\\" in person_id:
            continue
        display_name = _text(raw.get("full_name")) or " ".join(
            filter(None, (_text(raw.get("first_name")), _text(raw.get("last_name"))))
        )
        incoming = ImportedPerson(
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
        prior: ImportedPerson | None = combined.get(person_id)
        if prior is None:
            combined[person_id] = incoming
            continue
        merged = ImportedPerson(
            person_id=person_id,
            display_name=incoming.display_name or prior.display_name,
            emails=tuple(dict.fromkeys((*prior.emails, *incoming.emails))),
            phones=tuple(dict.fromkeys((*prior.phones, *incoming.phones))),
            source_channels=tuple(dict.fromkeys((*prior.source_channels, *incoming.source_channels))),
            superseded_person_ids=tuple(
                dict.fromkeys((*prior.superseded_person_ids, *incoming.superseded_person_ids))
            ),
            public_identifier=incoming.public_identifier or prior.public_identifier,
            interaction_counts={**prior.interaction_counts, **incoming.interaction_counts},
            last_interaction=max(prior.last_interaction, incoming.last_interaction),
            headline=incoming.headline or prior.headline,
            linkedin_url=incoming.linkedin_url or prior.linkedin_url,
            avatar_url=incoming.avatar_url or prior.avatar_url,
            title=incoming.title or prior.title,
            company=incoming.company or prior.company,
            location=incoming.location or prior.location,
            index_row=incoming.index_row,
        )
        full = merge_group(person_id, [incoming.index_row, prior.index_row])
        full["id"] = merged.person_id
        full["superseded_person_ids"] = json.dumps(
            [value for value in _superseded(full["superseded_person_ids"]) if value != person_id]
        )
        full["full_name"] = merged.display_name
        full["headline"] = merged.headline
        full["public_identifier"] = merged.public_identifier
        full["linkedin_url"] = merged.linkedin_url
        combined[person_id] = replace(merged, index_row=PeopleRow.model_validate(full))
    return tuple(combined[key] for key in sorted(combined))


def read_imported_people(path: Path) -> tuple[ImportedPerson, ...]:
    """Read the canonical fan-in CSV only at the import boundary."""
    if not path.is_file():
        return ()
    rows = []
    for raw in CsvIO.read_dict_rows(path):
        if not raw.get("primary_phone"):
            raw["primary_phone"] = raw.get("phone") or raw.get("phone_e164") or ""
        rows.append(PeopleRow.model_validate(raw))
    return _imported_people(tuple(rows))


def stored_imported_people(db: Db) -> tuple[ImportedPerson, ...]:
    """Read the current roster from SQLite for all downstream stages."""
    return _imported_people(stored_people_rows(db))


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
    repair = repair_merged_parents(db)
    removed = scrub_harmonic_profiles(db)
    if removed:
        print(f'[deep-context] invalidated {removed} Harmonic profile artifacts', file=sys.stderr)
    if repair.repaired or repair.unresolved:
        print(f'[deep-context] repaired {len(repair.repaired)} merged parents; '
              f'{len(repair.unresolved)} unresolved', file=sys.stderr)
    if not imported:
        return 0
    current = {row.id: row for row in stored_people_rows(db)}
    canonical_by_alias = {
        alias: row.id
        for row in current.values()
        for alias in _superseded(row.superseded_person_ids)
    }
    incoming_ids = {canonical_by_alias.get(person.person_id, person.person_id) for person in imported}
    represented_ids = set(incoming_ids)
    represented_ids.update(alias for person in imported for alias in person.superseded_person_ids)
    combined_rows: list[PeopleRow] = []
    for person in imported:
        source = person.index_row
        original_id = person.person_id
        canonical_id = canonical_by_alias.get(original_id, original_id)
        prior = current.get(canonical_id)
        prior_aliases = [current[alias] for alias in person.superseded_person_ids
                         if alias in current and alias != canonical_id]
        source = source.model_copy(update={
            "id": canonical_id,
            "full_name": person.display_name,
            "public_identifier": prior.public_identifier if canonical_id != original_id and prior else person.public_identifier,
            "linkedin_url": prior.linkedin_url if canonical_id != original_id and prior else person.linkedin_url,
            "superseded_person_ids": json.dumps((*person.superseded_person_ids, original_id)
                                               if canonical_id != original_id else person.superseded_person_ids),
        })
        if prior and canonical_id != original_id and person.public_identifier != prior.public_identifier:
            carry = {column: getattr(source, column)
                     for column in ("id", "superseded_person_ids", "source_artifacts", *CONTACT_CARRY_COLUMNS)}
            carry.update(public_identifier=prior.public_identifier, linkedin_url=prior.linkedin_url)
            source = PeopleRow.model_validate(carry)
        previous = ([prior] if prior is not None else []) + prior_aliases
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
            merged["superseded_person_ids"] = json.dumps(
                [value for value in _superseded(merged["superseded_person_ids"])
                 if value != canonical_id]
            )
            source = PeopleRow.model_validate(merged)
        combined_rows.append(source)
    combined_rows.extend(row for key, row in current.items() if key not in represented_ids)
    imported = _imported_people(tuple(combined_rows))
    existing_people = {row.person_id: row for row in person_rows(db)}
    existing_links = {row.row_key: row for row in links(db)}
    people_by_parent: dict[str, list[str]] = {}
    for person in existing_people.values():
        people_by_parent.setdefault(person.parent_id, []).append(person.person_id)
    # A realized LinkedIn keeps the people its SQLite decision already belongs to.
    approved_people: dict[str, list[str]] = {}
    for row in review_rows(db, include_worth=False):
        slug = row.new_public_identifier or row.public_identifier
        if slug and row.action in AFFIRMATIVE_MACHINE_ACTIONS and row.approved in AFFIRMATIVE_MACHINE_APPROVALS:
            approved_people.setdefault(slug, []).extend(people_by_parent[existing_links[row.key].parent_id])
    imported = tuple(
        replace(person, superseded_person_ids=(*person.superseded_person_ids, *approved_people.get(person.public_identifier, ())))
        for person in imported
    )
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
        # Imported assignments are candidates, not approvals. Existing review
        # rows own their verdicts, including retargets realized under a new id.
        slug = person.public_identifier
        url = person.linkedin_url or (f"https://www.linkedin.com/in/{slug}" if slug else "")
        if slug and slug not in existing_links and (parent_id, slug) not in represented_slugs:
            projection_rows.extend((
                LinkRow(slug, parent_id, slug, RowKind.PUB.value, url, person.display_name,
                        source=WriterSource.RECONCILE.value, updated_at=now_iso()),
                CandidatePeopleProjection(slug, (CandidatePersonRow(slug, person.person_id, parent_id),)),
            ))
            represented_slugs.add((parent_id, slug))
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
    return len(imported)
