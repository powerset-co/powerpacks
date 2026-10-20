"""Reads for block 09 Realize. Realize writes nothing to the store.

Created: 2026-10-07
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context_v2.db import queries_worth
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import MemberFacts
from packs.ingestion.primitives.deep_context_v2.db.schema import (
    SYNTHETIC_PROFILE_PREFIX, IdentifierKind, ResearchStatus, ReviewDecision, Verdict, Worth,
)
from packs.ingestion.primitives.deep_context_v2.enrich.research import handle as research_handle
from packs.ingestion.primitives.deep_context_v2.review.queue import review_list
from packs.ingestion.primitives.deep_context_v2.worth.evidence import family_facts
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
    """Every non-owner candidate whose family has facts and was not deleted on the People page: the people
    the owner has talked to. Ordered by parent, then candidate, so a family's members are adjacent."""
    members: list[Member] = []
    for row in conn.execute(
        "SELECT cp.parent_id, c.candidate_id, c.display_name, "
        "json_extract(c.import_json, '$.interaction_counts') AS counts, "
        "COALESCE(json_extract(c.import_json, '$.last_interaction'), '') AS last_interaction "
        "FROM current_parent cp JOIN candidates c USING (candidate_id) "
        "WHERE c.is_owner = 0 AND cp.parent_id IN "
        "(SELECT p.parent_id FROM current_parent p JOIN facts f USING (candidate_id)) "
        "AND cp.parent_id NOT IN (SELECT parent_id FROM deleted_parents) "
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
    """Accepted profiles win; otherwise use a worth-yes family's single unopposed proposal.

    A human answer stops automatic attachment. The verdict and parent rows stay untouched, so a
    proposed profile remains correctable through Check LinkedIn.
    """
    keys: dict[str, str] = {}
    for row in conn.execute("SELECT parent_id, profile_key FROM current_profile WHERE profile_key IS NOT NULL"):
        keys[row["parent_id"]] = row["profile_key"]

    proposed: dict[str, dict[str, str]] = {}
    queued: set[str] = {row["parent_id"] for row in conn.execute(
        "SELECT parent_id FROM review_queue WHERE decision IN (?, ?)",
        (ReviewDecision.DETACH.value, ReviewDecision.FIX.value),
    )}
    wrong: set[tuple[str, str]] = set()
    for row in conn.execute(
        "SELECT p.parent_id, l.member_id, l.linkedin_url, l.verdict "
        "FROM current_parent p JOIN current_linkedins l USING (candidate_id)"
    ):
        parent_id = row["parent_id"]
        if row["verdict"] == Verdict.WRONG_PERSON:
            wrong.add((parent_id, row["member_id"]))
        if row["verdict"] == Verdict.NEEDS_REVIEW:
            proposed.setdefault(parent_id, {})[row["member_id"]] = row["linkedin_url"]

    cards = {row["handle"] for row in conn.execute(
        "SELECT handle FROM research WHERE status = ? AND result_json IS NOT NULL", (ResearchStatus.NO_MATCH.value,)
    )}
    members: dict[str, list[MemberFacts]] = {}
    for member in queries_worth.members_with_facts(conn):
        members.setdefault(member.family_key, []).append(member)
    for parent_id, family in members.items():
        handle = research_handle(family_facts(family))
        if handle in cards:
            key = SYNTHETIC_PROFILE_PREFIX + handle
            proposed.setdefault(parent_id, {})[key] = key
    for parent_id in review_list(conn):
        if parent_id in keys or parent_id in queued:
            continue
        choices = {url for member_id, url in proposed.get(parent_id, {}).items() if (parent_id, member_id) not in wrong}
        if len(choices) == 1:
            keys[parent_id] = choices.pop()
    return keys


def worth_by_parent(conn: sqlite3.Connection) -> dict[str, Worth]:
    """parent_id -> its current worth. An undecided parent is absent."""
    worth: dict[str, Worth] = {}
    for row in conn.execute("SELECT parent_id, worth FROM current_worth"):
        worth[row["parent_id"]] = Worth(row["worth"])
    return worth
