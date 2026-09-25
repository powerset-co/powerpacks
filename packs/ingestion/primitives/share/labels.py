"""The share stage's whole policy: deterministic labels, Jev label reduction,
the confirm-flag table and the share decision.

Flow: `deterministic_labels(person)` -> cadence/direction/worth from metadata;
`labels_from_answers(answers)` -> one value per Jev question; `confirm_flag(jev)`
-> the first flag rule that fires; `share_decision(row, tags)` -> the
yes/no/confirm and reason the `share` table carries.

Share follows worth. The Jev labels decide nothing: they only flag a worth-yes
person for a human to confirm.

Every threshold is a module constant here; nothing downstream re-derives one.

Changelog:
  2026-09-24: share follows worth; the private rules became confirm flags.
  2026-09-24: used the shared share reasons and the decision row.
  2026-09-24: created.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable

from packs.ingestion.primitives.deep_context.db.models import MachineWorth, ShareDecisionRow
from packs.ingestion.primitives.share.models import (
    GROUP_CHANNELS,
    DeterministicLabels,
    HumanTags,
    JevLabels,
    LabelRow,
    PersonEvidence,
)
from packs.ingestion.primitives.share.questions import CHOICE_LABELS, NOUL_LABELS, SCORE_LABELS
from packs.ingestion.schemas.share_schema import (
    AUTOMATED_SENDER,
    FAMILY,
    HUMAN_PRIVATE,
    HUMAN_SHARE,
    MINOR,
    OWNER,
    ROMANTIC_PARTNER,
    SENSITIVE_CONTEXT,
    SENSITIVE_PROVIDER,
    SHARE_CONFIRM,
    SHARE_NO,
    SHARE_YES,
    STRANGER,
    WORTH_MAYBE,
    WORTH_NO,
    WORTH_YES,
)

# Cadence bands, first rule wins: staleness before volume.
DORMANT_DAYS = 730
STALE_DAYS = 365
FREQUENT_MESSAGES = 200
REGULAR_MESSAGES = 30

# Direction bands on the owner's share of the messages.
THEY_INITIATE_BELOW = 0.35
I_INITIATE_ABOVE = 0.65

# A noul label is "active" (listed on the share row) at this probability.
ACTIVE_P = 0.6
# A sensitive noul label raises a confirm flag at this probability — a lower bar,
# because a needless confirmation costs one click and a false share cannot be
# taken back.
CONFIRM_P = 0.5

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
    """Reduce one Jev response: choice -> argmax option, score -> expected level, noul -> p."""
    choices: dict[str, str] = {}
    choice_p: dict[str, float] = {}
    for name in CHOICE_LABELS:
        probabilities = answers[name]["probabilities"]
        best = max(probabilities, key=lambda option: probabilities[option])
        choices[name] = best
        choice_p[name] = float(probabilities[best])
    scores: dict[str, float] = {}
    for name in SCORE_LABELS:
        probabilities = answers[name]["probabilities"]
        scores[name] = round(sum(int(level) * p for level, p in probabilities.items()), 2)
    return JevLabels(
        choices=choices,
        choice_p=choice_p,
        scores=scores,
        probabilities={name: float(answers[name]["noul"]) for name in NOUL_LABELS},
    )


def labels_from_saved(labels: dict) -> JevLabels:
    """Parse the labels persisted with a person's synthesized facts."""
    return JevLabels(
        choices={name: str(labels[name]) for name in CHOICE_LABELS},
        choice_p={name: float(labels[f"{name}_p"]) for name in CHOICE_LABELS},
        scores={name: float(labels[name]) for name in SCORE_LABELS},
        probabilities={name: float(labels[name]) for name in NOUL_LABELS},
    )


# First rule wins; the rule NAME is the label row's flag.
_CONFIRM_RULES: tuple[tuple[str, Callable[[JevLabels], bool]], ...] = (
    (FAMILY, lambda j: j.choices["relationship_kind"] == "family" or j.probabilities["is_family"] >= CONFIRM_P),
    (ROMANTIC_PARTNER, lambda j: j.choices["relationship_kind"] == "romantic_partner"),
    (MINOR, lambda j: j.probabilities["is_minor"] >= CONFIRM_P),
    (SENSITIVE_CONTEXT, lambda j: j.probabilities["sensitive_context"] >= CONFIRM_P),
    (SENSITIVE_PROVIDER,
     lambda j: j.probabilities["is_healthcare_legal_or_financial_provider"] >= CONFIRM_P),
    (AUTOMATED_SENDER, lambda j: j.probabilities["is_automated_sender"] >= ACTIVE_P),
    (STRANGER, lambda j: j.probabilities["is_stranger"] >= ACTIVE_P),
)
# `confidential_dealings` stays a label, not a flag: on a real network it fired
# on 87 people, 50 of them recruiters — hiring talk is ordinary here.


def confirm_flag(jev: JevLabels | None) -> str | None:
    """The first flag rule that fires, or None. A flag asks a human; it decides nothing.

    A person Jev never saw raises no flag: no answers, no question to ask."""
    if jev is None:
        return None
    for name, fired in _CONFIRM_RULES:
        if fired(jev):
            return name
    return None


def active_labels(row: LabelRow) -> tuple[str, ...]:
    """The labels the share row carries: every noul at or above ACTIVE_P, then the flag."""
    active = tuple(name for name in NOUL_LABELS if row.probabilities.get(name, 0.0) >= ACTIVE_P)
    return active + ((row.flag,) if row.flag else ())


def share_decision(row: LabelRow, tags: HumanTags | None, *, updated_at: str) -> ShareDecisionRow:
    """First rule wins; the rule name is the row's `reason`.

    Share follows worth — the same effective worth the human reviewed, human over
    machine. `owner` comes before the human tags because the mailbox owner is not
    a contact; no tag makes them one. A flag on a worth-yes person shares nothing
    until a human confirms it.
    """
    held = tags.tags if tags else frozenset()
    if row.is_owner:
        share, reason = SHARE_NO, OWNER
    elif PRIVATE_TAG in held:
        share, reason = SHARE_NO, HUMAN_PRIVATE
    elif SHARE_TAG in held:
        share, reason = SHARE_YES, HUMAN_SHARE
    elif row.worth == MachineWorth.NO.value:
        share, reason = SHARE_NO, WORTH_NO
    elif row.worth != MachineWorth.YES.value:
        share, reason = SHARE_NO, WORTH_MAYBE
    elif row.flag:
        share, reason = SHARE_CONFIRM, row.flag
    else:
        share, reason = SHARE_YES, WORTH_YES
    return ShareDecisionRow(
        person_id=row.person_id,
        public_identifier=row.public_identifier,
        share=share,
        reason=reason,
        labels="|".join(active_labels(row)),
        source="human" if reason.startswith("human_") else "machine",
        updated_at=updated_at,
    )
