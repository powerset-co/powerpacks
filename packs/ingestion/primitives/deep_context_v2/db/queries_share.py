"""Read and write the People labels, human tags and local sets."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context_v2.db.queries import _batches

# Row shapes, in column order, for the writers below.
TagRow = tuple[str, str, str, str]                      # candidate_id, tags, note, updated_at
LabelRow = tuple[str, str, str, str, str, str, str]     # candidate_id, public_identifier, full_name, worth, flag, labels_json, updated_at


@dataclass(frozen=True)
class Tags:
    """A family's human tags: the latest member row (current_tags), or one member's own row."""

    parent_id: str
    candidate_id: str
    tags: str
    note: str
    updated_at: str


@dataclass(frozen=True)
class Labels:
    candidate_id: str
    public_identifier: str
    full_name: str
    worth: str
    flag: str
    labels_json: str
    updated_at: str


def current_tags(conn: sqlite3.Connection) -> dict[str, Tags]:
    """parent_id -> the family's tags: the union of every member's tags, so a human's word on any
    member survives the families merging; the note and time are the latest."""
    found: dict[str, Tags] = {}
    for row in conn.execute(
        "SELECT p.parent_id, t.candidate_id, t.tags, t.note, t.updated_at FROM current_parent p "
        "JOIN person_tags t USING (candidate_id) ORDER BY p.parent_id, t.updated_at DESC, t.candidate_id"
    ):
        held: Tags | None = found.get(row["parent_id"])
        if held is None:
            found[row["parent_id"]] = Tags(row["parent_id"], row["candidate_id"], row["tags"], row["note"], row["updated_at"])
        else:
            tags: str = "|".join(sorted((set(held.tags.split("|")) | set(row["tags"].split("|"))) - {""}))
            found[row["parent_id"]] = Tags(held.parent_id, held.candidate_id, tags, held.note, held.updated_at)
    return found


def labels_by_parent(conn: sqlite3.Connection) -> dict[str, Labels]:
    """The family's most recently exported labels; ties use candidate id."""
    found: dict[str, Labels] = {}
    for row in conn.execute(
        "SELECT p.parent_id, l.* FROM current_parent p JOIN person_labels l USING (candidate_id) "
        "ORDER BY p.parent_id, l.updated_at DESC, l.candidate_id"
    ):
        found.setdefault(row["parent_id"], Labels(row["candidate_id"], row["public_identifier"], row["full_name"],
                                                row["worth"], row["flag"], row["labels_json"], row["updated_at"]))
    return found


def replace_labels(conn: sqlite3.Connection, labels: list[LabelRow]) -> None:
    """Rewrite the label export for every family; the human's tags stay untouched."""
    conn.execute("DELETE FROM person_labels")
    for batch in _batches(labels):
        conn.executemany(
            "INSERT INTO person_labels (candidate_id, public_identifier, full_name, worth, flag, labels_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", batch)


def write_tags(conn: sqlite3.Connection, tags: list[TagRow]) -> None:
    """Write the human's tags for each member of the selected families."""
    with conn:
        conn.executemany(
            "INSERT INTO person_tags (candidate_id, tags, note, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (candidate_id) DO UPDATE SET tags = excluded.tags, note = excluded.note, updated_at = excluded.updated_at",
            tags)


# ---- the sets on this machine (sets.py): made here, or joined by accepting an invite

def sets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The sets on this machine, by name."""
    return conn.execute("SELECT set_id, name, role, members_json FROM sets ORDER BY name COLLATE NOCASE").fetchall()


def insert_set(conn: sqlite3.Connection, set_id: str, name: str, role: str, members_json: str, created_at: str) -> None:
    with conn:
        # A second invite to a set already joined is accepted again: the newer answer stands.
        conn.execute("INSERT OR REPLACE INTO sets (set_id, name, role, members_json, created_at) VALUES (?, ?, ?, ?, ?)",
                     (set_id, name, role, members_json, created_at))


def update_set_members(conn: sqlite3.Connection, set_id: str, members_json: str) -> None:
    with conn:
        conn.execute("UPDATE sets SET members_json = ? WHERE set_id = ?", (members_json, set_id))


def delete_set(conn: sqlite3.Connection, set_id: str) -> None:
    with conn:
        conn.execute("DELETE FROM sets WHERE set_id = ?", (set_id,))
