"""Manifest projected onto the local review HTTP contract."""

from dataclasses import asdict, dataclass
from typing import Any

from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp


@dataclass(frozen=True)
class ReviewManifest:
    stage: str
    status: str
    counts: tuple[tuple[str, int], ...]
    completed_stages: tuple[str, ...]
    people_revision: IsoTimestamp
    synthetic_people_csv: str
    privacy: tuple[tuple[str, bool], ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["counts"] = dict(self.counts)
        payload["completed_stages"] = list(self.completed_stages)
        payload["privacy"] = dict(self.privacy)
        return payload
