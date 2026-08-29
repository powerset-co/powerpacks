"""Stable canonical parent identity: get-or-create-or-absorb.

A ``parent_id`` is minted once — sha1 over the sorted person ids of the cluster
that founded it — and is opaque and immutable from then on. Later builds never
re-derive it from current membership, so a Gmail import that grows a cluster no
longer re-keys the parent (and every artifact hung off it).

Each cluster the unchanged clustering step produces resolves against the set E
of existing parents of its members:

    |E| = 0  mint a new id from the founding child set
    |E| = 1  absorb — the cluster keeps that id even though membership grew
    |E| > 1  merge — elect one survivor; ``Db.merge_parents`` repoints every
             dependent row onto it and deletes the absorbed parents in one
             transaction (no alias table)

Existing assignments come only from the canonical SQLite snapshot. Installs
that predate the database cross the one-time legacy import boundary first.

Splits are out of scope. An id is claimed by at most one cluster per build; a
cluster left with no unclaimed candidate mints a fresh id, which is exactly what
the old membership hash produced.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Sequence

from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.models import ParentFacts


def mint_parent_id(child_person_ids: Sequence[str]) -> str:
    """Mint one brand-new opaque id from a cluster's founding child person ids."""
    digest = hashlib.sha1("|".join(sorted(child_person_ids)).encode()).hexdigest()
    return f"parent-{digest[:12]}"


class ParentAssignment:
    """Resolve each cluster to one parent id: get, absorb, elect, or mint."""

    def __init__(
        self,
        parent_by_child: dict[str, str],
        facts: dict[str, ParentFacts],
    ) -> None:
        self.parent_by_child = parent_by_child
        self.facts = facts
        self.claimed: set[str] = set()

    def reserve(self, parent_id: str) -> None:
        """Proof harness only: reserve an owner id during migration replay.

        Removal countdown (2026-08-06): delete once no supported install
        predates powerpacks v1.19.0.
        """
        self.claimed.add(parent_id)

    def resolve(self, child_slugs: Sequence[str], child_person_ids: Sequence[str]) -> str:
        candidates = []
        for child_slug in child_slugs:
            parent_id = self.parent_by_child.get(child_slug, "")
            if parent_id and parent_id not in candidates and parent_id not in self.claimed:
                candidates.append(parent_id)
        parent_id = self.elect(candidates) if candidates else mint_parent_id(child_person_ids)
        self.claimed.add(parent_id)
        return parent_id

    def elect(self, candidates: list[str]) -> str:
        """Newest human worth decision wins, else most members, else smallest id."""
        best = max(self._rank(parent_id) for parent_id in candidates)
        return min(parent_id for parent_id in candidates if self._rank(parent_id) == best)

    def _rank(self, parent_id: str) -> tuple[bool, bool, IsoTimestamp, int]:
        """Rank known facts first, then decision recency and family size."""
        facts: ParentFacts | None = self.facts.get(parent_id)
        if facts is None:
            # The leading presence bit makes every absent row rank last.
            return False, False, "", 0
        return True, facts.decided, facts.decided_at, facts.members


def load_assignment(db: Db) -> ParentAssignment:
    """Existing child -> parent assignments from canonical SQLite."""
    parent_by_child: dict[str, str] = {}
    for person in queries.people(db):
        slug = str(person.child_slug or "").strip()
        if slug and person.parent_id:
            parent_by_child[slug] = person.parent_id
    members = Counter(parent_by_child.values())
    facts = {
        row.parent_id: ParentFacts(
            bool(row.human_worth is not None),
            row.human_worth_at or "",
            members.get(row.parent_id, 0),
        )
        for row in queries.parents(db)
    }
    for parent_id, count in members.items():
        facts.setdefault(parent_id, ParentFacts(False, "", count))
    return ParentAssignment(parent_by_child, facts)
