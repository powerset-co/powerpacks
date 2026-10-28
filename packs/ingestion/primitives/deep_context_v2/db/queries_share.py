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


def _only(parents: tuple[str, ...]) -> str:
    """A WHERE clause on the families named, or none for every family."""
    return "WHERE p.parent_id IN (" + ",".join("?" * len(parents)) + ") " if parents else ""


def current_tags(conn: sqlite3.Connection, parents: tuple[str, ...] = ()) -> dict[str, Tags]:
    """parent_id -> the family's tags: the union of every member's tags, so a human's word on any
    member survives the families merging; the note and time are the latest. `parents` narrows to those families."""
    found: dict[str, Tags] = {}
    for row in conn.execute(
        "SELECT p.parent_id, t.candidate_id, t.tags, t.note, t.updated_at FROM current_parent p "
        "JOIN person_tags t USING (candidate_id) " + _only(parents) + "ORDER BY p.parent_id, t.updated_at DESC, t.candidate_id",
        parents,
    ):
        held: Tags | None = found.get(row["parent_id"])
        if held is None:
            found[row["parent_id"]] = Tags(row["parent_id"], row["candidate_id"], row["tags"], row["note"], row["updated_at"])
        else:
            tags: str = "|".join(sorted((set(held.tags.split("|")) | set(row["tags"].split("|"))) - {""}))
            found[row["parent_id"]] = Tags(held.parent_id, held.candidate_id, tags, held.note, held.updated_at)
    return found


def names_by_parent(conn: sqlite3.Connection, parents: tuple[str, ...] = ()) -> dict[str, str]:
    """parent_id -> the human's name for the family: the newest named member's. `parents` narrows to those
    families."""
    names: dict[str, str] = {}
    for row in conn.execute(
        "SELECT p.parent_id, t.name FROM current_parent p JOIN person_tags t USING (candidate_id) "
        + (_only(parents) + "AND" if parents else "WHERE") + " t.name IS NOT NULL "
        "ORDER BY t.name_updated_at DESC, t.candidate_id",
        parents,
    ):
        names.setdefault(row["parent_id"], row["name"])
    return names


def write_name(conn: sqlite3.Connection, parent_id: str, members: tuple[str, ...], name: str, now: str) -> None:
    """The human's name for every member of the family, and the page's row with it; tags, relationship and
    note stay as they are."""
    with conn:
        conn.executemany(
            "INSERT INTO person_tags (candidate_id, tags, note, updated_at, name, name_updated_at) VALUES (?, '', '', ?, ?, ?) "
            "ON CONFLICT (candidate_id) DO UPDATE SET name = excluded.name, name_updated_at = excluded.name_updated_at",
            [(candidate_id, now, name, now) for candidate_id in members])
        conn.execute("UPDATE families SET name = ? WHERE parent_id = ?", (name, parent_id))


def relationships_by_parent(conn: sqlite3.Connection, parents: tuple[str, ...] = ()) -> dict[str, str]:
    """The human's relationship choices, including families without machine labels."""
    relationships: dict[str, str] = {}
    for row in conn.execute(
        "SELECT p.parent_id, t.relationship_kind FROM current_parent p JOIN person_tags t USING (candidate_id) "
        + (_only(parents) + "AND" if parents else "WHERE") + " t.relationship_kind IS NOT NULL "
        "ORDER BY t.updated_at DESC, t.candidate_id",
        parents,
    ):
        relationships.setdefault(row["parent_id"], row["relationship_kind"])
    return relationships


def labels_by_parent(conn: sqlite3.Connection, parents: tuple[str, ...] = ()) -> dict[str, Labels]:
    """The family's exported labels with the human's latest relationship choice and name applied. `parents`
    narrows to those families."""
    names: dict[str, str] = names_by_parent(conn, parents)
    relationships = relationships_by_parent(conn, parents)
    found: dict[str, Labels] = {}
    for row in conn.execute(
        "SELECT p.parent_id, l.*, json_extract(l.labels_json, '$.relationship_kind') AS relationship_kind "
        "FROM current_parent p JOIN person_labels l USING (candidate_id) " + _only(parents) +
        "ORDER BY p.parent_id, l.updated_at DESC, l.candidate_id",
        parents,
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
        found[parent_id] = Labels(row["candidate_id"], row["public_identifier"], names.get(parent_id, row["full_name"]),
                                  row["worth"], flag, labels_json, row["updated_at"], source)
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


def delete_parents(conn: sqlite3.Connection, parent_ids: tuple[str, ...], now: str) -> None:
    """Hide the families from the People page for good: their members and decisions stay, their
    `families` rows go, so the labels pass and the page see the same families."""
    with conn:
        conn.executemany("INSERT OR IGNORE INTO deleted_parents (parent_id, deleted_at) VALUES (?, ?)",
                         [(parent_id, now) for parent_id in parent_ids])
        conn.executemany("DELETE FROM families WHERE parent_id = ?", [(parent_id,) for parent_id in parent_ids])


def deleted_parents(conn: sqlite3.Connection) -> set[str]:
    return {row["parent_id"] for row in conn.execute("SELECT parent_id FROM deleted_parents")}


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


def families(conn: sqlite3.Connection, parents: tuple[str, ...] = ()) -> dict[str, sqlite3.Row]:
    """parent_id -> its families row as realize wrote it; `parents` narrows to those families."""
    return {row["parent_id"]: row for row in conn.execute(
        "SELECT * FROM families WHERE parent_id IN (SELECT parent_id FROM current_parent)"
        + (" AND parent_id IN (" + ",".join("?" * len(parents)) + ")" if parents else "")
        + " ORDER BY parent_id", parents)}
