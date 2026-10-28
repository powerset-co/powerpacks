"""Typed family evidence, labels and human tags for People and upload."""
from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import Any

from packs.ingestion.primitives.share.questions import CHOICE_LABELS, SCORE_LABELS

# Message channels that carry group traffic rather than DMs.
GROUP_CHANNELS = frozenset({"imessage_group"})


@dataclass(frozen=True)
class MessageStats:
    """Body-free counts from the family's bundles."""

    first_at: str | None
    last_at: str | None
    from_me: int
    from_them: int
    group_count: int
    channels: tuple[str, ...]


NO_MESSAGES = MessageStats(first_at=None, last_at=None, from_me=0, from_them=0, group_count=0, channels=())


@dataclass(frozen=True)
class PersonEvidence:
    """One family: its export row, its members, its worth and its facts. Absent = None."""

    person_id: str                       # the parent id, the export row's id
    candidate_ids: tuple[str, ...]       # the members; every table row is written for each
    public_identifier: str
    full_name: str
    source_channels: tuple[str, ...]
    interaction_counts: dict[str, int]
    last_interaction: str | None
    network_worth: str                   # the family's current worth: yes or no
    worth_labels: dict[str, Any] | None  # the labels worth saved; None when a human decided and no machine row exists
    facts: dict[str, Any]
    shared_overlaps: frozenset[str]      # `facts.shared_context[].overlap` values
    messages: MessageStats

    @property
    def is_owner(self) -> bool:
        return bool(self.facts.get("is_owner"))


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
class Labels:
    """Luna's answers reduced to one value per question."""

    choices: dict[str, str]
    choice_p: dict[str, float]
    # Expected level (0.00-4.00 for warmth), not the argmax: the tiers spread.
    scores: dict[str, float]
    probabilities: dict[str, float]


@dataclass(frozen=True)
class HumanTags:
    """The human's word on a family; machines never write it."""

    person_id: str
    tags: frozenset[str]
    note: str
    updated_at: str


@dataclass(frozen=True)
class PersonLabelRow:
    """One `person_labels` row: deterministic labels plus Luna's answers."""

    person_id: str
    public_identifier: str
    full_name: str
    worth: str
    flag: str
    labels_json: str
    updated_at: str


DETERMINISTIC_COLUMNS = tuple(field.name for field in fields(DeterministicLabels))


def label_payload(deterministic: DeterministicLabels, labels: Labels | None) -> str:
    """Serialize one person's label cells flat, the shape the export table holds."""
    cells: dict[str, Any] = {name: getattr(deterministic, name) for name in DETERMINISTIC_COLUMNS}
    if labels is not None:
        cells.update(labels.choices)
        cells.update({f"{name}_p": round(labels.choice_p[name], 3) for name in CHOICE_LABELS})
        cells.update({name: round(labels.scores[name], 2) for name in SCORE_LABELS})
        cells.update(labels.probabilities)
    return json.dumps(cells, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
