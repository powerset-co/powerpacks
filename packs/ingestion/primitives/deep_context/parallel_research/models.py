"""Frozen result and progress rows for one Parallel research run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResearchRunCounts:
    run_ids: int
    results_fetched: int
    errors: int
    real_name_found: int
    linkedin_found: int


@dataclass(frozen=True)
class ResearchProgressCounts:
    total: int
    completed: int
    pending: int
    failed: int


@dataclass(frozen=True)
class ResearchProgress:
    status: str
    counts: ResearchProgressCounts


@dataclass(frozen=True)
class ResearchRunResult:
    """One provider run after its raw SDK payload has been parsed."""

    status: str
    error: str = ""
    queue_rows: int | None = None
    skipped_already_done: int | None = None
    completed_at: str = ""
    output_dir: str = ""
    counts: ResearchRunCounts | None = None
    group_status: dict[str, Any] | None = None
    errors: tuple[str, ...] = ()

    @classmethod
    def failed(cls, error: str) -> ResearchRunResult:
        return cls("failed", error=error)
