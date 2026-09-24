"""Share-stage layout and the typed values every other module in the stage takes.

The stage writes one fixed directory and overwrites in place:

  .powerpacks/share/labels.csv    machine labels, one row per people.csv row
  .powerpacks/share/tags.csv      human tags, one row per tagged person
  .powerpacks/share/share.csv     the derived share list
  .powerpacks/share/manifest.json the `share` node's manifest

Flow: `evidence.py` parses people.csv + deep-context artifacts into
`PersonEvidence` -> `labels.py` renders `DeterministicLabels` + `JevLabels` ->
`share_list.py` (the node) writes labels.csv and share.csv.

Changelog:
  2026-09-24: the share node writes both files; no JEV cache here (synthesize's).
  2026-09-24: moved the share.csv contract and request state to their owners.
  2026-09-24: created.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.share.questions import CHOICE_LABELS, NOUL_LABELS, SCORE_LABELS
from packs.ingestion.schemas.share_schema import PRIVATE_SUGGESTED

SHARE_DIR = Path(".powerpacks/share")
LABELS_FILENAME = "labels.csv"
TAGS_FILENAME = "tags.csv"
SHARE_FILENAME = "share.csv"
MANIFEST_FILENAME = "manifest.json"

# Raw-bundle message channels that carry group traffic rather than DMs.
GROUP_CHANNELS = frozenset({"imessage_group"})


@dataclass(frozen=True)
class MessageStats:
    """Body-free counts from `deep-context/raw/<parent_id>.json`."""

    first_at: str | None
    last_at: str | None
    from_me: int
    from_them: int
    group_count: int
    channels: tuple[str, ...]


NO_MESSAGES = MessageStats(first_at=None, last_at=None, from_me=0, from_them=0, group_count=0, channels=())


@dataclass(frozen=True)
class PersonEvidence:
    """One people.csv row joined to its deep-context leaves. Absent = None."""

    person_id: str
    public_identifier: str | None
    full_name: str
    headline: str | None
    current_title: str | None
    current_company: str | None
    city: str | None
    state: str | None
    country: str | None
    source_channels: tuple[str, ...]
    interaction_counts: dict[str, int]
    last_interaction: str | None
    superseded_person_ids: tuple[str, ...]
    network_worth: str
    dossier: str | None
    # The dossier date, or facts file date when no dossier exists.
    evidence_date: str | None
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
    scores: dict[str, int]
    probabilities: dict[str, float]


@dataclass(frozen=True)
class LabelRow:
    """One labels.csv row parsed back at the boundary, for the share decision.

    `probabilities` is empty for a linkedin_only row — that person was never sent
    to Jev, so the noul cells are absent rather than zero.
    """

    person_id: str
    public_identifier: str | None
    is_owner: bool
    private_suggested: bool
    probabilities: dict[str, float]


@dataclass(frozen=True)
class HumanTags:
    """One tags.csv row. The human's word on a person; machines never write it."""

    person_id: str
    tags: frozenset[str]
    note: str | None
    updated_at: str


DETERMINISTIC_COLUMNS = tuple(field.name for field in fields(DeterministicLabels))

LABEL_COLUMNS = (
    "person_id",
    "public_identifier",
    "full_name",
    *DETERMINISTIC_COLUMNS,
    *CHOICE_LABELS,
    *tuple(f"{name}_p" for name in CHOICE_LABELS),
    *SCORE_LABELS,
    *NOUL_LABELS,
    PRIVATE_SUGGESTED,
    "private_reason",
    "updated_at",
)

TAG_COLUMNS = ("person_id", "tags", "note", "updated_at")
