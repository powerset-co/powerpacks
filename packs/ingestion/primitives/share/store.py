"""The human's tags in the v2 store: `person_tags`, one row per member candidate.

The People page writes through `TagStore`; every machine stage reads the table. Tags are the human's
word, so a machine never writes this table. A family's tags are the latest of its members' rows
(`current_tags`); a decision writes every member, so they agree.

Changelog:
  2026-10-07: v2 store, keyed by candidate id.
  2026-09-24: tags.csv became the `person_tags` table.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries_share
from packs.ingestion.primitives.share.labels import PRIVATE_TAG, SHARE_TAG
from packs.ingestion.primitives.share.models import HumanTags, ShareDecisionRow
from packs.ingestion.primitives.share.questions import NOUL_LABELS

# A human may assert any boolean-ish label Jev answers, plus the two decisions
# only a human makes. The choice/score labels are graded, not assertable.
TAG_VOCABULARY = frozenset(NOUL_LABELS) | {PRIVATE_TAG, SHARE_TAG}
TAG_SEPARATOR = "|"


def join_tags(tags: frozenset[str]) -> str:
    return TAG_SEPARATOR.join(sorted(tags))


def split_tags(value: str) -> frozenset[str]:
    return frozenset(tag for tag in value.split(TAG_SEPARATOR) if tag)


class TagStore:
    """Read `person_tags` by family."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def load(self) -> dict[str, HumanTags]:
        """parent_id -> the family's tags."""
        held: dict[str, HumanTags] = {}
        for parent_id, row in queries_share.current_tags(self.conn).items():
            held[parent_id] = HumanTags(person_id=parent_id, tags=split_tags(row.tags), note=row.note, updated_at=row.updated_at)
        return held


def share_rows(store: Path) -> tuple[ShareDecisionRow, ...]:
    """Every family's current share decision, read from the store at `store` for the upload: one row per
    export row (people.csv id = parent id)."""
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows: list[ShareDecisionRow] = []
    for row in queries_share.current_share(conn):
        rows.append(ShareDecisionRow(row.parent_id, row.public_identifier, row.share, row.reason, row.labels, row.source, row.updated_at))
    conn.close()
    return tuple(rows)
