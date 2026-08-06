"""Atomic identity-family normalization shared by every SQLite writer."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
    HUMAN_DECISION_SOURCES,
    ReviewAction,
    ReviewSource,
)


@dataclass(frozen=True)
class _EffectiveIdentityDecision:
    """One resolved decision plus the candidate URL presented for review."""

    action: str
    approved: str
    url: str
    public_identifier: str
    new_url: str
    new_public_identifier: str


class IdentityPolicy:
    """Atomic normalization applied by every canonical identity writer."""

    @staticmethod
    def effective_decision(
        *,
        decision_action: str | None,
        decision_approved: str | None,
        replacement_url: str | None,
        replacement_public_identifier: str | None,
        machine_action: str | None,
        machine_approved: str | None,
        machine_proposed_url: str | None,
        machine_proposed_public_identifier: str | None,
        linkedin_url: str | None,
        public_identifier: str | None,
    ) -> _EffectiveIdentityDecision:
        """Resolve human first, then a cleared machine decision, else pending.

        An uncleared machine retarget remains presentation-only: its proposed URL
        is shown to the reviewer, but it is not an effective decision and cannot be
        realized. This keeps human ``decision_*`` authoritative while making a
        recorded ``machine_approved`` value the only machine acceptance signal.
        """
        human = bool(decision_action)
        machine = bool(
            not human
            and machine_action
            and machine_approved
            in {
                ApprovedState.AUTO.value,
                ApprovedState.YES.value,
                ApprovedState.NO.value,
            }
        )
        action = str(
            decision_action if human else machine_action if machine else ""
        )
        approved = str(
            decision_approved if human else machine_approved if machine else ""
        )
        retarget_url = (
            replacement_url
            if human and action == ReviewAction.RETARGET.value
            else machine_proposed_url
            if machine and action == ReviewAction.RETARGET.value
            else None
        )
        retarget_public_identifier = (
            replacement_public_identifier
            if human and action == ReviewAction.RETARGET.value
            else machine_proposed_public_identifier
            if machine and action == ReviewAction.RETARGET.value
            else None
        )
        pending_url = (
            machine_proposed_url
            if not human
            and not machine
            and machine_action == ReviewAction.RETARGET.value
            else None
        )
        pending_public_identifier = (
            machine_proposed_public_identifier
            if not human
            and not machine
            and machine_action == ReviewAction.RETARGET.value
            else None
        )
        return _EffectiveIdentityDecision(
            action=action,
            approved=approved,
            url=str(retarget_url or pending_url or linkedin_url or ""),
            public_identifier=str(
                retarget_public_identifier
                or pending_public_identifier
                or public_identifier
                or ""
            ),
            new_url=str(retarget_url or pending_url or ""),
            new_public_identifier=str(
                retarget_public_identifier or pending_public_identifier or ""
            ),
        )

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
