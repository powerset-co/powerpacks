"""tags.csv — the human's word on a person.

The UI writes this file through `TagStore`; every machine stage reads it.
One row per tagged person: `person_id, tags, note, updated_at`.

Flow: `TagStore.apply(...)` upserts one row; `TagStore.load()` hands the share
decision a `dict[person_id, HumanTags]`.

Changelog:
  2026-09-24: dropped the tag CLI's person lookup; the UI resolves its own rows.
  2026-09-24: bound the tags path to the store output directory.
  2026-09-24: created.
"""

from __future__ import annotations

from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.share.labels import PRIVATE_TAG, SHARE_TAG
from packs.ingestion.primitives.share.models import SHARE_DIR, TAG_COLUMNS, TAGS_FILENAME, HumanTags
from packs.ingestion.primitives.share.questions import NOUL_LABELS
from packs.shared.csv_io import CsvIO

# A human may assert any boolean-ish label Jev answers, plus the two decisions
# only a human makes. The choice/score labels are graded, not assertable.
TAG_VOCABULARY = frozenset(NOUL_LABELS) | {PRIVATE_TAG, SHARE_TAG}

TAG_SEPARATOR = "|"


class TagStore:
    """Read/upsert tags.csv. Construct with the path, call load/apply."""

    def __init__(self, out_dir: Path = SHARE_DIR) -> None:
        self.tags_csv = Path(out_dir) / TAGS_FILENAME

    def load(self) -> dict[str, HumanTags]:
        rows: dict[str, HumanTags] = {}
        for row in CsvIO.read_dict_rows_normalized(self.tags_csv):
            person_id = row["person_id"].strip()
            if not person_id:
                continue
            rows[person_id] = HumanTags(
                person_id=person_id,
                tags=frozenset(t for t in row["tags"].split(TAG_SEPARATOR) if t),
                note=row["note"] or None,
                updated_at=row["updated_at"],
            )
        return rows

    def apply(self, person_id: str, *, add: set[str], remove: set[str], note: str | None) -> HumanTags:
        """Upsert one person's row. `note=None` keeps the note already there."""
        rows = self.load()
        current = rows.get(person_id)
        held = (current.tags if current else frozenset()) | add
        updated = HumanTags(
            person_id=person_id,
            tags=frozenset(held - remove),
            note=note if note is not None else (current.note if current else None),
            updated_at=now_iso(),
        )
        rows[person_id] = updated
        CsvIO.write_dict_rows(
            self.tags_csv,
            list(TAG_COLUMNS),
            [
                {
                    "person_id": row.person_id,
                    "tags": TAG_SEPARATOR.join(sorted(row.tags)),
                    "note": row.note or "",
                    "updated_at": row.updated_at,
                }
                for row in (rows[key] for key in sorted(rows))
            ],
        )
        return updated
