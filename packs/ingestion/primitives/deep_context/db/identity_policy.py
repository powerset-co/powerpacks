"""Atomic identity-family normalization shared by every SQLite writer."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from packs.ingestion.primitives.deep_context.db.models import (
    HUMAN_DECISION_SOURCES,
    ReviewSource,
)


class IdentityPolicy:
    """Atomic normalization applied by every canonical identity writer."""

    @staticmethod
    def clear_machine_winner_conflicts(
        conn: sqlite3.Connection,
        parent_ids: Iterable[str],
    ) -> None:
        """Leave conflicting machine winners pending instead of electing by order."""
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

    @staticmethod
    def settle_siblings(
        conn: sqlite3.Connection,
        parent_id: str,
        winner_key: str,
        decided_at: str | None,
    ) -> list[str]:
        """Force-detach every sibling through the one family-settlement policy."""
        siblings = [
            row["row_key"]
            for row in conn.execute(
                "SELECT row_key FROM links WHERE parent_id=? AND row_key!=? "
                "ORDER BY row_key",
                (parent_id, winner_key),
            )
        ]
        conn.execute(
            "UPDATE links SET decision_action='detach', decision_approved='yes', "
            "decision_source=?, decision_note=NULL, decided_at=?, replacement_url=NULL, "
            "replacement_public_identifier=NULL WHERE parent_id=? AND row_key!=?",
            (ReviewSource.SIBLING_SETTLE.value, decided_at, parent_id, winner_key),
        )
        return siblings

    @staticmethod
    def settle_human_families(
        conn: sqlite3.Connection,
        parent_ids: Iterable[str],
    ) -> None:
        """Extend each family's latest direct human decision to every sibling."""
        parents = tuple(sorted(set(parent_ids)))
        if not parents:
            return
        sources = tuple(sorted(HUMAN_DECISION_SOURCES))
        parent_slots = ",".join("?" for _ in parents)
        source_slots = ",".join("?" for _ in sources)
        winners: dict[str, sqlite3.Row] = {}
        rows = conn.execute(
            "SELECT row_key, parent_id, decided_at FROM links "
            f"WHERE parent_id IN ({parent_slots}) AND decision_action IS NOT NULL "
            f"AND decision_source IN ({source_slots}) "
            "ORDER BY decided_at DESC, row_key",
            (*parents, *sources),
        )
        for row in rows:
            winners.setdefault(row["parent_id"], row)
        for parent_id, winner in winners.items():
            IdentityPolicy.settle_siblings(
                conn,
                parent_id,
                winner["row_key"],
                winner["decided_at"],
            )
