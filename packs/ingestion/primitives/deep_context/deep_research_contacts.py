"""In-process API for SQLite-selected Parallel research."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.parallel_research import (
    config,
    driver,
)


@dataclass(frozen=True)
class ResearchRunParams:
    """One explicit configuration door for an in-process research pass."""

    output_dir: Path
    rows: tuple[dict[str, str], ...] = ()
    processor: str = config.DEFAULT_PROCESSOR
    selection_fingerprint: str = ""
    manifest: str = ""
    api_key: str | None = None
    base_url: str = config.DEFAULT_BASE_URL
    beta_header: str = config.DEFAULT_BETA_HEADER
    batch_size: int = config.DEFAULT_BATCH_SIZE
    limit: int | None = None
    poll_interval: int = config.DEFAULT_POLL_INTERVAL
    max_wait: int = config.DEFAULT_MAX_WAIT
    api_timeout: int = 60
    on_progress: Callable[[dict[str, Any]], None] | None = None
    db: Db | None = None
    owns_receipt: bool = True


run_research = driver.run_research
