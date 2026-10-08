"""Every read and write of the store that block 08 Review makes beyond the ones earlier blocks already name.

The current-state views rank rows over a whole ledger. Filtered by candidate id (their partition key)
SQLite pushes the filter inside and seeks the index; filtered by parent id it cannot, so the reads of one
family find its members from the candidate_parent indexes and then read every view by candidate id.

Created: 2026-10-07
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context_v2.db.queries_worth import MemberFacts
from packs.ingestion.primitives.deep_context_v2.db.schema import DecidedBy, ResearchStatus, Verdict, Worth

# The list: every worth-yes family on a p: id with no human LinkedIn row for any member, with whether it
# has a needs_review verdict or a usable research row. Each view is read once for the whole list.
QUEUE_SQL = f"""
WITH members AS (SELECT candidate_id, parent_id FROM current_parent WHERE parent_id LIKE 'p:%'),
yes AS (SELECT parent_id FROM current_worth WHERE worth = '{Worth.YES.value}' AND parent_id LIKE 'p:%'),
verdicts AS (
  SELECT m.parent_id, MAX(l.decided_by = '{DecidedBy.HUMAN.value}') AS human,
         MAX(l.verdict = '{Verdict.NEEDS_REVIEW.value}') AS needs_review
  FROM current_linkedins l JOIN members m USING (candidate_id) GROUP BY m.parent_id),
cards AS (SELECT DISTINCT parent_id FROM research
          WHERE status = '{ResearchStatus.NO_MATCH.value}' AND result_json IS NOT NULL)
SELECT y.parent_id, COALESCE(v.needs_review, 0) AS needs_review, c.parent_id IS NOT NULL AS has_card
FROM yes y LEFT JOIN verdicts v USING (parent_id) LEFT JOIN cards c USING (parent_id)
WHERE COALESCE(v.human, 0) = 0
ORDER BY y.parent_id
"""

# One family's members: the candidates whose latest parent row names it (two index seeks per member).
MEMBERS_SQL = """
SELECT c.candidate_id, c.display_name FROM candidate_parent m JOIN candidates c USING (candidate_id)
WHERE m.parent_id = ? AND m.seq = (SELECT MAX(seq) FROM candidate_parent WHERE candidate_id = m.candidate_id)
ORDER BY c.candidate_id
"""


@dataclass(frozen=True)
class QueueRow:
    parent_id: str
    needs_review: bool
    has_card: bool  # a usable no_match research row under the family's parent id, at some handle


@dataclass(frozen=True)
class Member:
    candidate_id: str
    display_name: str


@dataclass(frozen=True)
class IdentifierRow:
    kind: str
    normalized_value: str
    display_value: str


@dataclass(frozen=True)
class CurrentVerdict:
    """A member's current verdict on one LinkedIn member id."""

    candidate_id: str
    linkedin_url: str
    member_id: str
    origin: str
    verdict: str
    judgment_fingerprint: str
    confidence: float | None  # Sol's confidence; None when no Sol call made the row
    reason: str               # Sol's reason; '' when no Sol call made the row


@dataclass(frozen=True)
class ResearchCard:
    handle: str
    result_json: str


@dataclass(frozen=True)
class Queued:
    """One family's decision, waiting in the review queue."""

    parent_id: str
    decision: str
    key: str
    guidance: str


def _marks(values: list[str]) -> str:
    """One `?` per value, for an IN list."""
    return ", ".join(["?"] * len(values))


def queue(conn: sqlite3.Connection) -> list[QueueRow]:
    """The candidate list: every worth-yes p: family with no human LinkedIn row, with what it has pending."""
    rows: list[QueueRow] = []
    for row in conn.execute(QUEUE_SQL):
        rows.append(QueueRow(row["parent_id"], bool(row["needs_review"]), bool(row["has_card"])))
    return rows


