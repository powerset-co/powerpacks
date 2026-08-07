"""Canonical worth rows, review queue, and counts."""

from __future__ import annotations

from typing import Literal

from packs.ingestion.primitives.deep_context.db._view_rows import _worth_review
from packs.ingestion.primitives.deep_context.db.view_models import (
    WorthCounts,
    WorthMachineRow,
    WorthRow,
    WorthSummary,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError

__all__ = ["WorthCounts", "WorthMachineRow", "WorthRow", "WorthSummary", "worth_review"]


def worth_review(
    db: Db,
    scope: Literal["rows", "queue", "counts"],
) -> list[WorthRow] | WorthCounts:
    """Read one scope from the single canonical worth policy."""
    try:
        return _worth_review(db, scope)
    except ValueError as exc:
        raise StoreError(str(exc)) from exc
