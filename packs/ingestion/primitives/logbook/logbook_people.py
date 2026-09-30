"""People-page parents as Logbook people, read from the canonical SQLite store.

A selected parent becomes one ``Person``: every email and phone of every child
merged under it, owner and ghost children left out. The selection is the whole
scope — no worth, share, private or import-ignore filter applies.

``parent_slugs`` names every parent's entry the same way, so the People page can
tell which parents already have a saved logbook.

Changelog:
  2026-09-30: ``parent_slugs`` for the People page's Logbook reader and filter.
  2026-09-30: created for the People page's Build logbook action.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.models import IdentifierKind, ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.shared.common import Person
from packs.ingestion.primitives.discover.messages.wacli.util import canonicalize_phone


def people_for_parents(db: Db, parent_ids: Sequence[str]) -> list[Person]:
    """One ``Person`` per parent id, in the order given; unknown ids raise ``LookupError``."""
    parents = {parent_id: queries.parents(db, parent_id=parent_id) for parent_id in dict.fromkeys(parent_ids)}
    unknown = [parent_id for parent_id, rows in parents.items() if not rows]
    if unknown:
        raise LookupError(f"Not in your network: {', '.join(unknown)}")
    people = []
    for parent_id, (parent,) in parents.items():
        children = _eligible(queries.people(db, parent_id=parent_id))
        eligible = {child.person_id for child in children}
        identifiers = [row for row in queries.identifiers(db, parent_id=parent_id) if row.person_id in eligible]
        sources = [row.source for row in queries.sources(db, parent_id=parent_id) if row.person_id in eligible]
        people.append(Person(
            parent_id,
            _name(parent, children),
            emails=_unique(row.normalized_value for row in identifiers if row.kind == IdentifierKind.EMAIL),
            # The store keeps directory-form phones ("4155550101"); message readers match E.164.
            phones=_unique(canonicalize_phone(row.normalized_value)
                           for row in identifiers if row.kind == IdentifierKind.PHONE),
            source_channels=_unique(sources),
        ))
    return people


def parent_slugs(db: Db) -> dict[str, str]:
    """Every parent's Logbook entry slug, mapped to the parent id: the slug a People-page
    build of that parent writes today."""
    children: dict[str, list[PersonRow]] = defaultdict(list)
    for child in _eligible(queries.people(db)):
        children[child.parent_id].append(child)
    return {Person(parent.parent_id, _name(parent, children[parent.parent_id])).slug: parent.parent_id
            for parent in queries.parents(db)}


def _eligible(people: Iterable[PersonRow]) -> list[PersonRow]:
    return [child for child in people if not child.is_owner and not child.is_ghost]


def _name(parent: ParentRow, children: Sequence[PersonRow]) -> str:
    return parent.display_name or next((child.display_name for child in children if child.display_name), "")


def _unique(values) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
