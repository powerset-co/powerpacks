"""Every read and write of the v2 store, as named functions. No block holds SQL.

One function per thing a block does to the store: what it is named is what it does. Writers
upsert on the primary key or insert-and-ignore; nothing here deletes. Batches of BATCH rows per
executemany.

Created: 2026-10-06
"""
from __future__ import annotations

import sqlite3

from packs.ingestion.primitives.deep_context_v2.collect.bundle import Person
from packs.ingestion.primitives.deep_context_v2.db.schema import IdentifierKind

BATCH = 500

# Row shapes, in column order, for the writers below.
CandidateRow = tuple[str, str, int, str, str]            # candidate_id, display_name, is_owner, import_json, imported_at
NameRow = tuple[str, str]                                 # candidate_id, name
IdentifierRow = tuple[str, str, str, str]                 # candidate_id, kind, normalized_value, display_value
SourceRow = tuple[str, str]                               # candidate_id, source
ConnectionRow = tuple[str, str, str | None, str, str, str]  # linkedin_url, name, email, position, company, imported_at


def _batches(rows: list, size: int = BATCH) -> list[list]:
    chunks: list[list] = []
    for start in range(0, len(rows), size):
        chunks.append(rows[start:start + size])
    return chunks


# ---- owner


def upsert_owner(conn: sqlite3.Connection, payload_json: str, fingerprint: str, now: str) -> None:
    conn.execute(
        "INSERT INTO owner (owner_key, payload_json, content_fingerprint, projected_at) VALUES ('owner', ?, ?, ?) "
        "ON CONFLICT (owner_key) DO UPDATE SET payload_json = excluded.payload_json, "
        "content_fingerprint = excluded.content_fingerprint, projected_at = excluded.projected_at",
        (payload_json, fingerprint, now),
    )


def owner_payload_json(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT payload_json FROM owner WHERE owner_key = 'owner'").fetchone()["payload_json"]


# ---- 01 import


def upsert_candidates(conn: sqlite3.Connection, rows: list[CandidateRow]) -> None:
    """A rerun overwrites the row."""
    for batch in _batches(rows):
        conn.executemany(
            "INSERT INTO candidates (candidate_id, display_name, is_owner, import_json, imported_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (candidate_id) DO UPDATE SET "
            "display_name = excluded.display_name, is_owner = excluded.is_owner, "
            "import_json = excluded.import_json, imported_at = excluded.imported_at",
            batch,
        )


def insert_candidate_names(conn: sqlite3.Connection, rows: list[NameRow]) -> None:
    """A name already there is left alone."""
    for batch in _batches(rows):
        conn.executemany("INSERT OR IGNORE INTO candidate_names (candidate_id, name) VALUES (?, ?)", batch)


def insert_candidate_identifiers(conn: sqlite3.Connection, rows: list[IdentifierRow]) -> None:
    for batch in _batches(rows):
        conn.executemany(
            "INSERT OR IGNORE INTO candidate_identifiers (candidate_id, kind, normalized_value, display_value) "
            "VALUES (?, ?, ?, ?)",
            batch,
        )


def insert_candidate_sources(conn: sqlite3.Connection, rows: list[SourceRow]) -> None:
    for batch in _batches(rows):
        conn.executemany("INSERT OR IGNORE INTO candidate_sources (candidate_id, source) VALUES (?, ?)", batch)


def upsert_connections(conn: sqlite3.Connection, rows: list[ConnectionRow]) -> None:
    """A rerun overwrites by URL."""
    for batch in _batches(rows):
        conn.executemany(
            "INSERT INTO connections (linkedin_url, name, email, position, company, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (linkedin_url) DO UPDATE SET name = excluded.name, "
            "email = excluded.email, position = excluded.position, company = excluded.company, "
            "imported_at = excluded.imported_at",
            batch,
        )


# ---- 02 collect


def channels_present(conn: sqlite3.Connection) -> set[str]:
    """Which message channels the candidates came from. An unlinked channel has no candidates, so this
    is also the list of message stores a run opens."""
    channels: set[str] = set()
    for row in conn.execute("SELECT DISTINCT source FROM candidate_sources"):
        channels.add(row["source"])
    return channels


def candidates_to_collect(conn: sqlite3.Connection, limit: int) -> list[Person]:
    """Every non-owner candidate as a Person: its normalized emails and phones (the keys the message
    readers match on) and its channels (which stores to open). Nobody builds a dossier on the operator."""
    people: list[Person] = []
    for row in conn.execute(
        "SELECT candidate_id, display_name FROM candidates WHERE is_owner = 0 ORDER BY candidate_id LIMIT ?",
        (limit,),
    ):
        person = Person(row["candidate_id"], row["display_name"])
        for identifier in conn.execute(
            "SELECT kind, normalized_value FROM candidate_identifiers WHERE candidate_id = ? ORDER BY kind, normalized_value",
            (person.person_id,),
        ):
            kind: str = identifier["kind"]
            if kind == IdentifierKind.EMAIL:
                person.emails.append(identifier["normalized_value"])
            elif kind == IdentifierKind.PHONE:
                person.phones.append(identifier["normalized_value"])
            else:
                print(f"unknown identifier kind {kind!r}, skipped")  # the DDL allows only the two above
                continue
        for source in conn.execute(
            "SELECT source FROM candidate_sources WHERE candidate_id = ? ORDER BY source", (person.person_id,)
        ):
            person.source_channels.append(source["source"])
        people.append(person)
    return people


def upsert_bundle(conn: sqlite3.Connection, candidate_id: str, payload_json: str, fingerprint: str, now: str) -> None:
    conn.execute(
        "INSERT INTO bundles (candidate_id, payload_json, content_fingerprint, collected_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (candidate_id) DO UPDATE SET payload_json = excluded.payload_json, "
        "content_fingerprint = excluded.content_fingerprint, collected_at = excluded.collected_at",
        (candidate_id, payload_json, fingerprint, now),
    )


# ---- 03 synthesize


def count_bundles(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM bundles").fetchone()[0]


def bundles_with_facts_fingerprint(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every bundle with the fingerprint of the facts row it has, or NULL when it has none yet.
    Columns: candidate_id, payload_json, done."""
    return conn.execute(
        "SELECT b.candidate_id, b.payload_json, f.input_fingerprint AS done "
        "FROM bundles b LEFT JOIN facts f USING (candidate_id) ORDER BY b.candidate_id"
    ).fetchall()


def upsert_facts(conn: sqlite3.Connection, candidate_id: str, facts_json: str, fingerprint: str,
                 model: str, effort: str, now: str) -> None:
    conn.execute(
        "INSERT INTO facts (candidate_id, facts_json, input_fingerprint, model, reasoning_effort, synthesized_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (candidate_id) DO UPDATE SET "
        "facts_json = excluded.facts_json, input_fingerprint = excluded.input_fingerprint, "
        "model = excluded.model, reasoning_effort = excluded.reasoning_effort, "
        "synthesized_at = excluded.synthesized_at",
        (candidate_id, facts_json, fingerprint, model, effort, now),
    )
