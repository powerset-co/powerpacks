"""Queries and the settle transaction — every read is a query, every user
action is one transaction.

The settle rule, stated once: a click on one candidate settles the PARENT.
Siblings that already carry a decision row keep it (a decision row is
terminal — human or machine); siblings with NO identity decision row get a
sibling-withdrawal detach; synthetic siblings withdraw through their gate
unless a human already gated them ('auto' is machine-standing and yields).
Ghost rows need nothing special: they are links rows like any other — the
old per-namespace fan-out ceremony is replaced by the people JOIN.
"""
from __future__ import annotations

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.schema import (
    DecisionKind,
    DecisionRow,
    ReviewAction,
    ReviewSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db

_SIBLING_LINKS = """
    SELECT l.row_key FROM links l
    WHERE l.row_key != :clicked
      AND (l.person_id IN (SELECT p2.person_id FROM people p1
                           JOIN people p2 ON p2.parent_id = p1.parent_id
                           WHERE p1.person_id = :person_id)
           OR l.person_id = :person_id)
      AND NOT EXISTS (SELECT 1 FROM decisions d
                      WHERE d.kind = 'identity' AND d.target = l.row_key)
"""


def siblings_of(db: Db, person_id: str) -> list[str]:
    """Every links row_key belonging to the same canonical parent."""
    return [r["row_key"] for r in db.query(
        "SELECT l.row_key FROM links l JOIN people p ON p.person_id = l.person_id "
        "WHERE p.parent_id = (SELECT parent_id FROM people WHERE person_id = ?)",
        (person_id,))]


def settle_parent(db: Db, *, clicked_row_key: str, person_id: str,
                  decision: DecisionRow,
                  synthetic_withdraw_pubs: tuple[str, ...] = (),
                  decided_at: str = "") -> list[str]:
    """One transaction: the clicked decision plus the sibling withdrawals.
    Returns every row_key/pub the transaction settled."""
    decided_at = decided_at or now_iso()
    settled = [clicked_row_key]
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO decisions (kind, target, value, approved, source, note, decided_at) "
            "VALUES (:kind, :target, :value, :approved, :source, :note, :decided_at)",
            {**decision.__dict__, "decided_at": decided_at})
        siblings = [r["row_key"] for r in conn.execute(
            _SIBLING_LINKS, {"clicked": clicked_row_key, "person_id": person_id})]
        conn.executemany(
            "INSERT OR IGNORE INTO decisions (kind, target, value, approved, source, decided_at) "
            "VALUES ('identity', ?, 'detach', 'yes', ?, ?)",
            [(row_key, ReviewSource.REVIEW.value, decided_at) for row_key in siblings])
        settled.extend(siblings)
        for pub in synthetic_withdraw_pubs:
            # A machine-standing 'auto' gate yields to the settle; a human
            # yes/no gate stands (INSERT OR IGNORE after clearing only auto).
            conn.execute(
                "DELETE FROM decisions WHERE kind = 'synthetic_gate' AND target = ? "
                "AND approved = 'auto'", (pub,))
            changed = conn.execute(
                "INSERT OR IGNORE INTO decisions (kind, target, value, approved, source, decided_at) "
                "VALUES ('synthetic_gate', ?, 'no', 'yes', ?, ?)",
                (pub, ReviewSource.REVIEW.value, decided_at)).rowcount
            if changed:
                settled.append(pub)
    return settled


def identity_decision(*, target: str, decision: str, new_url: str = "",
                      new_pub: str = "", source: str = ReviewSource.REVIEW.value,
                      ) -> DecisionRow:
    """The clicked outcome as a typed row (keep/detach/exclude/fix mapping)."""
    value = {"keep": ReviewAction.VERIFY.value, "detach": ReviewAction.DETACH.value,
             "exclude": ReviewAction.EXCLUDE.value, "fix": ReviewAction.RETARGET.value,
             }.get(decision)
    if value is None:
        raise ValueError(f"unknown decision: {decision}")
    return DecisionRow(kind=DecisionKind.IDENTITY.value, target=target,
                       value=value, approved="yes", source=source)


def stage_progress(db: Db) -> dict[str, int]:
    """Pending counts straight from the store."""
    pending_links = db.query(
        "SELECT COUNT(*) AS n FROM links l WHERE NOT EXISTS "
        "(SELECT 1 FROM decisions d WHERE d.kind = 'identity' AND d.target = l.row_key)"
    )[0]["n"]
    pending_worth = db.query(
        "SELECT COUNT(*) AS n FROM parents p WHERE NOT EXISTS "
        "(SELECT 1 FROM decisions d WHERE d.kind = 'worth' AND d.target = p.parent_id)"
    )[0]["n"]
    return {"linkedin_pending": pending_links, "worth_pending": pending_worth}