def family_members(conn: sqlite3.Connection, parent_id: str) -> list[Member]:
    """The members of one family: candidate id and written name, in candidate order."""
    members: list[Member] = []
    for row in conn.execute(MEMBERS_SQL, (parent_id,)):
        members.append(Member(row["candidate_id"], row["display_name"]))
    return members


def identifiers(conn: sqlite3.Connection, candidate_ids: list[str]) -> list[IdentifierRow]:
    """Every email and phone of the given candidates, kind then value."""
    found: list[IdentifierRow] = []
    for row in conn.execute(
        f"SELECT kind, normalized_value, display_value FROM candidate_identifiers WHERE candidate_id IN ({_marks(candidate_ids)}) "
        "ORDER BY kind, normalized_value",
        candidate_ids,
    ):
        found.append(IdentifierRow(row["kind"], row["normalized_value"], row["display_value"]))
    return found


def sources(conn: sqlite3.Connection, candidate_ids: list[str]) -> list[str]:
    """The channels the given candidates were imported from, once each."""
    found: list[str] = []
    for row in conn.execute(
        f"SELECT DISTINCT source FROM candidate_sources WHERE candidate_id IN ({_marks(candidate_ids)}) ORDER BY source",
        candidate_ids,
    ):
        found.append(row["source"])
    return found


def message_counts(conn: sqlite3.Connection, candidate_ids: list[str]) -> dict[str, int]:
    """Messages per channel across the members' bundles, counted inside SQLite: no bundle reaches Python."""
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT json_extract(m.value, '$.channel') AS channel, COUNT(*) AS messages "
        f"FROM bundles b, json_each(b.payload_json, '$.messages') m WHERE b.candidate_id IN ({_marks(candidate_ids)}) "
        "GROUP BY channel ORDER BY channel",
        candidate_ids,
    ):
        counts[row["channel"]] = row["messages"]
    return counts


def member_facts(conn: sqlite3.Connection, parent_id: str, candidate_ids: list[str]) -> list[MemberFacts]:
    """The members' facts rows, in candidate order: the order worth and enrich collapse them in."""
    found: list[MemberFacts] = []
    for row in conn.execute(
        f"SELECT candidate_id, facts_json, synthesized_at FROM facts WHERE candidate_id IN ({_marks(candidate_ids)}) "
        "ORDER BY candidate_id",
        candidate_ids,
    ):
        found.append(MemberFacts(row["candidate_id"], parent_id, row["facts_json"], row["synthesized_at"]))
    return found


def labels_json(conn: sqlite3.Connection, candidate_ids: list[str]) -> str | None:
    """The family's latest machine worth labels; None when worth never judged a member."""
    row = conn.execute(
        f"SELECT labels_json FROM worth WHERE candidate_id IN ({_marks(candidate_ids)}) AND decided_by = ? "
        "ORDER BY seq DESC LIMIT 1",
        [*candidate_ids, DecidedBy.MACHINE.value],
    ).fetchone()
    if row is None:
        return None
    labels: str | None = row["labels_json"]
    return labels


def current_verdicts(conn: sqlite3.Connection, candidate_ids: list[str]) -> list[CurrentVerdict]:
    """The given candidates' current LinkedIn verdicts, oldest first."""
    found: list[CurrentVerdict] = []
    for row in conn.execute(
        "SELECT candidate_id, linkedin_url, member_id, origin, verdict, judgment_fingerprint, confidence, reason "
        "FROM current_linkedins "
        f"WHERE candidate_id IN ({_marks(candidate_ids)}) ORDER BY seq",
        candidate_ids,
    ):
        found.append(CurrentVerdict(row["candidate_id"], row["linkedin_url"], row["member_id"], row["origin"],
                                    row["verdict"], row["judgment_fingerprint"], row["confidence"], row["reason"]))
    return found


def research_card(conn: sqlite3.Connection, handle: str) -> ResearchCard | None:
    """The usable no_match research row at this handle, or None."""
    row = conn.execute(
        "SELECT handle, result_json FROM research WHERE handle = ? AND status = ? AND result_json IS NOT NULL",
        (handle, ResearchStatus.NO_MATCH.value),
    ).fetchone()
    if row is None:
        return None
    return ResearchCard(row["handle"], row["result_json"])


