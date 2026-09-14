"""Canonical worth rows, review queue, and counts."""

from __future__ import annotations

from packs.ingestion.primitives.deep_context.db._view_rows import _worth_counts, _worth_rows
from packs.ingestion.primitives.deep_context.db.view_models import WorthCounts, WorthRow
from packs.ingestion.primitives.deep_context.db.store import Db


def worth_rows(db: Db) -> list[WorthRow]:
    return _worth_rows(db, pending_only=False)


def worth_queue(db: Db) -> list[WorthRow]:
    return _worth_rows(db, pending_only=True)


def worth_counts(db: Db) -> WorthCounts:
    return _worth_counts(db)
