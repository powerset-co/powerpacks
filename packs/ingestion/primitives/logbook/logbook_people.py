"""People-page families as Logbook people from the v2 store.

Each selected parent includes every non-owner candidate's email and phone;
worth and share do not narrow the selection. Build and reader use the same
candidate name to derive the family's archive slug.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from packs.ingestion.primitives.common.person import Person
from packs.ingestion.primitives.discover.messages.wacli.util import canonicalize_phone


def _parent_names(conn: sqlite3.Connection) -> dict[str, str]:
    names: dict[str, str] = {}
    for row in conn.execute(
        "SELECT p.parent_id, c.display_name FROM current_parent p "
        "JOIN candidates c USING (candidate_id) WHERE c.is_owner = 0 "
        "ORDER BY p.parent_id, c.candidate_id"
    ):
        names.setdefault(row["parent_id"], row["display_name"])
    return names


def people_for_parents(conn: sqlite3.Connection, parent_ids: Sequence[str]) -> list[Person]:
    """One person per parent in selection order; unknown parent ids raise LookupError."""
    names = _parent_names(conn)
    parent_ids = list(dict.fromkeys(parent_ids))
    unknown = [parent_id for parent_id in parent_ids if parent_id not in names]
    if unknown:
        raise LookupError(f"Not in your network: {', '.join(unknown)}")
    people = []
    for parent_id in parent_ids:
        identifiers = conn.execute(
            "SELECT DISTINCT i.kind, i.normalized_value FROM current_parent p "
            "JOIN candidates c USING (candidate_id) JOIN candidate_identifiers i USING (candidate_id) "
            "WHERE p.parent_id = ? AND c.is_owner = 0 ORDER BY i.kind, i.normalized_value",
            (parent_id,),
        ).fetchall()
        sources = conn.execute(
            "SELECT DISTINCT s.source FROM current_parent p JOIN candidates c USING (candidate_id) "
            "JOIN candidate_sources s USING (candidate_id) WHERE p.parent_id = ? AND c.is_owner = 0 "
            "ORDER BY s.source", (parent_id,),
        )
        people.append(Person(
            parent_id, names[parent_id],
            emails=[row["normalized_value"] for row in identifiers if row["kind"] == "email"],
            phones=list(dict.fromkeys(phone for row in identifiers if row["kind"] == "phone"
                                      if (phone := canonicalize_phone(row["normalized_value"])))),
            source_channels=[row["source"] for row in sources],
        ))
    return people


def parent_slugs(conn: sqlite3.Connection) -> dict[str, str]:
    """Map each family's archive slug to its parent id."""
    return {Person(parent_id, name).slug: parent_id for parent_id, name in _parent_names(conn).items()}
