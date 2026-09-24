"""The share node: every merged person's labels, then who leaves the laptop.

Flow: people.csv + the deep-context leaves (facts with their JEV labels, index,
raw bundles, review worth) -> PersonEvidence -> deterministic labels + the saved
JEV labels + the confirm flag -> labels.csv -> the human's tags.csv ->
share_decision per person -> share.csv -> the stage manifest.

Free and local: JEV already answered during deep_synthesize. One pass writes
both files, so share.csv always covers the whole network.

Changelog:
  2026-09-24: share follows worth; the manifest counts the confirm rows a UI
    puts to the human.
  2026-09-24: created from the share CLI; became the `share` Node (labels.csv
    and share.csv in one pass, the `label` export folded in).
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    FACTS_TEMPLATE,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    RAW_BUNDLE_TEMPLATE,
)
from packs.ingestion.primitives.pipeline.contract import (
    STATUS_COMPLETED,
    Artifact,
    Node,
    StageManifest,
    row_model_for,
)
from packs.ingestion.primitives.share.csv_cells import cell_value
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.ingestion.primitives.share.labels import (
    confirm_flag,
    deterministic_labels,
    labels_from_saved,
    share_decision,
)
from packs.ingestion.primitives.share.models import (
    DETERMINISTIC_COLUMNS,
    FLAG_COLUMN,
    LABEL_COLUMNS,
    LABELS_FILENAME,
    MANIFEST_FILENAME,
    SHARE_DIR,
    SHARE_FILENAME,
    TAGS_FILENAME,
    DeterministicLabels,
    JevLabels,
    LabelRow,
    PersonEvidence,
)
from packs.ingestion.primitives.share.questions import CHOICE_LABELS, NOUL_LABELS, SCORE_LABELS
from packs.ingestion.primitives.share.tags import TagStore
from packs.ingestion.schemas.share_schema import (
    SHARE_COLUMNS,
    SHARE_CONFIRM,
    SHARE_NO,
    SHARE_YES,
)
from packs.shared.csv_io import CsvIO

LabelCsvRow = row_model_for("LabelCsvRow", list(LABEL_COLUMNS))
ShareCsvRow = row_model_for("ShareCsvRow", list(SHARE_COLUMNS))


class ShareManifest(StageManifest):
    source: str = "share"
    people: int = 0
    saved_labels: int = 0
    deterministic_only: int = 0
    share_yes: int = 0
    share_no: int = 0
    # Worth-yes people a flag fired on: the rows a UI puts to the human.
    confirm: int = 0
    by_reason: dict[str, int] = {}
    labels_csv: str = ""
    share_csv: str = ""
    elapsed_ms: int = 0
    updated_at: str = ""
    error: str = ""


class ShareList(Node):
    """Writes labels.csv and share.csv for every people.csv row. Free, local."""

    name = "share"
    # tags.csv is the human's file (the UI writes it); no node produces it.
    inputs = (
        Artifact(path=str(DEFAULT_PEOPLE_CSV)),
        Artifact(path=FACTS_TEMPLATE, required=False),
        Artifact(path=str(INDEX_JSON), required=False),
        Artifact(path=RAW_BUNDLE_TEMPLATE, required=False),
        Artifact(path=str(LINKEDIN_OVERRIDES_CSV), required=False),
        Artifact(path=str(SHARE_DIR / TAGS_FILENAME), external=True, required=False),
    )
    outputs = (
        Artifact(path=str(SHARE_DIR / LABELS_FILENAME), row_model=LabelCsvRow, writes="full_rewrite"),
        Artifact(path=str(SHARE_DIR / SHARE_FILENAME), row_model=ShareCsvRow, writes="full_rewrite"),
    )
    payload = ShareManifest
    manifest = str(SHARE_DIR / MANIFEST_FILENAME)

    def __init__(self, *, out_dir: Path = SHARE_DIR, evidence: ShareEvidence | None = None) -> None:
        self.out_dir = Path(out_dir)
        self.labels_csv = self.out_dir / LABELS_FILENAME
        self.share_csv = self.out_dir / SHARE_FILENAME
        self.tags_csv = self.out_dir / TAGS_FILENAME
        self.evidence = evidence or ShareEvidence()
        self.reference_date = date.today().isoformat()

    def bindings(self) -> dict[str, str]:
        evidence = self.evidence
        return {
            str(DEFAULT_PEOPLE_CSV): str(evidence.people_csv),
            FACTS_TEMPLATE: str(evidence.facts_dir / "{person_id}.jsonl"),
            str(INDEX_JSON): str(evidence.index_json),
            RAW_BUNDLE_TEMPLATE: str(evidence.raw_dir / "{person_id}.json"),
            str(LINKEDIN_OVERRIDES_CSV): str(evidence.overrides_csv),
            str(SHARE_DIR / TAGS_FILENAME): str(self.tags_csv),
            str(SHARE_DIR / LABELS_FILENAME): str(self.labels_csv),
            str(SHARE_DIR / SHARE_FILENAME): str(self.share_csv),
            self.manifest: str(self.out_dir / MANIFEST_FILENAME),
        }

    def execute(self) -> ShareManifest:
        started = time.monotonic()
        people = self.evidence.load()
        missing = [p.person_id for p in people if not p.linkedin_only and not (p.facts or {}).get("labels")]
        if missing:
            return ShareManifest(
                status="failed",
                people=len(people),
                error=f"{len(missing)} people have facts without JEV labels; run deep-context synthesize first",
                updated_at=now_iso(),
            )

        updated_at = now_iso()
        tags = TagStore(self.out_dir).load()
        label_rows: list[dict[str, Any]] = []
        share_rows: list[dict[str, str]] = []
        manifest = ShareManifest(
            status=STATUS_COMPLETED,
            people=len(people),
            labels_csv=str(self.labels_csv),
            share_csv=str(self.share_csv),
            updated_at=updated_at,
        )
        by_reason: dict[str, int] = {}
        for person in people:
            saved = (person.facts or {}).get("labels")
            jev = labels_from_saved(saved) if saved else None
            deterministic = deterministic_labels(person, reference_date=self.reference_date)
            flag = confirm_flag(jev)
            label_rows.append(_label_row(person, deterministic, jev, flag, updated_at))

            # A tag set before a merge is keyed by the id that merged away; the
            # surviving row is the only row that can still carry that decision.
            held = tags.get(person.person_id) or next(
                (tags[old] for old in person.superseded_person_ids if old in tags), None
            )
            decision = share_decision(_label_for_decision(person, deterministic, jev, flag), held, updated_at=updated_at)
            share_rows.append(decision.to_csv_row())

            manifest.saved_labels += int(jev is not None)
            manifest.deterministic_only += int(jev is None)
            manifest.share_yes += int(decision.share == SHARE_YES)
            manifest.share_no += int(decision.share == SHARE_NO)
            manifest.confirm += int(decision.share == SHARE_CONFIRM)
            by_reason[decision.reason] = by_reason.get(decision.reason, 0) + 1

        self.out_dir.mkdir(parents=True, exist_ok=True)
        CsvIO.write_dict_rows(self.labels_csv, list(LABEL_COLUMNS), label_rows)
        CsvIO.write_dict_rows(self.share_csv, list(SHARE_COLUMNS), share_rows)
        manifest.by_reason = by_reason
        manifest.elapsed_ms = int((time.monotonic() - started) * 1000)
        return manifest


def _label_for_decision(
    person: PersonEvidence, deterministic: DeterministicLabels, jev: JevLabels | None, flag: str | None
) -> LabelRow:
    return LabelRow(
        person_id=person.person_id,
        public_identifier=person.public_identifier,
        is_owner=deterministic.is_owner,
        worth=deterministic.network_worth,
        flag=flag,
        probabilities=dict(jev.probabilities) if jev else {},
    )


def _label_row(
    person: PersonEvidence,
    deterministic: DeterministicLabels,
    jev: JevLabels | None,
    flag: str | None,
    updated_at: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "person_id": person.person_id,
        "public_identifier": person.public_identifier or "",
        "full_name": person.full_name,
        **{name: cell_value(getattr(deterministic, name)) for name in DETERMINISTIC_COLUMNS},
        FLAG_COLUMN: flag or "",
        "updated_at": updated_at,
    }
    if jev is not None:
        row.update({name: jev.choices[name] for name in CHOICE_LABELS})
        row.update({f"{name}_p": f"{jev.choice_p[name]:.3f}" for name in CHOICE_LABELS})
        row.update({name: jev.scores[name] for name in SCORE_LABELS})
        row.update({name: f"{jev.probabilities[name]:.3f}" for name in NOUL_LABELS})
    return row
