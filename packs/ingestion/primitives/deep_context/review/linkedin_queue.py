"""The LinkedIn review queue, held in the server's memory.

Who is still to be checked, in card order (parent id and slug). Nothing in the store says
"pending": it is derived from every identity link, which costs a fraction of a second on a
large store, so the server derives it once and keeps the answer.

    load     read the queue from the store: when Enrich finishes, when a re-research
             finishes, and on the first read after a restart or a worth decision
    rows     the queue as it stands
    settle   a decision was written for a parent: it leaves, unless one of its
             candidates is still pending; a parent a reset made pending again comes back
    drop     a parent the store says is no longer pending leaves
    forget   who is pending may have changed (a worth decision): the next read loads

A write the server did not make (another process) is not seen until the next load; a card
it would have served is checked against the store when it is hydrated.

Changelog:
  2026-10-01: created. A LinkedIn decision re-read the whole queue to pick the next card.
"""

from __future__ import annotations

import threading

from packs.ingestion.primitives.deep_context.db.identity_views import (
    linkedin_parent_pending,
    linkedin_queue_order,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.view_models import LinkedInQueueRow


class LinkedinQueue:
    def __init__(self, db: Db) -> None:
        self._db = db
        self._rows: tuple[LinkedInQueueRow, ...] | None = None
        self._lock = threading.Lock()

    def load(self) -> tuple[LinkedInQueueRow, ...]:
        # Read inside the lock: reads land in the order they were made, so a slow one
        # never replaces a newer one.
        with self._lock:
            self._rows = tuple(linkedin_queue_order(self._db))
            return self._rows

    def forget(self) -> None:
        with self._lock:
            self._rows = None

    def rows(self) -> tuple[LinkedInQueueRow, ...]:
        with self._lock:
            rows = self._rows
        return self.load() if rows is None else rows

    def slug(self, parent_id: str) -> str | None:
        """A queued parent's slug; None for a parent that is not queued."""
        return next((row.slug for row in self.rows() if row.parent_id == parent_id), None)

    def settle(self, parent_id: str) -> None:
        # Checked and changed under one lock, so the last settle leaves what the store has.
        with self._lock:
            if self._rows is None:
                return

            queued = any(row.parent_id == parent_id for row in self._rows)
            pending = linkedin_parent_pending(self._db, parent_id)
            if queued and not pending:
                self._rows = _without(self._rows, parent_id)
            if pending and not queued:
                # Back in the queue at its place in the card order, which only the store knows.
                self._rows = tuple(linkedin_queue_order(self._db))

    def drop(self, parent_id: str) -> None:
        with self._lock:
            if self._rows is not None:
                self._rows = _without(self._rows, parent_id)


def _without(rows: tuple[LinkedInQueueRow, ...], parent_id: str) -> tuple[LinkedInQueueRow, ...]:
    return tuple(row for row in rows if row.parent_id != parent_id)
