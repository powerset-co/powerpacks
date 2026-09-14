"""Typed stable-parent assignment inputs."""

from __future__ import annotations

from dataclasses import dataclass

from packs.ingestion.primitives.deep_context.db.models import IsoTimestamp


@dataclass(frozen=True)
class ParentFacts:
    """Inputs used to elect the surviving identity for an existing parent."""

    decided: bool
    decided_at: IsoTimestamp
    members: int
