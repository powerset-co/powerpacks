"""Typed parent dossier inputs shared by migration planning and rendering."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
    merged: dict[str, Any]
