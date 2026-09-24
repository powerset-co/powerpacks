"""The share.csv contract used by the share stage and Powerset upload.

Flow: parse one CSV row into ShareRow; serialize a decision back to one row.

Changelog:
  2026-09-24: share became three-way (yes | no | confirm); reasons follow worth
    and the JEV rules became confirm flags.
  2026-09-24: created.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SHARE_COLUMNS = ("person_id", "public_identifier", "share", "reason", "labels", "source", "updated_at")

ShareValue = Literal["yes", "no", "confirm"]
SHARE_YES: ShareValue = "yes"
SHARE_NO: ShareValue = "no"
# A worth-yes person a confirm flag fired on: the human decides, nothing is uploaded.
SHARE_CONFIRM: ShareValue = "confirm"
SHARE_VALUES = frozenset({SHARE_YES, SHARE_NO, SHARE_CONFIRM})

# Reasons, first rule wins; the rule name is the reason.
OWNER = "owner"
HUMAN_PRIVATE = "human_private"
HUMAN_SHARE = "human_share"
WORTH_NO = "worth_no"
WORTH_MAYBE = "worth_maybe"
WORTH_YES = "worth_yes"

# Confirm flags (labels.confirm_flag owns the order). A flag is a reason too:
# it is what share.csv carries on a `confirm` row.
FAMILY = "family"
ROMANTIC_PARTNER = "romantic_partner"
MINOR = "minor"
SENSITIVE_CONTEXT = "sensitive_context"
SENSITIVE_PROVIDER = "sensitive_provider"
AUTOMATED_SENDER = "automated_sender"
STRANGER = "stranger"


@dataclass(frozen=True)
class ShareRow:
    """One share.csv decision."""

    person_id: str
    public_identifier: str | None
    share: ShareValue
    reason: str
    labels: tuple[str, ...]
    source: str
    updated_at: str

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> ShareRow:
        identifier = row["public_identifier"].strip().lower()
        share = row["share"].strip().lower()
        # The upload un-shares whatever is not `yes`; a cell outside the vocabulary
        # must stop the run, not quietly withdraw a person.
        if share not in SHARE_VALUES:
            raise ValueError(f"share.csv: unknown share value {share!r} for {row['person_id']}")
        return cls(
            person_id=row["person_id"].strip(),
            public_identifier=identifier or None,
            share=share,
            reason=row["reason"].strip(),
            labels=tuple(label for label in row["labels"].split("|") if label),
            source=row["source"].strip(),
            updated_at=row["updated_at"].strip(),
        )

    def to_csv_row(self) -> dict[str, str]:
        return {
            "person_id": self.person_id,
            "public_identifier": self.public_identifier or "",
            "share": self.share,
            "reason": self.reason,
            "labels": "|".join(self.labels),
            "source": self.source,
            "updated_at": self.updated_at,
        }
