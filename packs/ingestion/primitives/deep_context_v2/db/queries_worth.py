"""Every read and write of the store that block 06 Worth makes.

Created: 2026-10-06
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context_v2.db.schema import DecidedBy

BATCH = 500

# Row shape, in column order, for append_worth.
WorthRow = tuple[str, str, str, str, str, str, str]  # candidate_id, worth, decided_by, reason, labels_json, input_fingerprint, created_at


@dataclass(frozen=True)
class MemberFacts:
    """One candidate with facts, and the family it belongs to: its current parent, or itself while it
    has none (before dedupe writes the singleton rows)."""

    candidate_id: str
    family_key: str
    facts_json: str
    facts_fingerprint: str
    synthesized_at: str


@dataclass(frozen=True)
class ChannelCount:
    """How many of a candidate's collected messages went one way on one channel, and when."""

    candidate_id: str
    channel: str
    direction: str
    messages: int
    first_at: str | None  # None = no message on this channel and direction carried a timestamp
    last_at: str | None


@dataclass(frozen=True)
class Connection:
    """One row of the LinkedIn export."""

    linkedin_url: str
    name: str
    position: str


def members_with_facts(conn: sqlite3.Connection) -> list[MemberFacts]:
    """Every candidate with a facts row, with its family key. A candidate without facts has no evidence
    to judge and is not a member of any family worth looks at."""
    members: list[MemberFacts] = []
    for row in conn.execute(
        "SELECT f.candidate_id, COALESCE(p.parent_id, f.candidate_id) AS family_key, f.facts_json, "
        "f.input_fingerprint, f.synthesized_at FROM facts f LEFT JOIN current_parent p USING (candidate_id) "
        "ORDER BY family_key, f.candidate_id"
    ):
        members.append(MemberFacts(row["candidate_id"], row["family_key"], row["facts_json"],
                                   row["input_fingerprint"], row["synthesized_at"]))
    return members


def names_by_candidate(conn: sqlite3.Connection) -> dict[str, list[str]]:
    names: dict[str, list[str]] = {}
    for row in conn.execute("SELECT candidate_id, name FROM candidate_names ORDER BY candidate_id, name"):
        names.setdefault(row["candidate_id"], []).append(row["name"])
    return names


def sources_by_candidate(conn: sqlite3.Connection) -> dict[str, list[str]]:
    sources: dict[str, list[str]] = {}
    for row in conn.execute("SELECT candidate_id, source FROM candidate_sources ORDER BY candidate_id, source"):
        sources.setdefault(row["candidate_id"], []).append(row["source"])
    return sources


def channel_counts(conn: sqlite3.Connection) -> list[ChannelCount]:
    """Message counts per candidate, channel and direction, with the first and last timestamp. Only the
    counts leave this function. (The node authorizer denies json_each, so the bundle is parsed here.)"""
    counts: list[ChannelCount] = []
    for row in conn.execute("SELECT candidate_id, payload_json FROM bundles ORDER BY candidate_id"):
        candidate_id: str = row["candidate_id"]
        messages: dict[tuple[str, str], int] = {}  # (channel, direction) -> messages
        first: dict[tuple[str, str], str] = {}
        last: dict[tuple[str, str], str] = {}
        for message in json.loads(row["payload_json"])["messages"]:
            key: tuple[str, str] = (message["channel"], message["direction"])
            messages[key] = messages.get(key, 0) + 1
            at: str = message["at"]
            if not at:
                continue  # a source row without a timestamp still counts as a message
            if key not in first or at < first[key]:
                first[key] = at
            if key not in last or at > last[key]:
                last[key] = at
        for key in sorted(messages):
            counts.append(ChannelCount(candidate_id, key[0], key[1], messages[key], first.get(key), last.get(key)))
    return counts


def group_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many group chats each candidate's bundle names."""
    groups: dict[str, int] = {}
    for row in conn.execute("SELECT candidate_id, payload_json FROM bundles"):
        groups[row["candidate_id"]] = len(json.loads(row["payload_json"])["groups"])
    return groups


def all_connections(conn: sqlite3.Connection) -> list[Connection]:
    connections: list[Connection] = []
    for row in conn.execute("SELECT linkedin_url, name, position FROM connections ORDER BY linkedin_url"):
        connections.append(Connection(row["linkedin_url"], row["name"], row["position"]))
    return connections


def latest_machine_worth_fingerprints(conn: sqlite3.Connection) -> dict[str, str]:
    """Each candidate's latest machine worth row's evidence fingerprint: what worth last judged."""
    fingerprints: dict[str, str] = {}
    for row in conn.execute(
        "SELECT candidate_id, input_fingerprint FROM worth w WHERE decided_by = ? "
        "AND seq = (SELECT MAX(seq) FROM worth WHERE candidate_id = w.candidate_id AND decided_by = ?)",
        (DecidedBy.MACHINE.value, DecidedBy.MACHINE.value),
    ):
        fingerprints[row["candidate_id"]] = row["input_fingerprint"]
    return fingerprints


def append_worth(conn: sqlite3.Connection, rows: list[WorthRow]) -> None:
    """Worth is a ledger: every decision is a new row."""
    for start in range(0, len(rows), BATCH):
        conn.executemany(
            "INSERT INTO worth (candidate_id, worth, decided_by, reason, labels_json, input_fingerprint, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows[start:start + BATCH],
        )
