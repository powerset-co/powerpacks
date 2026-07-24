#!/usr/bin/env python3
"""Models for the enrichment stage: config, manifest, columns, failure type.

The typed contracts the enrich_people orchestrator (and its in-process callers
like linkedin/network_import) build and exchange — no behavior beyond
construction and serialization.

- `EnrichConfig` / `build_config` — frozen per-run config; `build_config`
  resolves the `None` inherit-sentinel throughput knobs to the
  `rapidapi_client.DEFAULT_RAPIDAPI_*` defaults so every field is concrete.
- `EnrichManifest` — typed constructor for the stage `manifest.json`, the whole
  durable state contract (status + per-step timing + counts + artifact paths).
  No ledger, no run id: the artifact dir is fixed so reruns overwrite in place.
- `QUEUE_COLUMNS` / `CACHE_COLUMNS` / `RECENT_FAILURE_COLUMNS` /
  `PROVIDER_COLUMNS` — the stage CSV schemas, layered on the shared people
  schema.
- `PipelineFailed` — a hard, non-recoverable step failure.

Changelog:
  2026-07-23 (audit decomposition): split out of enrich_people.py verbatim.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import now_iso  # noqa: E402
from packs.ingestion.primitives.enrich.rapidapi_client import (  # noqa: E402
    DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS,
    DEFAULT_RAPIDAPI_MAX_RPM,
    DEFAULT_RAPIDAPI_MAX_WORKERS,
)
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS  # noqa: E402

QUEUE_COLUMNS = PEOPLE_SCHEMA_COLUMNS + ["enrichment_route", "enrichment_reason"]
CACHE_COLUMNS = QUEUE_COLUMNS + ["cache_status", "cache_path", "cache_reason"]
RECENT_FAILURE_COLUMNS = CACHE_COLUMNS + ["last_checked_at", "retry_after", "rapidapi_status_code", "rapidapi_error"]
PROVIDER_COLUMNS = QUEUE_COLUMNS + [
    "rapidapi_status_code",
    "rapidapi_error",
    "rapidapi_attempts",
    "rapidapi_retry_outcome",
    "rapidapi_response_enriched",
    "rapidapi_from_cache",
    "provider_enriched_at",
]


class PipelineFailed(Exception):
    """A hard, non-recoverable step failure (bad input, missing key for paid work)."""


@dataclass(frozen=True)
class EnrichConfig:
    """Frozen, keyword-only config for one enrichment run. `build_config`
    resolves the inherit-sentinel (`None`) throughput knobs to their defaults so
    every field here is concrete."""

    input_csv: Path
    artifact_dir: Path
    profile_cache_dir: Path
    limit: int | None = None
    force: bool = False
    refresh_cache: bool = False
    company_corpus_jsonl: tuple[str, ...] = ()
    sleep_seconds: float = 0.0
    max_workers: int = DEFAULT_RAPIDAPI_MAX_WORKERS
    max_rpm: float = DEFAULT_RAPIDAPI_MAX_RPM
    failure_retry_hours: float = DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS
    approve_spend: bool = False

    def manifest_input(self) -> dict[str, Any]:
        """The `input` block recorded in the manifest (what this run was asked to do)."""
        return {
            "input_csv": str(self.input_csv),
            "limit": self.limit,
            "force": self.force,
            "profile_cache_dir": str(self.profile_cache_dir),
            "refresh_cache": self.refresh_cache,
            "company_corpus_jsonl": [str(p) for p in self.company_corpus_jsonl],
            "sleep_seconds": self.sleep_seconds,
            "max_workers": self.max_workers,
            "max_rpm": self.max_rpm,
            "failure_retry_hours": self.failure_retry_hours,
            "approve_spend": self.approve_spend,
        }


def build_config(
    *,
    input_csv: str | Path,
    artifact_dir: str | Path,
    profile_cache_dir: str | Path,
    limit: int | None = None,
    force: bool = False,
    refresh_cache: bool = False,
    company_corpus_jsonl: list[str] | tuple[str, ...] | None = None,
    sleep_seconds: float | None = None,
    max_workers: int | None = None,
    max_rpm: float | None = None,
    failure_retry_hours: float | None = None,
    approve_spend: bool = False,
) -> EnrichConfig:
    """Build a frozen EnrichConfig, resolving `None` throughput knobs (the
    inherit sentinel that in-process callers like linkedin/network_import pass)
    to their module defaults."""
    return EnrichConfig(
        input_csv=Path(input_csv),
        artifact_dir=Path(artifact_dir),
        profile_cache_dir=Path(profile_cache_dir),
        limit=limit,
        force=force,
        refresh_cache=refresh_cache,
        company_corpus_jsonl=tuple(str(p) for p in (company_corpus_jsonl or [])),
        sleep_seconds=float(sleep_seconds) if sleep_seconds else 0.0,
        max_workers=int(max_workers) if max_workers else DEFAULT_RAPIDAPI_MAX_WORKERS,
        max_rpm=float(max_rpm) if max_rpm is not None else DEFAULT_RAPIDAPI_MAX_RPM,
        failure_retry_hours=float(failure_retry_hours) if failure_retry_hours is not None else DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS,
        approve_spend=approve_spend,
    )


@dataclass
class EnrichManifest:
    """Typed constructor for the enrichment stage `manifest.json` — the entire
    durable state contract (status + per-step timing + counts + artifact paths).
    No ledger, no run id: the artifact dir is fixed so reruns overwrite here."""

    status: str
    artifact_dir: str
    input: dict[str, Any]
    counts: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    steps: dict[str, Any] = field(default_factory=dict)
    needs_approval: dict[str, Any] | None = None
    error: str | None = None
    started_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "primitive": "enrich_people",
            "status": self.status,
            "artifact_dir": self.artifact_dir,
            "input": self.input,
            "counts": self.counts,
            "artifacts": self.artifacts,
            "steps": self.steps,
            "started_at": self.started_at,
            "updated_at": self.updated_at or now_iso(),
        }
        if self.needs_approval is not None:
            payload["needs_approval"] = self.needs_approval
        if self.error is not None:
            payload["error"] = self.error
        return payload
