"""Read and write the People labels, human tags and local sets; delete a family for good."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context_v2.db.queries import _batches
from packs.ingestion.primitives.share.labels import FLAG_P
from packs.ingestion.schemas.share_schema import FAMILY, MINOR, ROMANTIC_PARTNER

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
    relationship_kind_source: str


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
    """The family's exported labels with the human's latest relationship choice applied."""
    relationships: dict[str, str] = {}
    for row in conn.execute(
        "SELECT p.parent_id, t.relationship_kind FROM current_parent p JOIN person_tags t USING (candidate_id) "
        "WHERE t.relationship_kind IS NOT NULL ORDER BY t.updated_at DESC, t.candidate_id"
    ):
        relationships.setdefault(row["parent_id"], row["relationship_kind"])
    found: dict[str, Labels] = {}
    for row in conn.execute(
        "SELECT p.parent_id, l.*, json_extract(l.labels_json, '$.relationship_kind') AS relationship_kind "
        "FROM current_parent p JOIN person_labels l USING (candidate_id) "
        "ORDER BY p.parent_id, l.updated_at DESC, l.candidate_id"
    ):
        parent_id = row["parent_id"]
        if parent_id in found:
            continue
        kind = relationships.get(parent_id)
        flag = row["flag"]
        labels_json = row["labels_json"]
        if kind is not None:
            cells = json.loads(labels_json)
            cells["relationship_kind"] = kind
            cells.pop("relationship_kind_p", None)
            flag = kind if kind in (FAMILY, ROMANTIC_PARTNER) else (
                MINOR if cells.get("is_minor", 0.0) >= FLAG_P else "")
            labels_json = json.dumps(cells)
        source = "human" if kind is not None else "machine" if row["relationship_kind"] else ""
        found[parent_id] = Labels(row["candidate_id"], row["public_identifier"], row["full_name"], row["worth"],
                                  flag, labels_json, row["updated_at"], source)
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


def delete_family(conn: sqlite3.Connection, parent_id: str) -> None:
    """Delete the family and its member candidates from every table, in one transaction.
    candidate_names, candidate_identifiers, candidate_sources, bundles and facts go by ON DELETE CASCADE;
    the tables below reference candidates without it, so they go first."""
    members = [row["candidate_id"] for row in
               conn.execute("SELECT candidate_id FROM current_parent WHERE parent_id = ?", (parent_id,))]
    marks = ", ".join("?" * len(members))
    with conn:
        conn.execute(f"DELETE FROM pair_verdicts WHERE candidate_a IN ({marks}) OR candidate_b IN ({marks})",
                     members + members)
        for table in ("worth", "candidate_linkedins", "person_tags", "person_labels"):
            conn.execute(f"DELETE FROM {table} WHERE candidate_id IN ({marks})", members)
        conn.execute(f"DELETE FROM candidate_parent WHERE candidate_id IN ({marks}) OR parent_id = ?",
                     [*members, parent_id])
        conn.execute("DELETE FROM research WHERE parent_id = ?", (parent_id,))
        conn.execute("DELETE FROM review_queue WHERE parent_id = ?", (parent_id,))
        conn.execute(f"DELETE FROM candidates WHERE candidate_id IN ({marks})", members)


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
