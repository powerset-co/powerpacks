"""Export machine labels already produced during deep-context worth.

Flow: load saved facts/labels -> write labels.csv and manifest (free, local).

Changelog:
  2026-09-24: created from the share CLI.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso, read_json, write_json
from packs.ingestion.primitives.share.csv_cells import cell_value
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.ingestion.primitives.share.labels import deterministic_labels, labels_from_saved, private_reason
from packs.ingestion.primitives.share.models import (
    DETERMINISTIC_COLUMNS, LABEL_COLUMNS, LABELS_FILENAME, MANIFEST_FILENAME, SHARE_DIR,
    DeterministicLabels, JevLabels, PersonEvidence,
)
from packs.ingestion.primitives.share.questions import (
    CHOICE_LABELS, NOUL_LABELS, SCORE_LABELS,
)
from packs.ingestion.schemas.share_schema import PRIVATE_SUGGESTED
from packs.shared.csv_io import CsvIO


def update_manifest(out_dir: Path, key: str, payload: dict[str, Any]) -> None:
    """Keep label and share counts in one manifest."""
    path = out_dir / MANIFEST_FILENAME
    manifest = read_json(path, {}) or {}
    manifest[key] = payload
    manifest["updated_at"] = payload["updated_at"]
    write_json(path, manifest)


class ShareLabels:
    """Build labels.csv from saved JEV labels for every people.csv row."""

    def __init__(
        self,
        *,
        estimate_only: bool = False,
        limit: int = 0,
        out_dir: Path = SHARE_DIR,
        evidence: ShareEvidence | None = None,
    ) -> None:
        self.estimate_only = estimate_only
        self.limit = limit
        self.out_dir = Path(out_dir)
        self.labels_csv = self.out_dir / LABELS_FILENAME
        self.evidence = evidence or ShareEvidence()
        self.reference_date = date.today().isoformat()

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        people = self.evidence.load(limit=self.limit)
        missing = [p.person_id for p in people if not p.linkedin_only and not (p.facts or {}).get("labels")]
        if missing:
            return {"primitive": "share_labels", "status": "failed",
                    "error": f"{len(missing)} people need JEV worth and labels; run deep-context synthesize first"}
        if self.estimate_only:
            return {"primitive": "share_labels", "status": "completed", "mode": "estimate",
                    "estimate": {"people": len(people), "cost_usd": 0, "uncached_calls": 0}}
        updated_at = now_iso()
        rows = []
        counts = {"people": len(people), "saved_labels": 0, "deterministic_only": 0, PRIVATE_SUGGESTED: 0}
        for person in people:
            saved = (person.facts or {}).get("labels")
            jev = labels_from_saved(saved) if saved else None
            counts["saved_labels" if jev else "deterministic_only"] += 1
            deterministic = deterministic_labels(person, reference_date=self.reference_date)
            reason = private_reason(deterministic, jev)
            counts[PRIVATE_SUGGESTED] += int(reason is not None)
            rows.append(_label_row(person, deterministic, jev, reason, updated_at))
        CsvIO.write_dict_rows(self.labels_csv, list(LABEL_COLUMNS), rows)
        payload = {"counts": counts, "elapsed_ms": int((time.monotonic() - started) * 1000),
                   "updated_at": updated_at}
        update_manifest(self.out_dir, "labels", payload)
        return {"primitive": "share_labels", "status": "completed", "labels_csv": str(self.labels_csv),
                "manifest": str(self.out_dir / MANIFEST_FILENAME), **payload}


def _label_row(
    person: PersonEvidence,
    deterministic: DeterministicLabels,
    jev: JevLabels | None,
    reason: str | None,
    updated_at: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "person_id": person.person_id,
        "public_identifier": person.public_identifier or "",
        "full_name": person.full_name,
        **{name: cell_value(getattr(deterministic, name)) for name in DETERMINISTIC_COLUMNS},
        PRIVATE_SUGGESTED: cell_value(reason is not None),
        "private_reason": reason or "",
        "updated_at": updated_at,
    }
    if jev is not None:
        row.update({name: jev.choices[name] for name in CHOICE_LABELS})
        row.update({f"{name}_p": f"{jev.choice_p[name]:.3f}" for name in CHOICE_LABELS})
        row.update({name: jev.scores[name] for name in SCORE_LABELS})
        row.update({name: f"{jev.probabilities[name]:.3f}" for name in NOUL_LABELS})
    return row
