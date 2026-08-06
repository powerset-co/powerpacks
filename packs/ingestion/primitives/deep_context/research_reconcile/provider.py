"""Invoke the official Parallel-backed research primitive in process."""

from __future__ import annotations

import sys
from typing import Any

from packs.ingestion.primitives.deep_context.deep_research_contacts import (
    ResearchRunParams,
    run_research,
)


RESEARCH_OK_STATUSES = frozenset({"no_work", "completed"})


def run_provider(
    params: ResearchRunParams, *, pending_count: int, processor: str
) -> dict[str, Any]:
    print(
        f"[deep-research] researching {pending_count} net-new people via "
        f"Parallel.ai ({processor}); this can take several minutes — live "
        "progress below:",
        file=sys.stderr,
        flush=True,
    )
    try:
        result = run_research(params)
    except SystemExit as exc:
        result = {"status": "failed", "error": f"SystemExit: {exc}"}
    except Exception as exc:
        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    print(
        f"[deep-research] research finished "
        f"({str(result.get('status') or 'failed')}).",
        file=sys.stderr,
        flush=True,
    )
    return result
