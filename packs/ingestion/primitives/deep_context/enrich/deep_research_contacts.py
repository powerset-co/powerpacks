"""In-process API for SQLite-selected Parallel research."""

from __future__ import annotations

from packs.ingestion.primitives.deep_context.enrich.parallel_research import driver
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
    ResearchRunParams,
)

__all__ = ["ResearchRunParams", "run_research"]


run_research = driver.run_research
