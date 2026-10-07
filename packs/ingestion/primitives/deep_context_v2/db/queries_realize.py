"""Reads for block 09 Realize. Realize writes nothing to the store.

Created: 2026-10-07
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context_v2.db.schema import IdentifierKind, Worth
from packs.ingestion.schemas.people_schema import parse_interaction_counts


@dataclass(frozen=True)
class Member:
    """One candidate of a family, with the importer's message counts."""

    parent_id: str
    candidate_id: str
    display_name: str
    interaction_counts: dict[str, int]  # channel -> messages, as the importer counted them
    last_interaction: str               # ISO-8601 UTC


@dataclass(frozen=True)
class Identifier:
    kind: IdentifierKind
    display_value: str


def members_of_talked_to_parents(conn: sqlite3.Connection) -> list[Member]:
    """Every non-owner candidate whose family has facts: the people the owner has talked to.
    Ordered by parent, then candidate, so a family's members are adjacent."""
    members: list[Member] = []
    for row in conn.execute(
        "SELECT cp.parent_id, c.candidate_id, c.display_name, "
        "json_extract(c.import_json, '$.interaction_counts') AS counts, "
        "json_extract(c.import_json, '$.last_interaction') AS last_interaction "
        "FROM current_parent cp JOIN candidates c USING (candidate_id) "
        "WHERE c.is_owner = 0 AND cp.parent_id IN "
        "(SELECT p.parent_id FROM current_parent p JOIN facts f USING (candidate_id)) "
        "ORDER BY cp.parent_id, c.candidate_id"
    ):
        # The importer stores the people.csv cell as written: JSON, or blank for a person with no
        # counted interactions (people_schema is the cell's contract).
        counts: dict[str, int] = parse_interaction_counts(row["counts"])
        members.append(Member(row["parent_id"], row["candidate_id"], row["display_name"], counts,
                              row["last_interaction"]))
    return members


def identifiers_by_candidate(conn: sqlite3.Connection) -> dict[str, list[Identifier]]:
    """candidate_id -> its emails and phones as they were written, in a stable order."""
    identifiers: dict[str, list[Identifier]] = {}
    for row in conn.execute(
        "SELECT candidate_id, kind, display_value FROM candidate_identifiers ORDER BY candidate_id, kind, normalized_value"
    ):
        identifiers.setdefault(row["candidate_id"], []).append(Identifier(IdentifierKind(row["kind"]), row["display_value"]))
    return identifiers


def channels_by_candidate(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """candidate_id -> the message channels it came from."""
    channels: dict[str, list[str]] = {}
    for row in conn.execute("SELECT candidate_id, source FROM candidate_sources ORDER BY candidate_id, source"):
        channels.setdefault(row["candidate_id"], []).append(row["source"])
    return channels


def profile_keys(conn: sqlite3.Connection) -> dict[str, str]:
    """parent_id -> its accepted profile key: a confirmed LinkedIn URL or a synthetic key. A parent
    with neither is absent."""
    keys: dict[str, str] = {}
    for row in conn.execute("SELECT parent_id, profile_key FROM current_profile WHERE profile_key IS NOT NULL"):
        keys[row["parent_id"]] = row["profile_key"]
    return keys


def worth_by_parent(conn: sqlite3.Connection) -> dict[str, Worth]:
    """parent_id -> its current worth. An undecided parent is absent."""
    worth: dict[str, Worth] = {}
    for row in conn.execute("SELECT parent_id, worth FROM current_worth"):
        worth[row["parent_id"]] = Worth(row["worth"])
    return worth
