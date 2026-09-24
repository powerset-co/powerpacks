"""The share stage's whole policy: deterministic labels, Jev label reduction,
the private-suggestion table and the share decision.

Flow: `deterministic_labels(person)` -> cadence/direction/worth from metadata;
`labels_from_answers(answers)` -> one value per Jev question; `private_reason`
-> the first private rule that fires; `share_decision(row, tags)` -> the
yes/no + reason that `share.csv` carries.

Every threshold is a module constant here; nothing downstream re-derives one.

Changelog:
  2026-09-24: created.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable

from packs.ingestion.primitives.share.models import (
    GROUP_CHANNELS,
    DeterministicLabels,
    HumanTags,
    JevLabels,
    LabelRow,
    PersonEvidence,
    ShareRow,
)
from packs.ingestion.primitives.share.questions import CHOICE_LABELS, NOUL_LABELS, SCORE_LABELS

# Cadence bands, first rule wins: staleness before volume.
DORMANT_DAYS = 730
STALE_DAYS = 365
FREQUENT_MESSAGES = 200
REGULAR_MESSAGES = 30

# Direction bands on the owner's share of the messages.
THEY_INITIATE_BELOW = 0.35
I_INITIATE_ABOVE = 0.65

# A noul label is "active" (listed in share.csv, and blocking where it blocks) at
# this probability.
ACTIVE_P = 0.6
# A noul label suggests `private` at this probability — a lower bar, because a
# false private costs one tag and a false share cannot be taken back.
PRIVATE_P = 0.5

SHARE_YES = "yes"
SHARE_NO = "no"
PRIVATE_TAG = "private"
SHARE_TAG = "share"

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
        linkedin_only=person.linkedin_only,
        network_worth=person.network_worth,
        is_owner=person.is_owner,
        shared_employer=EMPLOYER_OVERLAP in person.shared_overlaps,
        shared_school=SCHOOL_OVERLAP in person.shared_overlaps,
    )


def labels_from_answers(answers: dict[str, dict]) -> JevLabels:
    """Reduce one Jev response: choice -> argmax option, score -> argmax level, noul -> p."""
    choices: dict[str, str] = {}
    choice_p: dict[str, float] = {}
    for name in CHOICE_LABELS:
        probabilities = answers[name]["probabilities"]
        best = max(probabilities, key=lambda option: probabilities[option])
        choices[name] = best
        choice_p[name] = float(probabilities[best])
    scores: dict[str, int] = {}
    for name in SCORE_LABELS:
        probabilities = answers[name]["probabilities"]
        scores[name] = int(max(probabilities, key=lambda level: probabilities[level]))
    return JevLabels(
        choices=choices,
        choice_p=choice_p,
        scores=scores,
        probabilities={name: float(answers[name]["noul"]) for name in NOUL_LABELS},
    )


# First rule wins; the rule NAME is the reason written to labels.csv.
_JEV_PRIVATE_RULES: tuple[tuple[str, Callable[[JevLabels], bool]], ...] = (
    ("family", lambda j: j.choices["relationship_kind"] == "family" or j.probabilities["is_family"] >= PRIVATE_P),
    ("romantic_partner", lambda j: j.choices["relationship_kind"] == "romantic_partner"),
    ("minor", lambda j: j.probabilities["is_minor"] >= PRIVATE_P),
    ("sensitive_context", lambda j: j.probabilities["sensitive_context"] >= PRIVATE_P),
    ("sensitive_provider",
     lambda j: j.probabilities["is_healthcare_legal_or_financial_provider"] >= PRIVATE_P),
)
# `confidential_dealings` stays a label, not a private rule: on a real network it
# fired on 87 people, 50 of them recruiters — hiring talk is ordinary here.


def private_reason(deterministic: DeterministicLabels, jev: JevLabels | None) -> str | None:
    """The first private rule that fires, or None. `owner` is last and needs no Jev."""
    for name, fired in _JEV_PRIVATE_RULES:
        if jev is not None and fired(jev):
            return name
    return "owner" if deterministic.is_owner else None


def active_labels(row: LabelRow) -> tuple[str, ...]:
    """The labels share.csv carries: every noul at or above ACTIVE_P, then the suggestion."""
    active = tuple(name for name in NOUL_LABELS if row.probabilities.get(name, 0.0) >= ACTIVE_P)
    return active + (("private_suggested",) if row.private_suggested else ())


def share_decision(row: LabelRow, tags: HumanTags | None) -> ShareRow:
    """First rule wins; the rule name is the row's `reason`.

    `owner` comes before the human tags because the mailbox owner is not a contact —
    no tag makes them one. Everything else defaults to yes, matching the whole-CSV
    LinkedIn upload this replaces.
    """
    held = tags.tags if tags else frozenset()
    if row.is_owner:
        decision, reason = SHARE_NO, "owner"
    elif PRIVATE_TAG in held:
        decision, reason = SHARE_NO, "human_private"
    elif SHARE_TAG in held:
        decision, reason = SHARE_YES, "human_share"
    elif row.private_suggested:
        decision, reason = SHARE_NO, "private_suggested"
    elif row.probabilities.get("is_automated_sender", 0.0) >= ACTIVE_P:
        decision, reason = SHARE_NO, "automated_sender"
    elif row.probabilities.get("is_stranger", 0.0) >= ACTIVE_P:
        decision, reason = SHARE_NO, "stranger"
    else:
        decision, reason = SHARE_YES, "default"
    return ShareRow(
        person_id=row.person_id,
        public_identifier=row.public_identifier,
        share=decision,
        reason=reason,
        labels=active_labels(row),
        source="human" if reason.startswith("human_") else "machine",
    )
