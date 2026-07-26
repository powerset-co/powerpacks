#!/usr/bin/env python3
"""Models for the enrichment stage: config, manifest, columns, rows, payloads.

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
  schema. `QUEUE_COLUMNS` is the shared BASE the other three extend; no artifact
  is written with it directly.
- `EnrichCacheRow` / `EnrichRecentFailureRow` / `EnrichProviderRow` — the
  `pipeline/contract.py:RowModel`s generated FROM those column constants, so the
  declared row shape cannot drift from the CSV each step actually writes.
- `PrepareQueueSummary` / `EnrichLinkedInSummary` / `EnrichMergeSummary` — the
  typed `StageManifest` payload each enrich step node returns from `execute()`.
  They are the step `summary` blocks the stage manifest already carried, now
  declared instead of assembled as raw dicts. Optional fields are `None` by
  default and `to_payload()` drops them, which is how the enrich step keeps its
  two historical summary shapes (the no-work early return omits the throughput /
  retry keys) byte-for-byte.
- `PipelineFailed` — a hard, non-recoverable step failure.

Changelog:
  2026-07-26 (enrich store is a Node): `EnrichManifest` is a pydantic
    `StageManifest` instead of a dataclass with a hand-written `to_dict()`, so it
    can be the declared payload of the `EnrichPeople` node. `to_payload()` replaces
    `to_dict()` (same keys, same None-dropping; `write_json` sorts keys, so the
    file is unchanged) and the `primitive: "enrich_people"` stamp is a field.
  2026-07-25 (declared contract): added the three `RowModel`s and the three step
    `StageManifest` payloads for the enrich stage's `Node` conversion. The row
    models are generated from the existing column constants, so the constants
    stay the single home for CSV order.
  2026-07-23 (audit decomposition): split out of enrich_people.py verbatim.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.enrich.rapidapi_client import (  # noqa: E402
    DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS,
    DEFAULT_RAPIDAPI_MAX_RPM,
    DEFAULT_RAPIDAPI_MAX_WORKERS,
)
from packs.ingestion.primitives.pipeline.contract import (  # noqa: E402
    STATUS_COMPLETED,
    StageManifest,
    row_model_for,
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

# Row models generated FROM the column constants above — one home for the order.
EnrichCacheRow = row_model_for("EnrichCacheRow", CACHE_COLUMNS)
EnrichRecentFailureRow = row_model_for("EnrichRecentFailureRow", RECENT_FAILURE_COLUMNS)
EnrichProviderRow = row_model_for("EnrichProviderRow", PROVIDER_COLUMNS)


class PrepareQueueSummary(StageManifest):
    """`enrich_prepare_queue`'s payload: how the input rows routed and how the
    LinkedIn-provider rows split across the local profile cache.

    `paid_call_rows` is the number the spend gate reads: it is the cache-MISS
    count, i.e. the RapidAPI fetches this run would bill for."""

    status: str = STATUS_COMPLETED
    input_rows: int
    queue_rows: int
    cache_hit_rows: int
    paid_call_rows: int
    recent_failure_rows: int
    unresolved_rows: int
    skipped_rows: int
    route_counts: dict[str, int]


class EnrichLinkedInSummary(StageManifest):
    """`enrich_linkedin_profiles`'s payload. The throughput/retry fields default
    to None and are dropped by `to_payload()`, which reproduces the shorter
    summary the no-work early return has always emitted."""

    status: str = STATUS_COMPLETED
    processed: int
    cached: int
    fetched: int
    output_file: str
    providers: dict[str, bool]
    max_workers: int | None = None
    max_rpm: float | None = None
    retried: int | None = None
    retry_successes: int | None = None
    retry_failures: int | None = None


class EnrichMergeSummary(StageManifest):
    """`enrich_merge_people`'s payload: every input row survives, counted by the
    terminal `enrichment_status` it was stamped with."""

    status: str = STATUS_COMPLETED
    rows: int
    enriched_rows: int
    failed_rows: int
    skipped_rows: int
    output_file: str


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


class EnrichManifest(StageManifest):
    """Typed constructor for the enrichment stage `manifest.json` — the entire
    durable state contract (status + per-step timing + counts + artifact paths).
    No ledger, no run id: the artifact dir is fixed so reruns overwrite here.

    A `StageManifest`, so it is the payload the stage's `EnrichPeople` Node
    returns; `to_payload()` drops `needs_approval`/`error` when None exactly as the
    hand-written `to_dict()` did, and `write_json` sorts keys, so the manifest on
    disk is unchanged."""

    primitive: str = "enrich_people"
    status: str = ""
    artifact_dir: str = ""
    input: dict[str, Any] = {}
    counts: dict[str, Any] = {}
    artifacts: dict[str, Any] = {}
    steps: dict[str, Any] = {}
    needs_approval: dict[str, Any] | None = None
    error: str | None = None
    started_at: str = ""
    updated_at: str = ""
