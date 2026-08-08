"""In-process API for SQLite-selected Parallel research.

The one import surface for ``run_research``/``ResearchRunParams``: both paid
callers (``research_reconcile.coordinator``'s queue-driven reconcile, and
``identity_reconcile.guided``'s guided retargets) go through here rather than
reaching into ``parallel_research.driver``/``.models`` directly.
"""

from __future__ import annotations

from packs.ingestion.primitives.deep_context.enrich.parallel_research import driver
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
    ResearchRunParams,
)

__all__ = ["ResearchRunParams", "run_research"]


run_research = driver.run_research
