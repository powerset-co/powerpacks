"""Deterministic labels, saved Luna labels and the family, partner and minor flags."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from packs.ingestion.primitives.share.models import (
    GROUP_CHANNELS,
    DeterministicLabels,
    Labels,
    PersonEvidence,
)
from packs.ingestion.primitives.share.questions import (
    CHOICE_LABELS,
    NOUL_LABELS,
    SCORE_LABELS,
)
from packs.ingestion.schemas.share_schema import FAMILY, MINOR, ROMANTIC_PARTNER

# Cadence bands, first rule wins: staleness before volume.
DORMANT_DAYS = 730
STALE_DAYS = 365
FREQUENT_MESSAGES = 200
REGULAR_MESSAGES = 30

# Direction bands on the owner's share of the messages.
THEY_INITIATE_BELOW = 0.35
I_INITIATE_ABOVE = 0.65

# A noul label is "active" (listed on the People row) at this probability.
ACTIVE_P = 0.6
# is_family or is_minor keeps a family home at this probability — a lower bar, because a
# needless hold costs one click and a false share cannot be taken back.
FLAG_P = 0.5

# `facts.shared_context[].overlap` values that mean a shared employer / school.
EMPLOYER_OVERLAP = "employer"
SCHOOL_OVERLAP = "school"


def _recency_days(last_interaction: str | None, reference_date: str) -> int | None:
    if not last_interaction:
        return None
    return (date.fromisoformat(reference_date) - datetime.fromisoformat(last_interaction).date()).days


def _cadence(recency_days: int | None, total_interactions: int) -> str | None:
    if recency_days is None:
        return None
    if recency_days > DORMANT_DAYS:
        return "dormant"
    if recency_days > STALE_DAYS:
        return "stale"
    if total_interactions >= FREQUENT_MESSAGES:
        return "frequent"
    if total_interactions >= REGULAR_MESSAGES:
        return "regular"
    return "occasional"


def _direction(from_me: int, from_them: int) -> str | None:
    total = from_me + from_them
    if not total:
        return None
    mine = from_me / total
    if mine < THEY_INITIATE_BELOW:
        return "they_initiate"
    if mine > I_INITIATE_ABOVE:
        return "i_initiate"
    return "mutual"


def deterministic_labels(person: PersonEvidence, *, reference_date: str) -> DeterministicLabels:
    """The labels that need no LLM. Every person gets these, LinkedIn-only included."""
    recency_days = _recency_days(person.last_interaction, reference_date)
    return DeterministicLabels(
        cadence=_cadence(recency_days, sum(person.interaction_counts.values())),
        recency_days=recency_days,
        channels="|".join(person.source_channels),
        direction=_direction(person.messages.from_me, person.messages.from_them),
        group_chat_only=bool(person.messages.channels) and set(person.messages.channels) <= GROUP_CHANNELS,
        linkedin_only=False,
        network_worth=person.network_worth,
        is_owner=person.is_owner,
        shared_employer=EMPLOYER_OVERLAP in person.shared_overlaps,
        shared_school=SCHOOL_OVERLAP in person.shared_overlaps,
    )


def labels_from_saved(labels: dict[str, Any]) -> Labels:
    """Parse the labels worth saved: one value per question."""
    return Labels(
        choices={name: str(labels[name]) for name in CHOICE_LABELS},
        choice_p={name: float(labels[f"{name}_p"]) for name in CHOICE_LABELS},
        scores={name: float(labels[name]) for name in SCORE_LABELS},
        # A yes/no question Luna declined has no label.
        probabilities={name: float(labels[name]) for name in NOUL_LABELS if name in labels},
    )


def flag(labels: Labels | None) -> str:
    """The first rule that keeps a worth-yes family home, or ''. A family worth never judged has none."""
    if labels is None:
        return ""
    if labels.choices["relationship_kind"] == "family" or labels.probabilities.get("is_family", 0.0) >= FLAG_P:
        return FAMILY
    if labels.choices["relationship_kind"] == "romantic_partner":
        return ROMANTIC_PARTNER
    if labels.probabilities.get("is_minor", 0.0) >= FLAG_P:
        return MINOR
    return ""
