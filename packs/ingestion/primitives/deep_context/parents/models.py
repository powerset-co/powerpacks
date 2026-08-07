"""Typed parent dossier inputs shared by migration planning and rendering."""
from __future__ import annotations

from dataclasses import dataclass
from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp
from packs.ingestion.primitives.deep_context.dossier.models import SynthesizedFacts


@dataclass(frozen=True)
class ParentFacts:
    """Inputs used to elect the surviving identity for an existing parent."""

    decided: bool
    decided_at: IsoTimestamp
    members: int


@dataclass(frozen=True)
class ChildEntry:
    slug: str
    name: str
    score: float
    reason: str
    channels: tuple[str, ...]
    person_id: str


@dataclass(frozen=True)
class ParentPlan:
    parent_id: str
    slug: str
    name: str
    emails: tuple[str, ...]
    phones: tuple[str, ...]
    confirmed: tuple[ChildEntry, ...]
    merged: SynthesizedFacts
