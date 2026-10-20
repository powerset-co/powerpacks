"""Step 5, settle: an own LinkedIn connection is worth yes.

An own connection is a family one of the owner's first-degree connections was proposed for and not
judged wrong: the owner chose to connect with this person, so they are in until a human says no. A
human worth row wins in the view, so nothing is written when a member has one, and a family already
at worth yes needs no row.

Changelog:
- 2026-10-13: `settle` applies the rule in one call, for Enrich and anything else that rewrites worth.
- 2026-10-07 (Arthur): the own-connection yes, dropped in the first port, restored.

Created: 2026-10-07
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries_worth
from packs.ingestion.primitives.deep_context_v2.db.queries_worth import WorthRow
from packs.ingestion.primitives.deep_context_v2.db.schema import DecidedBy, Origin, Verdict, Worth
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.enrich.family import Family, load_families
from packs.ingestion.primitives.deep_context_v2.worth import evidence

OWN_CONNECTION_REASON = "settle: own LinkedIn connection"


def own_connection(family: Family) -> bool:
    """One of the owner's first-degree connections was proposed for this family and not judged wrong."""
    for verdict in family.verdicts:
        if verdict.origin == Origin.LINKEDIN_NETWORK and verdict.verdict != Verdict.WRONG_PERSON:
            return True
    return False


def settle_rows(families: list[Family], now: str) -> list[WorthRow]:
    """One worth row per member for every family settle changes: yes for an own connection not already
    at yes. The family's worth labels ride along, so the People page keeps them."""
    rows: list[WorthRow] = []
    for family in families:
        if family.worth.decided_by == DecidedBy.HUMAN:
            continue  # a human row wins in the view
        if family.worth.worth == Worth.YES or not own_connection(family):
            continue
        key: str = evidence.version_key()
        for candidate_id in family.candidates:
            rows.append((candidate_id, Worth.YES.value, DecidedBy.MACHINE.value, OWN_CONNECTION_REASON,
                         family.worth.labels_json, key, now))
    return rows


def settle(conn: sqlite3.Connection, now: str) -> int:
    """Settle every family as it stands: the worth rows written. Enrich runs
    it after the judge; anything else that rewrites worth runs it after, so the rule holds."""
    families: list[Family] = load_families(conn)
    rows: list[WorthRow] = settle_rows(families, now)
    queries_worth.append_worth(conn, rows)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="07 Enrich step 5, settle own LinkedIn connections to worth yes: counts only. "
                                                 "Settle writes through enrich.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    args = parser.parse_args(argv)
    conn = open_store(store_path(args.data_root))
    families: list[Family] = load_families(conn)
    rows: list[WorthRow] = settle_rows(families, "")
    settled: dict[str, str] = {}
    for row in rows:
        settled[row[0]] = row[1]
    counts: dict[str, int] = {"families_to_yes": 0, "families_to_no": 0, "worth_rows": len(rows)}
    for family in families:
        worth: str | None = settled.get(family.candidates[0])
        if worth is not None:
            counts["families_to_" + worth] += 1
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
