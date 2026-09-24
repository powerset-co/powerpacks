"""tags.csv — the human's word on a person, and the lookup that finds them.

Only `bin/deep-context tag` writes this file; every machine stage reads it.
One row per tagged person: `person_id, tags, note, updated_at`.

Flow: `lookup_targets(name=…)` resolves a query to person ids through the
deep-context index; `TagStore.apply(...)` upserts one row; `TagStore.load()`
hands the share decision a `dict[person_id, HumanTags]`.

Changelog:
  2026-09-24: created.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import INDEX_JSON
from packs.ingestion.primitives.deep_context.lookup_person import PersonLookup
from packs.ingestion.primitives.share.labels import PRIVATE_TAG, SHARE_TAG
from packs.ingestion.primitives.share.models import TAG_COLUMNS, TAGS_CSV, HumanTags
from packs.ingestion.primitives.share.questions import NOUL_LABELS
from packs.shared.csv_io import CsvIO

# A human may assert any boolean-ish label Jev answers, plus the two decisions
# only a human makes. The choice/score labels are graded, not assertable.
TAG_VOCABULARY = frozenset(NOUL_LABELS) | {PRIVATE_TAG, SHARE_TAG}

TAG_SEPARATOR = "|"


@dataclass(frozen=True)
class TagTarget:
    """One person a lookup query resolved to."""

    person_id: str
    slug: str
    name: str


def lookup_targets(
    *, name: str = "", phone: str = "", email: str = "", index_json: Path = INDEX_JSON
) -> tuple[TagTarget, ...]:
    """Resolve a name/phone/email query to person ids through the deep-context index."""
    result = PersonLookup(name=name, phone=phone, email=email, index_json=index_json).run()
    targets = []
    for match in result.matches:
        person_id = str(match.record.get("person_id") or "").strip()
        if person_id:
            targets.append(TagTarget(person_id=person_id, slug=match.slug, name=match.label))
    return tuple(targets)


class TagStore:
    """Read/upsert tags.csv. Construct with the path, call load/apply."""

    def __init__(self, path: Path = TAGS_CSV) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, HumanTags]:
        rows: dict[str, HumanTags] = {}
        for row in CsvIO.read_dict_rows_normalized(self.path):
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
            self.path,
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
