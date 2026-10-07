"""Every read and write of the store that block 07 Enrich makes beyond the ones blocks 05 and 06 already name.

Created: 2026-10-07
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context_v2.db.queries import BATCH, _batches
from packs.ingestion.primitives.deep_context_v2.db.schema import DecidedBy

# Row shapes, in column order, for the writers below.
LinkedinRow = tuple[str, str, str, str, str, str, str, str]  # candidate_id, linkedin_url, member_id, origin, verdict, decided_by, judgment_fingerprint, created_at
ResearchRow = tuple[str, str, str, str | None, str]       # handle, parent_id, status, result_json (NULL on failed), researched_at


@dataclass(frozen=True)
class FamilyWorth:
    """A family's current worth: the human row when a member has one, else the latest machine row."""

    worth: str
    decided_by: str


@dataclass(frozen=True)
class LinkedinVerdict:
    """A candidate's current verdict on one LinkedIn member id."""

    seq: int
    candidate_id: str
    linkedin_url: str
    member_id: str
    origin: str
    verdict: str
    decided_by: str


@dataclass(frozen=True)
class Research:
    status: str
    result_json: str | None  # None = failed, or a no_match with nothing usable


def family_worth(conn: sqlite3.Connection) -> dict[str, FamilyWorth]:
    """parent_id -> its current worth, for every family with a worth row."""
    worth: dict[str, FamilyWorth] = {}
    for row in conn.execute("SELECT parent_id, worth, decided_by FROM current_worth"):
        worth[row["parent_id"]] = FamilyWorth(row["worth"], row["decided_by"])
    return worth


def connection_emails(conn: sqlite3.Connection) -> dict[str, str]:
    """LinkedIn URL -> the lowercased email the export carries, for the connections that have one."""
    emails: dict[str, str] = {}
    for row in conn.execute("SELECT linkedin_url, email FROM connections WHERE email IS NOT NULL AND email <> ''"):
        emails[row["linkedin_url"]] = row["email"].strip().lower()
    return emails


def current_linkedins(conn: sqlite3.Connection) -> list[LinkedinVerdict]:
    """Every candidate's current verdict per member id: a human row above every machine row, then the latest."""
    verdicts: list[LinkedinVerdict] = []
    for row in conn.execute(
        "SELECT seq, candidate_id, linkedin_url, member_id, origin, verdict, decided_by FROM current_linkedins "
        "ORDER BY candidate_id, member_id"
    ):
        verdicts.append(LinkedinVerdict(row["seq"], row["candidate_id"], row["linkedin_url"], row["member_id"],
                                        row["origin"], row["verdict"], row["decided_by"]))
    return verdicts


def machine_judgments(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """(candidate_id, judgment_fingerprint) of every machine verdict ever written: what was already paid for."""
    judged: set[tuple[str, str]] = set()
    for row in conn.execute(
        "SELECT DISTINCT candidate_id, judgment_fingerprint FROM candidate_linkedins WHERE decided_by = ?",
        (DecidedBy.MACHINE.value,),
    ):
        judged.add((row["candidate_id"], row["judgment_fingerprint"]))
    return judged


def research_by_handle(conn: sqlite3.Connection) -> dict[str, Research]:
    found: dict[str, Research] = {}
    for row in conn.execute("SELECT handle, status, result_json FROM research"):
        found[row["handle"]] = Research(row["status"], row["result_json"])
    return found


def insert_linkedin(conn: sqlite3.Connection, row: LinkedinRow) -> int:
    """One verdict row, appended alone so its seq can be named by the parent row it confirms."""
    cursor = conn.execute(
        "INSERT INTO candidate_linkedins (candidate_id, linkedin_url, member_id, origin, verdict, decided_by, "
        "judgment_fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        row,
    )
    return int(cursor.lastrowid)


def append_linkedins(conn: sqlite3.Connection, rows: list[LinkedinRow]) -> None:
    """Verdicts are a ledger: every judgment is new rows."""
    for batch in _batches(rows, BATCH):
        conn.executemany(
            "INSERT INTO candidate_linkedins (candidate_id, linkedin_url, member_id, origin, verdict, decided_by, "
            "judgment_fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )


def upsert_research(conn: sqlite3.Connection, row: ResearchRow) -> None:
    """One research answer, written as it arrives. A handle is researched once; a rerun of a failed one overwrites it."""
    conn.execute(
        "INSERT INTO research (handle, parent_id, status, result_json, researched_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (handle) DO UPDATE SET parent_id = excluded.parent_id, status = excluded.status, "
        "result_json = excluded.result_json, researched_at = excluded.researched_at",
        row,
    )
