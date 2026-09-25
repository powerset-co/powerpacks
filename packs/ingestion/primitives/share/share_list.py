"""The share node: every live person's labels, then who leaves the laptop.

Flow: people.csv + the canonical store (facts with their JEV labels, parents'
worth, dossier bodies, message bundles) -> PersonEvidence -> deterministic labels
+ the saved JEV labels + the confirm flag -> `person_labels` -> the human's
`person_tags` -> share decision per person -> `share` -> the stage manifest.

Free and local: JEV already answered during deep_synthesize. One pass writes both
tables, so `share` always covers the whole network.

Changelog:
  2026-09-24: labels and the share list became SQLite tables; no CSV state.
  2026-09-24: share follows worth; the manifest counts the confirm rows a UI
    puts to the human.
  2026-09-24: created from the share CLI.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.models import (
    PersonLabelRow,
    ShareDecisionRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.shared.common import CANONICAL_DB, DEFAULT_PEOPLE_CSV
from packs.ingestion.primitives.pipeline.contract import (
    STATUS_COMPLETED,
    Artifact,
    Node,
    StageManifest,
)
from packs.ingestion.primitives.share.evidence import ShareEvidence
from packs.ingestion.primitives.share.labels import (
    confirm_flag,
    deterministic_labels,
    labels_from_saved,
    share_decision,
)
from packs.ingestion.primitives.share.models import (
    MANIFEST_FILENAME,
    MANIFEST_PATH,
    SHARE_DIR,
    DeterministicLabels,
    HumanTags,
    JevLabels,
    LabelRow,
    PersonEvidence,
    label_payload,
)
from packs.ingestion.primitives.share.store import TagStore
from packs.ingestion.schemas.share_schema import SHARE_CONFIRM, SHARE_NO, SHARE_YES

# The labels export is missing until JEV has answered: worth alone is not the list.
MISSING_LABELS_ERROR = (
    "{count} people have facts without JEV labels; run deep-context synthesize first"
)


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
    elapsed_ms: int = 0
    updated_at: str = ""
    error: str = ""


def _label_row(
    person: PersonEvidence,
    deterministic: DeterministicLabels,
    jev: JevLabels | None,
    flag: str | None,
    updated_at: str,
) -> PersonLabelRow:
    return PersonLabelRow(
        person_id=person.person_id,
        public_identifier=person.public_identifier,
        full_name=person.full_name,
        worth=deterministic.network_worth,
        flag=flag,
        labels_json=label_payload(deterministic, jev),
        updated_at=updated_at,
    )


def _decision(
    person: PersonEvidence,
    deterministic: DeterministicLabels,
    jev: JevLabels | None,
    flag: str | None,
    held: HumanTags | None,
    updated_at: str,
) -> ShareDecisionRow:
    return share_decision(
        LabelRow(
            person_id=person.person_id,
            public_identifier=person.public_identifier,
            is_owner=deterministic.is_owner,
            worth=deterministic.network_worth,
            flag=flag,
            probabilities=dict(jev.probabilities) if jev else {},
        ),
        held,
        updated_at=updated_at,
    )


class ShareList(Node):
    """Writes `person_labels` and `share` for every roster row. Free, local."""

    name = "share"
    # `person_tags` is the human's table (the UI writes it); no node produces it.
    inputs = (
        Artifact(path=str(DEFAULT_PEOPLE_CSV), external=True),
        Artifact(path=str(CANONICAL_DB), external=True),
    )
    # The table writes are the deliverable; only the run manifest is a file.
    outputs = ()
    payload = ShareManifest
    manifest = str(MANIFEST_PATH)

    def __init__(
        self,
        *,
        db: Db,
        out_dir: Path | None = None,
        evidence: ShareEvidence | None = None,
    ) -> None:
        self.db = db
        self.evidence = evidence or ShareEvidence(db)
        self.out_dir = Path(out_dir) if out_dir is not None else SHARE_DIR
        self.reference_date = date.today().isoformat()

    def bindings(self) -> dict[str, str]:
        return {
            str(DEFAULT_PEOPLE_CSV): str(self.evidence.people_csv),
            str(CANONICAL_DB): str(self.db.db_path),
            str(MANIFEST_PATH): str(self.out_dir / MANIFEST_FILENAME),
        }

    def execute(self) -> ShareManifest:
        started = time.monotonic()
        people = self.evidence.load()
        missing = [
            person.person_id
            for person in people
            if not person.linkedin_only and not (person.facts or {}).get("labels")
        ]
        if missing:
            return ShareManifest(
                status="failed",
                people=len(people),
                error=MISSING_LABELS_ERROR.format(count=len(missing)),
                updated_at=now_iso(),
            )

        updated_at = now_iso()
        tags = TagStore(self.db).load()
        label_rows: list[PersonLabelRow] = []
        share_rows: list[ShareDecisionRow] = []
        by_reason: dict[str, int] = {}
        manifest = ShareManifest(
            status=STATUS_COMPLETED,
            people=len(people),
            updated_at=updated_at,
        )
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
            decision = _decision(person, deterministic, jev, flag, held, updated_at)
            share_rows.append(decision)

            manifest.saved_labels += int(jev is not None)
            manifest.deterministic_only += int(jev is None)
            manifest.share_yes += int(decision.share == SHARE_YES)
            manifest.share_no += int(decision.share == SHARE_NO)
            manifest.confirm += int(decision.share == SHARE_CONFIRM)
            by_reason[decision.reason] = by_reason.get(decision.reason, 0) + 1

        self.db.replace_share_rows(tuple(label_rows), tuple(share_rows))
        manifest.by_reason = by_reason
        manifest.elapsed_ms = int((time.monotonic() - started) * 1000)
        return manifest
