"""The share stage's whole policy: deterministic labels, the labels worth saved, the
flags that keep a family home, and the share decision.

Flow: `deterministic_labels(person)` -> cadence/direction/worth from metadata;
`labels_from_saved(labels)` -> one value per Luna question and the share call; `flag(labels)`
-> the first rule that keeps a worth-yes family home; `share_decision(row, tags)` -> the
yes/no and reason the `share` table carries.

In order: you (the owner, then your tags), worth, the flags, then Luna's share call, which read
every label and its confidence (worth/luna.py request 2). Its call stands at any confidence.

Every threshold is a module constant here; nothing downstream re-derives one.

Changelog:
  2026-10-14: no confirm: Luna's call stands at any confidence; keep_private keeps them home and
    anything else shares (a wrong share is undone with a Private tag).
  2026-10-13: Luna's share call decides a worth-yes family that no flag keeps home; minor keeps a
    family home; the clinician, automated-sender and stranger flags are gone into the call.
  2026-10-07: v2. Worth comes from the v2 store's current_worth; the row types are the share
    package's own; a flag or identifier that is absent is '' not None.
  2026-09-26: `label_row_from_export` rebuilds the decision inputs from
    `person_labels`, so the share UI re-decides a tagged person without the
    roster or the bundles.
  2026-09-24: share follows worth; the private rules became confirm flags.
  2026-09-24: used the shared share reasons and the decision row.
  2026-09-24: created.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from packs.ingestion.primitives.common.jsonio import parse_json_object
from packs.ingestion.primitives.deep_context_v2.db.schema import DecidedBy, Worth
from packs.ingestion.primitives.share.models import (
    GROUP_CHANNELS,
    DeterministicLabels,
    HumanTags,
    LabelRow,
    Labels,
    PersonEvidence,
    PersonLabelRow,
    ShareDecisionRow,
)
from packs.ingestion.primitives.share.questions import (
    CALL_KEEP_PRIVATE,
    CHOICE_LABELS,
    NOUL_LABELS,
    SCORE_LABELS,
)
from packs.ingestion.schemas.share_schema import (
    FAMILY,
    HUMAN_PRIVATE,
    HUMAN_SHARE,
    MINOR,
    NOT_PROFESSIONAL,
    OWNER,
    ROMANTIC_PARTNER,
    SHARE_NO,
    SHARE_YES,
    WORTH_MAYBE,
    WORTH_NO,
    WOULD_VOUCH,
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
# is_family or is_minor keeps a family home at this probability — a lower bar, because a
# needless hold costs one click and a false share cannot be taken back.
FLAG_P = 0.5

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
        linkedin_only=False,
        network_worth=person.network_worth,
        is_owner=person.is_owner,
        shared_employer=EMPLOYER_OVERLAP in person.shared_overlaps,
        shared_school=SCHOOL_OVERLAP in person.shared_overlaps,
    )


def labels_from_saved(labels: dict[str, Any]) -> Labels:
    """Parse the labels worth saved: one value per question, and the share call."""
    return Labels(
        choices={name: str(labels[name]) for name in CHOICE_LABELS},
        choice_p={name: float(labels[f"{name}_p"]) for name in CHOICE_LABELS},
        scores={name: float(labels[name]) for name in SCORE_LABELS},
        # A yes/no question Luna declined has no label.
        probabilities={name: float(labels[name]) for name in NOUL_LABELS if name in labels},
        share_call=str(labels["share_call"]),
        share_p=float(labels["share_call_p"]),
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


def active_labels(row: LabelRow) -> tuple[str, ...]:
    """The labels the share row carries: every noul at or above ACTIVE_P, then the flag."""
    active = tuple(name for name in NOUL_LABELS if row.probabilities.get(name, 0.0) >= ACTIVE_P)
    return active + ((row.flag,) if row.flag else ())


def label_row_from_export(row: PersonLabelRow) -> LabelRow:
    """The decision inputs, read back from the label export the node wrote.

    `labels_json` carries `is_owner`, every noul probability and the share call (none of them
    for a family worth never judged; an export written before Luna has no call); `worth` and
    `flag` are columns. The same `share_decision` then yields the same row the node wrote.
    """
    cells = parse_json_object(row.labels_json)
    return LabelRow(
        person_id=row.person_id,
        public_identifier=row.public_identifier,
        is_owner=bool(cells.get("is_owner")),
        worth=row.worth,
        flag=row.flag,
        probabilities={name: float(cells[name]) for name in NOUL_LABELS if name in cells},
        share_call=str(cells.get("share_call", "")),
        share_p=float(cells.get("share_call_p", 0.0)),
    )


def share_decision(row: LabelRow, tags: HumanTags | None, *, updated_at: str) -> ShareDecisionRow:
    """First rule wins; the rule name is the row's `reason`.

    Worth first — the same effective worth the human reviewed, human over machine. `owner`
    comes before the human tags because the mailbox owner is not a contact; no tag makes them
    one. A flag keeps a worth-yes family home. Everyone else gets Luna's share call, whatever its
    confidence: keep_private keeps them home, anything else shares them.
    """
    held = tags.tags if tags else frozenset()
    if row.is_owner:
        share, reason = SHARE_NO, OWNER
    elif PRIVATE_TAG in held:
        share, reason = SHARE_NO, HUMAN_PRIVATE
    elif SHARE_TAG in held:
        share, reason = SHARE_YES, HUMAN_SHARE
    elif row.worth == Worth.NO.value:
        share, reason = SHARE_NO, WORTH_NO
    elif row.worth != Worth.YES.value:
        share, reason = SHARE_NO, WORTH_MAYBE
    elif row.flag:
        # Family, partners and minors stay home unless the human tags them Share (decided 2026-10-08).
        share, reason = SHARE_NO, row.flag
    elif row.share_call == CALL_KEEP_PRIVATE:
        share, reason = SHARE_NO, NOT_PROFESSIONAL
    else:
        share, reason = SHARE_YES, WOULD_VOUCH
    return ShareDecisionRow(
        person_id=row.person_id,
        public_identifier=row.public_identifier,
        share=share,
        reason=reason,
        labels="|".join(active_labels(row)),
        source=DecidedBy.HUMAN.value if reason.startswith("human_") else DecidedBy.MACHINE.value,
        updated_at=updated_at,
    )
