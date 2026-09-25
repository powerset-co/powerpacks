"""Canonical worth rows, review queue, and counts.

Changelog:
- 2026-09-25: `worth_row(db, key)` reads one row; the decision handler no longer loads every worth row.
"""

from __future__ import annotations

from packs.ingestion.primitives.deep_context.db._view_rows import _worth_counts, _worth_rows
from packs.ingestion.primitives.deep_context.db.models import PARENT_WORTH_PREFIX
from packs.ingestion.primitives.deep_context.db.view_models import WorthCounts, WorthRow
from packs.ingestion.primitives.deep_context.db.store import Db


def worth_rows(db: Db) -> list[WorthRow]:
    return _worth_rows(db, pending_only=False)


def worth_row(db: Db, key: str) -> WorthRow | None:
    """The one worth row a decision just wrote, by its parent-worth key."""
    rows = _worth_rows(db, pending_only=False, parent_id=key.removeprefix(PARENT_WORTH_PREFIX))
    return rows[0] if rows else None


def worth_queue(db: Db) -> list[WorthRow]:
    return _worth_rows(db, pending_only=True)


def worth_counts(db: Db) -> WorthCounts:
    return _worth_counts(db)
