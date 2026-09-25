"""The human's tags in the canonical store: `person_tags`, one row per person.

The UI writes through `TagStore`; every machine stage reads the table. Tags are
the human's word, so a machine never writes this table and a tag may name an id
that later merged away.

Flow: `TagStore.apply(...)` upserts one row; `TagStore.load()` hands the share
decision a `dict[person_id, HumanTags]`.

Changelog:
  2026-09-24: tags.csv became the `person_tags` table.
  2026-09-24: created.
"""

from __future__ import annotations

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.models import PersonTagRow
from packs.ingestion.primitives.deep_context.db.share_views import person_tags
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.share.labels import PRIVATE_TAG, SHARE_TAG
from packs.ingestion.primitives.share.models import HumanTags
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
    """Read/upsert `person_tags`. Construct with the store, call load/apply."""

    def __init__(self, db: Db) -> None:
        self.db = db

    def load(self) -> dict[str, HumanTags]:
        return {
            row.person_id: HumanTags(
                person_id=row.person_id,
                tags=split_tags(row.tags),
                note=row.note,
                updated_at=row.updated_at or "",
            )
            for row in person_tags(self.db)
        }

    def apply(self, person_id: str, *, add: set[str], remove: set[str], note: str | None) -> HumanTags:
        """Upsert one person's row. `note=None` keeps the note already there."""
        current = self.load().get(person_id)
        held = (current.tags if current else frozenset()) | add
        updated = HumanTags(
            person_id=person_id,
            tags=frozenset(held - remove),
            note=note if note is not None else (current.note if current else None),
            updated_at=now_iso(),
        )
        self.db.upsert_person_tag(
            PersonTagRow(
                person_id=person_id,
                tags=join_tags(updated.tags),
                note=updated.note,
                updated_at=updated.updated_at,
            )
        )
        return updated
