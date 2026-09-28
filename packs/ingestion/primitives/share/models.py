"""The typed values every module in the share stage takes.

The stage writes no file state: labels, the share list, and human tags are
tables in the canonical store (`.powerpacks/deep-context/deep-context.sqlite`).
Only the run manifest stays a file, in `.powerpacks/share/manifest.json`.

Flow: `evidence.py` parses people.csv + the canonical store into
`PersonEvidence` -> `labels.py` renders `DeterministicLabels` + `JevLabels` ->
`share_list.py` (the node) writes `person_labels` and `share`.

Changelog:
  2026-09-26: the noul probabilities persist unrounded: the share UI re-decides
    a tagged person from `person_labels`, and a rounded 0.5999 would cross
    ACTIVE_P and change `share.labels`.
  2026-09-24: labels/share/tags left CSV for SQLite tables.
  2026-09-24: labels carried one `flag` column; LabelRow carries worth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.share.questions import CHOICE_LABELS, NOUL_LABELS, SCORE_LABELS

SHARE_DIR = Path(".powerpacks/share")
MANIFEST_FILENAME = "manifest.json"
MANIFEST_PATH = SHARE_DIR / MANIFEST_FILENAME

# Raw-bundle message channels that carry group traffic rather than DMs.
GROUP_CHANNELS = frozenset({"imessage_group"})


@dataclass(frozen=True)
class MessageStats:
    """Body-free counts from the parent's projected source bundle."""

    first_at: str | None
    last_at: str | None
    from_me: int
    from_them: int
    group_count: int
    channels: tuple[str, ...]


NO_MESSAGES = MessageStats(first_at=None, last_at=None, from_me=0, from_them=0, group_count=0, channels=())


@dataclass(frozen=True)
class PersonEvidence:
    """One roster row joined to its canonical store leaves. Absent = None."""

    person_id: str
    public_identifier: str | None
    full_name: str
    source_channels: tuple[str, ...]
    interaction_counts: dict[str, int]
    last_interaction: str | None
    superseded_person_ids: tuple[str, ...]
    network_worth: str
    dossier: str | None
    facts: dict[str, Any] | None
    # `facts.shared_context[].overlap` values, parsed once here.
    shared_overlaps: frozenset[str]
    messages: MessageStats

    @property
    def linkedin_only(self) -> bool:
        """No synthesized context at all — deterministic labels only, no Jev call."""
        return self.facts is None and self.dossier is None

    @property
    def is_owner(self) -> bool:
        return bool((self.facts or {}).get("is_owner"))


@dataclass(frozen=True)
class DeterministicLabels:
    """Labels computed from body-free metadata alone; no LLM, always present."""

    cadence: str | None
    recency_days: int | None
    channels: str
    direction: str | None
    group_chat_only: bool
    linkedin_only: bool
    network_worth: str
    is_owner: bool
    shared_employer: bool
    shared_school: bool


@dataclass(frozen=True)
class JevLabels:
    """Jev's answers reduced to one value per question."""

    choices: dict[str, str]
    choice_p: dict[str, float]
    # Expected level (0.00-4.00 for warmth), not the argmax: the tiers spread.
    scores: dict[str, float]
    probabilities: dict[str, float]


@dataclass(frozen=True)
class LabelRow:
    """One person's label cells, for the share decision.

    `probabilities` is empty for a linkedin_only row — that person was never sent
    to Jev, so the noul cells are absent rather than zero.
    """

    person_id: str
    public_identifier: str | None
    is_owner: bool
    worth: str
    flag: str | None
    probabilities: dict[str, float]


@dataclass(frozen=True)
class HumanTags:
    """One `person_tags` row. The human's word on a person; machines never write it."""

    person_id: str
    tags: frozenset[str]
    note: str | None
    updated_at: str


DETERMINISTIC_COLUMNS = tuple(field.name for field in fields(DeterministicLabels))


def label_payload(deterministic: DeterministicLabels, jev: JevLabels | None) -> str:
    """Serialize one person's label cells flat, the shape the export table holds."""
    cells: dict[str, Any] = {name: getattr(deterministic, name) for name in DETERMINISTIC_COLUMNS}
    if jev is not None:
        cells.update(jev.choices)
        cells.update({f"{name}_p": round(jev.choice_p[name], 3) for name in CHOICE_LABELS})
        cells.update({name: round(jev.scores[name], 2) for name in SCORE_LABELS})
        cells.update({name: jev.probabilities[name] for name in NOUL_LABELS})
    return json.dumps(cells, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
