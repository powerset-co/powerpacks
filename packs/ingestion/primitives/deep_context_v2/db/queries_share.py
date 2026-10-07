"""Every read and write of the store that the share stage and the People page make.

Rows are keyed by candidate id and written for every member of a family together; the two views
`current_tags` and `current_share` give the family's one row (a human's share row wins).

Created: 2026-10-07
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context_v2.db.queries import _batches

# Row shapes, in column order, for the writers below.
TagRow = tuple[str, str, str, str]                      # candidate_id, tags, note, updated_at
LabelRow = tuple[str, str, str, str, str, str, str]     # candidate_id, public_identifier, full_name, worth, flag, labels_json, updated_at
ShareRow = tuple[str, str, str, str, str, str, str]     # candidate_id, public_identifier, share, reason, labels, source, updated_at


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


@dataclass(frozen=True)
class Share:
    """A share decision: the family's current one (current_share, with its parent) or a member's row."""

    parent_id: str
    candidate_id: str
    public_identifier: str
    share: str
    reason: str
    labels: str
    source: str
    updated_at: str


def current_tags(conn: sqlite3.Connection) -> dict[str, Tags]:
    """parent_id -> the family's tags."""
    found: dict[str, Tags] = {}
    for row in conn.execute("SELECT parent_id, candidate_id, tags, note, updated_at FROM current_tags"):
        found[row["parent_id"]] = Tags(row["parent_id"], row["candidate_id"], row["tags"], row["note"], row["updated_at"])
    return found


def labels_by_candidate(conn: sqlite3.Connection) -> dict[str, Labels]:
    found: dict[str, Labels] = {}
    for row in conn.execute("SELECT candidate_id, public_identifier, full_name, worth, flag, labels_json, updated_at FROM person_labels"):
        found[row["candidate_id"]] = Labels(row["candidate_id"], row["public_identifier"], row["full_name"], row["worth"],
                                            row["flag"], row["labels_json"], row["updated_at"])
    return found


def current_share(conn: sqlite3.Connection) -> list[Share]:
    """Every family's current share decision, by parent id."""
    found: list[Share] = []
    for row in conn.execute(
        "SELECT parent_id, candidate_id, public_identifier, share, reason, labels, source, updated_at FROM current_share "
        "ORDER BY parent_id"
    ):
        found.append(Share(row["parent_id"], row["candidate_id"], row["public_identifier"], row["share"], row["reason"],
                           row["labels"], row["source"], row["updated_at"]))
    return found


def replace_share(conn: sqlite3.Connection, labels: list[LabelRow], shares: list[ShareRow]) -> None:
    """The share node's one pass: both tables rewritten for every family it saw, in one transaction. A row
    from an earlier run is stale, not informative; tags are the human's and are not touched."""
    with conn:
        conn.execute("DELETE FROM person_labels")
        conn.execute("DELETE FROM share")
        for batch in _batches(labels):
            conn.executemany(
                "INSERT INTO person_labels (candidate_id, public_identifier, full_name, worth, flag, labels_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
        for batch in _batches(shares):
            conn.executemany(
                "INSERT INTO share (candidate_id, public_identifier, share, reason, labels, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", batch)


def decide_share(conn: sqlite3.Connection, tags: list[TagRow], shares: list[ShareRow]) -> None:
    """One human decision: the family's tag rows and the share rows they re-decide, committed together."""
    with conn:
        conn.executemany(
            "INSERT INTO person_tags (candidate_id, tags, note, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (candidate_id) DO UPDATE SET tags = excluded.tags, note = excluded.note, updated_at = excluded.updated_at",
            tags)
        conn.executemany(
            "INSERT INTO share (candidate_id, public_identifier, share, reason, labels, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (candidate_id) DO UPDATE SET public_identifier = excluded.public_identifier, "
            "share = excluded.share, reason = excluded.reason, labels = excluded.labels, source = excluded.source, "
            "updated_at = excluded.updated_at",
            shares)
