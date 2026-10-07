"""Every read and write of the store that block 05 Dedupe makes.

Created: 2026-10-06
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context_v2.db.queries import BATCH, _batches

# Row shapes, in column order, for the writers below.
ParentRow = tuple[str, str, str, str | None, str]  # candidate_id, parent_id, reason, verdict_ref (NULL on a singleton), created_at
VerdictRow = tuple[str, str, str, int | None, float, str, str]  # candidate_a, candidate_b, signature, same_person, confidence, reason, judged_at


@dataclass(frozen=True)
class Identifier:
    kind: str
    normalized_value: str


@dataclass(frozen=True)
class SavedVerdict:
    candidate_a: str
    candidate_b: str
    signature: str
    same_person: int | None  # None = Sol said uncertain


def all_candidate_ids(conn: sqlite3.Connection) -> list[str]:
    """Every candidate, owner rows and candidates without facts included: each gets a parent."""
    ids: list[str] = []
    for row in conn.execute("SELECT candidate_id FROM candidates ORDER BY candidate_id"):
        ids.append(row["candidate_id"])
    return ids


def dossier_names(conn: sqlite3.Connection) -> dict[str, str]:
    """candidate_id -> the canonical name in its facts, for every candidate with facts."""
    names: dict[str, str] = {}
    for row in conn.execute("SELECT candidate_id, json_extract(facts_json, '$.canonical_name') AS name FROM facts"):
        names[row["candidate_id"]] = row["name"]
    return names


def candidate_identifiers(conn: sqlite3.Connection) -> dict[str, list[Identifier]]:
    """Every normalized email and phone of every candidate, sorted."""
    identifiers: dict[str, list[Identifier]] = {}
    for row in conn.execute(
        "SELECT candidate_id, kind, normalized_value FROM candidate_identifiers ORDER BY candidate_id, kind, normalized_value"
    ):
        identifiers.setdefault(row["candidate_id"], []).append(Identifier(row["kind"], row["normalized_value"]))
    return identifiers


def facts_fingerprints(conn: sqlite3.Connection) -> dict[str, str]:
    """candidate_id -> its facts input fingerprint, for every candidate with facts."""
    fingerprints: dict[str, str] = {}
    for row in conn.execute("SELECT candidate_id, input_fingerprint FROM facts"):
        fingerprints[row["candidate_id"]] = row["input_fingerprint"]
    return fingerprints


def facts_json(conn: sqlite3.Connection, candidate_ids: list[str]) -> dict[str, str]:
    """The facts of the given candidates only: the ones in a pair going to Sol."""
    found: dict[str, str] = {}
    for batch in _batches(candidate_ids, BATCH):
        marks: str = ",".join("?" * len(batch))
        for row in conn.execute(f"SELECT candidate_id, facts_json FROM facts WHERE candidate_id IN ({marks})", batch):
            found[row["candidate_id"]] = row["facts_json"]
    return found


def bundle_payloads(conn: sqlite3.Connection, candidate_ids: list[str]) -> dict[str, str]:
    """The bundles of the given candidates only, for their message samples."""
    found: dict[str, str] = {}
    for batch in _batches(candidate_ids, BATCH):
        marks: str = ",".join("?" * len(batch))
        for row in conn.execute(f"SELECT candidate_id, payload_json FROM bundles WHERE candidate_id IN ({marks})", batch):
            found[row["candidate_id"]] = row["payload_json"]
    return found


def current_parents(conn: sqlite3.Connection) -> dict[str, str]:
    """candidate_id -> its current parent id, for every candidate that has one."""
    parents: dict[str, str] = {}
    for row in conn.execute("SELECT candidate_id, parent_id FROM current_parent"):
        parents[row["candidate_id"]] = row["parent_id"]
    return parents


def saved_verdicts(conn: sqlite3.Connection) -> list[SavedVerdict]:
    """Every Sol answer ever saved, under every signature."""
    verdicts: list[SavedVerdict] = []
    for row in conn.execute("SELECT candidate_a, candidate_b, signature, same_person FROM pair_verdicts"):
        verdicts.append(SavedVerdict(row["candidate_a"], row["candidate_b"], row["signature"], row["same_person"]))
    return verdicts


def insert_pair_verdict(conn: sqlite3.Connection, row: VerdictRow) -> None:
    """One answer, written as it arrives. The same pair and evidence is never judged twice, so the key is new."""
    conn.execute(
        "INSERT INTO pair_verdicts (candidate_a, candidate_b, signature, same_person, confidence, reason, judged_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        row,
    )


def append_parent_rows(conn: sqlite3.Connection, rows: list[ParentRow]) -> None:
    """Appends; the ledger never updates or deletes."""
    for batch in _batches(rows, BATCH):
        conn.executemany(
            "INSERT INTO candidate_parent (candidate_id, parent_id, reason, verdict_ref, created_at) VALUES (?, ?, ?, ?, ?)",
            batch,
        )
