"""Typed reads for the human tags, the label export, and the share decisions."""

from __future__ import annotations

from packs.ingestion.primitives.deep_context.db.models import (
    PersonLabelRow,
    PersonTagRow,
    ShareDecisionRow,
)
from packs.ingestion.primitives.deep_context.db.queries import typed_rows
from packs.ingestion.primitives.deep_context.db.store import Db


def person_tags(db: Db) -> tuple[PersonTagRow, ...]:
    """Every human tag row; the human's own words, never a machine's."""
    return typed_rows(db, "SELECT * FROM person_tags ORDER BY person_id", PersonTagRow)


def person_labels(db: Db) -> tuple[PersonLabelRow, ...]:
    """The label export, one row per person, written whole by the share node."""
    return typed_rows(db, "SELECT * FROM person_labels ORDER BY person_id", PersonLabelRow)


def share_decisions(db: Db) -> tuple[ShareDecisionRow, ...]:
    """Who leaves the laptop for Powerset, one row per person."""
    return typed_rows(db, "SELECT * FROM share ORDER BY person_id", ShareDecisionRow)
