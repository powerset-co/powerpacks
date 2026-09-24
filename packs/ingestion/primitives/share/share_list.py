"""Derive the complete share.csv list from labels, tags, and people.csv.

Flow: read labels and tags -> decide each person -> write share.csv and manifest.

Changelog:
  2026-09-24: created from the share CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import DEFAULT_PEOPLE_CSV, parse_list
from packs.ingestion.primitives.share.csv_cells import cell_text
from packs.ingestion.primitives.share.label import update_manifest
from packs.ingestion.primitives.share.labels import share_decision
from packs.ingestion.primitives.share.models import (
    LABELS_FILENAME, MANIFEST_FILENAME, SHARE_DIR, SHARE_FILENAME, LabelRow,
)
from packs.ingestion.primitives.share.questions import NOUL_LABELS
from packs.ingestion.primitives.share.tags import TagStore
from packs.ingestion.schemas.share_schema import PRIVATE_SUGGESTED, SHARE_COLUMNS
from packs.shared.csv_io import CsvIO


class ShareList:
    """Rebuild share.csv from labels.csv + tags.csv + people.csv. Free, local."""

    def __init__(self, *, out_dir: Path = SHARE_DIR, people_csv: Path = DEFAULT_PEOPLE_CSV) -> None:
        self.out_dir = Path(out_dir)
        self.share_csv = self.out_dir / SHARE_FILENAME
        self.labels_csv = self.out_dir / LABELS_FILENAME
        self.people_csv = Path(people_csv)

    def run(self) -> dict[str, Any]:
        labels = _load_label_rows(self.labels_csv)
        people = _people_order(self.people_csv)
        # share.csv is the whole network or nothing: the upload reconciles the
        # cloud to it, so a list missing people (after `label --limit N`) would
        # un-share everyone it omits.
        unlabeled = [person_id for person_id, _ in people if person_id not in labels]
        if unlabeled:
            return {
                "primitive": "share_list",
                "status": "failed",
                "error": f"{len(unlabeled)} of {len(people)} people have no label row; run `label` for everyone first",
            }
        tags = TagStore(self.out_dir).load()
        updated_at = now_iso()
        rows: list[dict[str, Any]] = []
        reasons: dict[str, int] = {}
        counts = {"share_yes": 0, "share_no": 0}
        for person_id, superseded in people:
            # A tag set before a merge is keyed by the id that merged away; the
            # surviving row is the only row that can still carry that decision.
            held = tags.get(person_id) or next((tags[old] for old in superseded if old in tags), None)
            decision = share_decision(labels[person_id], held, updated_at=updated_at)
            counts["share_yes" if decision.share else "share_no"] += 1
            reasons[decision.reason] = reasons.get(decision.reason, 0) + 1
            rows.append(decision.to_csv_row())
        CsvIO.write_dict_rows(self.share_csv, list(SHARE_COLUMNS), rows)
        payload = {"counts": {**counts, "by_reason": reasons}, "updated_at": updated_at}
        update_manifest(self.out_dir, "share", payload)
        return {
            "primitive": "share_list",
            "status": "completed",
            "share_csv": str(self.share_csv),
            "manifest": str(self.out_dir / MANIFEST_FILENAME),
            **payload,
        }


def _people_order(people_csv: Path) -> list[tuple[str, tuple[str, ...]]]:
    return [
        (str(row.get("id") or "").strip(), tuple(parse_list(row.get("superseded_person_ids"))))
        for row in CsvIO.read_dict_rows(people_csv)
        if str(row.get("id") or "").strip()
    ]


def _load_label_rows(path: Path) -> dict[str, LabelRow]:
    rows: dict[str, LabelRow] = {}
    for row in CsvIO.read_dict_rows_normalized(path):
        person_id = row["person_id"].strip()
        if not person_id:
            continue
        rows[person_id] = LabelRow(
            person_id=person_id,
            public_identifier=cell_text(row.get("public_identifier")),
            is_owner=row.get("is_owner") == "yes",
            private_suggested=row.get(PRIVATE_SUGGESTED) == "yes",
            probabilities={name: float(row[name]) for name in NOUL_LABELS if row.get(name)},
        )
    return rows
