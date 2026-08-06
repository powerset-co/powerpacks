"""Atomic identity-family normalization shared by every SQLite writer."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable


def _clear_machine_winner_conflicts(
    conn: sqlite3.Connection,
    parent_ids: Iterable[str],
) -> None:
    """Leave conflicting machine winners pending instead of electing by completion order."""
    for parent_id in set(parent_ids):
        winners = conn.execute(
            "SELECT count(*) FROM links WHERE parent_id=? AND decision_action IS NULL "
            "AND machine_action IN ('verify', 'retarget') "
            "AND machine_approved IN ('auto', 'yes')",
            (parent_id,),
        ).fetchone()[0]
        if winners <= 1:
            continue
        conn.execute(
            "UPDATE links SET machine_approved=NULL WHERE parent_id=? "
            "AND decision_action IS NULL AND machine_action IN ('verify', 'retarget') "
            "AND machine_approved IN ('auto', 'yes')",
            (parent_id,),
        )