# The SQL the card runs per family, for the timing check's query plans.
VERDICTS_SQL = "SELECT * FROM current_linkedins WHERE candidate_id IN ({marks})"
MESSAGES_SQL = ("SELECT json_extract(m.value, '$.channel'), COUNT(*) FROM bundles b, "
                "json_each(b.payload_json, '$.messages') m WHERE b.candidate_id IN ({marks}) GROUP BY 1")


def first_family(conn: sqlite3.Connection) -> str:
    """The family with the most members: the heaviest card, for the timing check."""
    parent_id: str = conn.execute(
        "SELECT parent_id FROM current_parent GROUP BY parent_id ORDER BY COUNT(*) DESC, parent_id LIMIT 1"
    ).fetchone()["parent_id"]
    return parent_id


def query_plan(conn: sqlite3.Connection, sql: str, params: list[str]) -> list[str]:
    """EXPLAIN QUERY PLAN for one statement, as its detail lines."""
    lines: list[str] = []
    for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params):
        lines.append(row["detail"])
    return lines


# ---- writes: one decision is one transaction, opened by the caller


def insert_linkedin(conn: sqlite3.Connection, candidate_id: str, url: str, member_id: str, origin: str, verdict: str,
                    fingerprint: str, now: str) -> int:
    """One human verdict row; its seq names it in the parent row it confirms."""
    cursor = conn.execute(
        "INSERT INTO candidate_linkedins (candidate_id, linkedin_url, member_id, origin, verdict, decided_by, "
        "judgment_fingerprint, confidence, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, '', ?)",
        (candidate_id, url, member_id, origin, verdict, DecidedBy.HUMAN.value, fingerprint, now),
    )
    assert cursor.lastrowid is not None  # A successful INSERT into this rowid table sets lastrowid.
    return cursor.lastrowid


def insert_parent(conn: sqlite3.Connection, candidate_id: str, parent_id: str, reason: str, verdict_ref: str,
                  now: str) -> None:
    """One appended parent row for a human decision."""
    conn.execute(
        "INSERT INTO candidate_parent (candidate_id, parent_id, reason, verdict_ref, created_at) VALUES (?, ?, ?, ?, ?)",
        (candidate_id, parent_id, reason, verdict_ref, now),
    )


def review_queue(conn: sqlite3.Connection) -> dict[str, Queued]:
    """parent_id -> its queued decision."""
    queued: dict[str, Queued] = {}
    for row in conn.execute("SELECT parent_id, decision, key, guidance FROM review_queue"):
        queued[row["parent_id"]] = Queued(row["parent_id"], row["decision"], row["key"], row["guidance"])
    return queued


def upsert_queued(conn: sqlite3.Connection, queued: Queued, now: str) -> None:
    """The family's decision, replacing an earlier one."""
    conn.execute(
        "INSERT INTO review_queue (parent_id, decision, key, guidance, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (parent_id) DO UPDATE SET decision = excluded.decision, key = excluded.key, "
        "guidance = excluded.guidance, updated_at = excluded.updated_at",
        (queued.parent_id, queued.decision, queued.key, queued.guidance, now),
    )


def clear_queue(conn: sqlite3.Connection) -> None:
    """The queue after finish applied it: empty, so the next review starts over."""
    conn.execute("DELETE FROM review_queue")


def insert_worth(conn: sqlite3.Connection, candidate_id: str, worth: str, reason: str, now: str) -> None:
    """One human worth decision."""
    conn.execute(
        "INSERT INTO worth (candidate_id, worth, decided_by, reason, labels_json, input_fingerprint, created_at) "
        "VALUES (?, ?, ?, ?, NULL, NULL, ?)",
        (candidate_id, worth, DecidedBy.HUMAN.value, reason, now),
    )
