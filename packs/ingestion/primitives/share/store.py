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
from packs.ingestion.primitives.share.models import HumanTags, PersonLabelRow
from packs.ingestion.primitives.share.questions import NOUL_LABELS

# Choice and score labels are graded; the human may assert boolean labels.
TAG_VOCABULARY = frozenset(NOUL_LABELS)
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


def share_rows(store: Path) -> tuple[PersonLabelRow, ...]:
    """The upload list: effective worth yes, excluding the owner and unreviewed family flags."""
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        labels = queries_share.labels_by_parent(conn)
        rows = conn.execute(
            "SELECT p.parent_id, w.decided_by, MAX(l.flag != '') AS flagged "
            "FROM current_parent p JOIN person_labels l USING (candidate_id) "
            "JOIN current_worth w USING (parent_id) "
            "WHERE w.worth = 'yes' "
            "AND NOT EXISTS (SELECT 1 FROM current_parent members JOIN candidates c USING (candidate_id) "
            "WHERE members.parent_id = p.parent_id AND c.is_owner = 1) "
            "GROUP BY p.parent_id "
            "HAVING MAX(COALESCE(json_extract(l.labels_json, '$.is_owner'), 0)) = 0 "
            "ORDER BY p.parent_id"
        ).fetchall()
        people: list[PersonLabelRow] = []
        for row in rows:
            parent_id = row["parent_id"]
            label = labels[parent_id]
            flagged = bool(label.flag) if label.relationship_kind_source == "human" else row["flagged"]
            if flagged and row["decided_by"] != "human":
                continue
            people.append(PersonLabelRow(parent_id, label.public_identifier, label.full_name, "yes", label.flag,
                                         label.labels_json, label.updated_at))
        return tuple(people)
    finally:
        conn.close()
