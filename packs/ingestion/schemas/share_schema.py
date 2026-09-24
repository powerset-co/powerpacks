"""The share.csv contract used by the share stage and Powerset upload.

Flow: parse one CSV row into ShareRow; serialize a decision back to one row.

Changelog:
  2026-09-24: created.
"""

from __future__ import annotations

from dataclasses import dataclass

SHARE_COLUMNS = ("person_id", "public_identifier", "share", "reason", "labels", "source", "updated_at")

OWNER = "owner"
HUMAN_PRIVATE = "human_private"
HUMAN_SHARE = "human_share"
PRIVATE_SUGGESTED = "private_suggested"
AUTOMATED_SENDER = "automated_sender"
STRANGER = "stranger"
DEFAULT = "default"
PRIVATE_REASONS = frozenset({HUMAN_PRIVATE, PRIVATE_SUGGESTED})


@dataclass(frozen=True)
class ShareRow:
    """One share.csv decision."""

    person_id: str
    public_identifier: str | None
    share: bool
    reason: str
    labels: tuple[str, ...]
    source: str
    updated_at: str

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> ShareRow:
        identifier = row["public_identifier"].strip().lower()
        return cls(
            person_id=row["person_id"].strip(),
            public_identifier=identifier or None,
            share=row["share"].strip().lower() == "yes",
            reason=row["reason"].strip(),
            labels=tuple(label for label in row["labels"].split("|") if label),
            source=row["source"].strip(),
            updated_at=row["updated_at"].strip(),
        )

    def to_csv_row(self) -> dict[str, str]:
        return {
            "person_id": self.person_id,
            "public_identifier": self.public_identifier or "",
            "share": "yes" if self.share else "no",
            "reason": self.reason,
            "labels": "|".join(self.labels),
            "source": self.source,
            "updated_at": self.updated_at,
        }
