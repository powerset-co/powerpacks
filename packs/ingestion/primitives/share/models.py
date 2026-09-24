"""Share-stage layout and the typed values every other module in the stage takes.

The stage writes one fixed directory and overwrites in place:

  .powerpacks/share/labels.csv    machine labels, one row per people.csv row
  .powerpacks/share/tags.csv      human tags, one row per tagged person
  .powerpacks/share/share.csv     the derived share list
  .powerpacks/share/manifest.json counts + versions
  .powerpacks/share/jev/<sha>.json  the Jev client's per-request cache

Flow: `share.py` parses people.csv + deep-context artifacts into `PersonEvidence`
(this file) -> `labels.py` renders `DeterministicLabels` + `JevLabels` ->
`share.py` writes the CSVs whose column tuples live here.

Changelog:
  2026-09-24: created.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.share.questions import CHOICE_LABELS, NOUL_LABELS, SCORE_LABELS

SHARE_DIR = Path(".powerpacks/share")
LABELS_CSV = SHARE_DIR / "labels.csv"
TAGS_CSV = SHARE_DIR / "tags.csv"
SHARE_CSV = SHARE_DIR / "share.csv"
MANIFEST_JSON = SHARE_DIR / "manifest.json"

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
    # The dossier's `generated_at` date — the Jev request's reference_date, so a
    # request only changes when its evidence does.
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

    def profile_state(self) -> dict[str, Any]:
        location = ", ".join(part for part in (self.city, self.state, self.country) if part)
        return {
            "name": self.full_name,
            "headline": self.headline,
            "title": self.current_title,
            "company": self.current_company,
            "location": location or None,
        }

    def channel_state(self) -> dict[str, Any]:
        return {
            "source_channels": list(self.source_channels),
            "interaction_counts": self.interaction_counts,
            "last_interaction": self.last_interaction,
            "first_message_at": self.messages.first_at,
            "last_message_at": self.messages.last_at,
            "from_me": self.messages.from_me,
            "from_them": self.messages.from_them,
            "group_count": self.messages.group_count,
        }

    def facts_state(self) -> dict[str, Any] | None:
        """The facts object minus `owned_identifiers` (the owner's own addresses)."""
        if self.facts is None:
            return None
        return {key: value for key, value in self.facts.items() if key != "owned_identifiers"}


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
    linkedin_only: bool
    is_owner: bool
    private_suggested: bool
    private_reason: str | None
    probabilities: dict[str, float]


@dataclass(frozen=True)
class HumanTags:
    """One tags.csv row. The human's word on a person; machines never write it."""

    person_id: str
    tags: frozenset[str]
    note: str | None
    updated_at: str


@dataclass(frozen=True)
class ShareRow:
    """One share.csv row — the contract the upload half reads."""

    person_id: str
    public_identifier: str | None
    share: str
    reason: str
    labels: tuple[str, ...]
    source: str


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
    "private_suggested",
    "private_reason",
    "updated_at",
)

TAG_COLUMNS = ("person_id", "tags", "note", "updated_at")

SHARE_COLUMNS = ("person_id", "public_identifier", "share", "reason", "labels", "source", "updated_at")
