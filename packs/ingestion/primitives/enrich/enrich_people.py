#!/usr/bin/env python3
"""Unified local people enrichment flow (RapidAPI-only).

Self-contained Powerpacks RapidAPI enrichment implementation. No imports from
the legacy app or hosted search API.

This file holds the three step NODES, the stage orchestrator, and the CLI. The
rest of the stage is decomposed into sibling modules, one concern each — import
from the defining module, not through here:

- `models.py` — EnrichConfig/build_config, EnrichManifest, the stage CSV
  columns + row models, the step payloads, PipelineFailed.
- `rapidapi_client.py` — the RapidApiClient class: key/env handling, http_json,
  retry/backoff, the one `get_profile` door, DEFAULT_RAPIDAPI_* knobs.
- `profile_cache.py` — profile-cache slugs/paths/reads, failure TTL,
  cache-status classification, and the cache seeding format documentation.
- `profile_transforms.py` — pure row transforms: route_row, normalize_rapidapi,
  merge_provider_profile, confirmed_people_row.

Consumers:
- Primary whole-pipeline owner: ``imports/linkedin/network_import.py``.
- Shared-library consumers (deep-context profile hydration/reconciliation,
  search's ``fetch_person_profile``) import the sibling modules directly.

Input: a shared people schema CSV, usually the `imports/merge_people.py` output.
Output: enriched people schema CSV plus the two provider hand-off CSVs.

LinkedIn hydration runs through the Powerset gateway when POWERSET_API_KEY is
present. Missing keys fail clearly
instead of opening an approval step.

Contract: ONE idempotent `run` (plus `status`, which reads the stage manifest,
and `check-keys`). A run writes its output CSVs and one `manifest.json` into a
fixed artifact directory (default `.powerpacks/network-import/enrichment/`,
override with `--artifact-dir`/`--output-dir`) and overwrites in place — there
is no ledger, no `continue`, no per-step state store. Reruns are idempotent
because the output path is stable. The manifest holds status, per-step timing,
counts, and the artifact paths.

Flow (EnrichPeople.execute, under the Node run template):
  enrich_prepare_queue -> paid cache misses & no --approve-spend? -> stop at a
  needs_approval manifest BEFORE any client or fetch -> enrich_linkedin_profiles
  -> enrich_merge_people -> one manifest.json

The store AND its three steps are `pipeline/contract.py:Node`s that DECLARE the
files they read and write, so the hand-offs between them are checkable without a
run. The three steps declare `manifest = ""`: the stage has ONE manifest.json,
written by the store's run template, embedding each step's typed payload as its
`summary`.

1. enrich_prepare_queue: routes rows with LinkedIn URLs/public identifiers and
   profile gaps, then splits them by the local profile cache into
   `rapidapi_cache_hits.csv`, `rapidapi_cache_misses.csv`, and
   `rapidapi_recent_failures.csv`. Rows without LinkedIn, and complete-looking
   rows, are COUNTED (`unresolved_rows` / `skipped_rows`) but not written to a
   file of their own — they come out of step 3 in `people.csv` carrying
   `enrichment_status=skipped`.
2. enrich_linkedin_profiles: hydrates the hits and fetches the misses into
   `provider_enriched.csv`.
3. enrich_merge_people: merges profile data back into the input rows and writes
   canonical `people.csv` — every input row, each carrying the shared schema's
   `enrichment_status` (`enriched` / `failed` / `skipped`) and, for a failure,
   `enrichment_error`. Enrichment never deletes a row.

Spend gate: cache hits never need approval. If enrich_prepare_queue finds
RapidAPI cache misses (paid fetches) and `--approve-spend` was not passed, `run`
writes a `needs_approval` manifest with the miss count + credit estimate and
exits nonzero-but-clean (code 20) BEFORE any client is constructed and before
any fetch. With `--approve-spend` it proceeds (and still fails clearly if no
RAPIDAPI_* key is set). `estimated_credits` is a FLOOR (one credit per miss);
`estimated_credits_max` is the worst case where every miss exhausts its retry
attempts, each of which RapidAPI bills.

Usage:
    enrich_people.py run --input .powerpacks/network-import/merged/people.csv [--approve-spend]
    enrich_people.py status | check-keys

Options: `--profile-cache-dir` (default
`.powerpacks/network-import/profile_cache_v2`), `--company-corpus-jsonl`
(repeatable; company metadata by RapidAPI company ID or LinkedIn company slug),
`--max-workers`/`--max-rpm` (defaults 64 workers / 300 RPM, env-overridable),
`--failure-retry-hours` (skip recently failed lookups; default 24h),
`--approve-spend` (authorize paid RapidAPI fetches for cache misses), `--force`
(re-enrich complete-looking rows), hidden `--limit` for tiny smoke tests only.

Cache seeding format is documented in `profile_cache.py`. Company identity
field behavior is documented in `profile_transforms.py`.

Changelog:
  2026-07-26 (the store is a Node too): `EnrichPeople` is a
    `pipeline/contract.py:Node`. What blocked it was `Node.run()` returning a dict
    while `imports/linkedin/network_import.py` consumes this stage's typed
    `EnrichManifest` by attribute; the template returns the typed payload now, so
    the store fits. `run()` -> `execute()`, `_write()` -> `_build()` (the template
    writes the manifest, which therefore gains this stage's declared IO stats), and
    `EnrichManifest` became a pydantic `StageManifest` (see models.py). Same
    statuses, same spend gate, same manifest keys.
  2026-07-25 (declared contract): the three steps became `pipeline/contract.py`
    Nodes — `EnrichQueuePrepare`, `EnrichLinkedInProfiles`, `EnrichedPeopleMerge`
    — each DECLARING its inputs/outputs as `Artifact`s and returning a typed
    payload from `execute()`; `run()` is the inherited template (validate
    declared inputs -> execute -> validate declared outputs -> payload). The
    steps read their hand-off CSVs from their own FIXED paths instead of from
    the orchestrator's `self.artifacts` dict, and EnrichLinkedInProfiles derives
    the paid-call count from the misses CSV it was given rather than being told.
    EnrichPeople stayed the store (it owns the artifact dir, the spend gate, and
    the one manifest.json) and was NOT a Node at that point: `Node.run()` returned
    a dict and `imports/linkedin/network_import.py` consumes the typed
    `EnrichManifest` by attribute, so converting the store was that caller's
    change to make. The store now stops at the first non-completed step payload
    (the store pattern), which turns a missing input CSV from a FileNotFoundError
    traceback into a typed failed manifest. CLI: `command_run`/`command_status`
    dispatchers and `set_defaults(func=...)` are gone, dispatched inline in
    main(); `command_check_keys` is KEPT because network_import calls it.
  2026-07-25 (dead outputs deleted): `linkedin_enrichment_queue.csv`,
    `needs_resolution_queue.csv`, `skipped_enrichment.csv`, and the
    `raw_provider_responses/` JSON dump are no longer written — a repo-wide grep
    for real readers found none (the queue CSV's only reader was an unreachable
    fallback here, since enrich_linkedin_profiles always writes
    provider_enriched.csv). Their counts stay in the manifest and every row they
    described still comes out in people.csv, stamped. The raw dump duplicated
    data already held three times over (the profile cache, provider_enriched's
    `rapidapi_response_enriched`, people.csv's `rapidapi_response`) and cost one
    file write per profile per run.
  2026-07-24: merge_people stopped filtering people.csv down to confirmed rows.
    A row the provider could not hydrate was previously written nowhere at all,
    making a rate-limited fetch indistinguishable from "never attempted" — the
    only trace was an aggregate count. Rows now survive with
    `enrichment_status`/`enrichment_error`, and the summary reports
    enriched/failed/skipped instead of `filtered_rows`. The spend gate also
    quotes a credit RANGE, since each cache miss can bill once per retry.
  2026-07-23 (audit oo-cli): the CLI command handlers (command_run/status/
    check_keys) moved onto EnrichPeople so the class is the single entry point.
    RapidAPI access is now through the RapidApiClient class (resolve_key/
    fetch_profile) instead of the module rapidapi_key/rapidapi_profile functions.
  2026-07-23 (audit decomposition): split the module into models.py /
    rapidapi_client.py / profile_cache.py / profile_transforms.py, keeping only
    the EnrichPeople orchestrator, its progress knobs, and the CLI here. The
    dead `split_name` was deleted; `cached_profile_from_row` lost its two
    unused parameters. Behavior, CSV bytes, and the CLI are unchanged.
  2026-07-23 (audit class-sharing): the spend-gate exit code + CLI-emit helpers
    moved to common/gates.py — EXIT_NEEDS_APPROVAL (NEEDS_APPROVAL_CODE is now an
    alias of it), exit_code_for_status, and manifest_emit_payload are imported
    from there. The needs_approval PAYLOAD stays a local literal: it is the
    credit-gate shape (reason/paid_call_count/cache_hit_count/estimated_credits/
    message), distinct from twitter's step-gate shape, so it does not use the
    shared step-gate builder.
  2026-07-23 (audit): replaced the per-step ledger runner with an EnrichPeople
    orchestrator that owns the fixed artifact dir, the three steps, and one
    manifest.json. Spend is now gated by an explicit `--approve-spend` flag (a
    needs_approval manifest + clean nonzero exit on cache misses) instead of the
    dead approval machinery; `continue`/`approve` are gone.
  2026-07-23 (audit): dropped the local byte-identical read_csv/write_csv for
    the shared CsvIO.read_dict_rows / CsvIO.write_dict_rows.
  2026-07-23 (audit): enrich_people.README.md sidecar folded into this
    docstring; fixed its stale worker default (10 -> 64).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.gates import EXIT_NEEDS_APPROVAL, exit_code_for_status, manifest_emit_payload  # noqa: E402
from packs.ingestion.primitives.common.jsonio import emit, now_iso, read_json, short_hash  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR  # noqa: E402
from packs.ingestion.primitives.common.proc import emit_progress as _emit_progress  # noqa: E402
from packs.ingestion.primitives.enrich.models import (  # noqa: E402
    CACHE_COLUMNS,
    EnrichCacheRow,
    EnrichConfig,
    EnrichLinkedInSummary,
    EnrichManifest,
    EnrichMergeSummary,
    EnrichProviderRow,
    EnrichRecentFailureRow,
    PROVIDER_COLUMNS,
    PipelineFailed,
    PrepareQueueSummary,
    RECENT_FAILURE_COLUMNS,
    build_config,
)
from packs.ingestion.primitives.enrich.profile_cache import (  # noqa: E402
    cached_profile_from_row,
    classify_rapidapi_cache_status,
    profile_cache_index,
    profile_cache_path,
    read_usable_cached_profile,
)
from packs.ingestion.primitives.enrich.profile_transforms import (  # noqa: E402
    merge_provider_profile,
    normalize_rapidapi,
    provider_failure_reason,
    route_row,
    stamp_enrichment_outcome,
)
from packs.ingestion.primitives.enrich.rapidapi_client import (  # noqa: E402
    DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS,
    DEFAULT_RAPIDAPI_MAX_RPM,
    DEFAULT_RAPIDAPI_MAX_WORKERS,
    DEFAULT_RAPIDAPI_RETRY_ATTEMPTS,
    RapidApiClient,
)
from packs.ingestion.primitives.pipeline.contract import (  # noqa: E402
    STATUS_COMPLETED,
    Artifact,
    Node,
    PeopleRow,
)
from packs.ingestion.schemas.company_identity import build_company_identity_lookup  # noqa: E402
from packs.ingestion.schemas.linkedin_profile_normalizer import normalize_linkedin_profile  # noqa: E402
from packs.ingestion.schemas.people_schema import (  # noqa: E402
    ENRICHMENT_STATUS_ENRICHED,
    ENRICHMENT_STATUS_FAILED,
    ENRICHMENT_STATUS_SKIPPED,
    PEOPLE_SCHEMA_COLUMNS,
    extract_public_identifier,
    normalize_linkedin_url,
    normalize_people_row,
)
from packs.shared.csv_io import CsvIO  # noqa: E402
from packs.shared.rate_limiter import StartRateLimiter  # noqa: E402

DEFAULT_PROGRESS_INTERVAL_SECONDS = float(os.environ.get("POWERPACKS_RAPIDAPI_PROGRESS_INTERVAL_SECONDS", "60"))
DEFAULT_PROGRESS_INTERVAL_ROWS = int(os.environ.get("POWERPACKS_RAPIDAPI_PROGRESS_INTERVAL_ROWS", "100"))
# `run` exit code when paid RapidAPI cache-miss fetches are gated behind
# --approve-spend. The value + the status->code mapping live in common/gates.py;
# kept here as a module alias for the name callers/tests already reach for.
NEEDS_APPROVAL_CODE = EXIT_NEEDS_APPROVAL

# The stage's artifact FILE NAMES live here once; both the declared default paths
# below and each node's instance paths are built from them, so a rename cannot
# leave a declaration pointing at a file nothing writes.
CACHE_HITS_FILE = "rapidapi_cache_hits.csv"
CACHE_MISSES_FILE = "rapidapi_cache_misses.csv"
RECENT_FAILURES_FILE = "rapidapi_recent_failures.csv"
PROVIDER_ENRICHED_FILE = "provider_enriched.csv"
PEOPLE_FILE = "people.csv"
MANIFEST_FILE = "manifest.json"

# The DECLARED paths: what the stage reads and writes when nobody overrides the
# artifact dir. The input is the fan-in merge's output — declaring that exact
# path is what puts the enrich steps downstream of `merge_people` in the graph.
# Every instance rebinds these through `bindings()` (linkedin/network_import runs
# enrichment against its own discover dir, and tests against a temp dir).
DEFAULT_ARTIFACT_DIR = DEFAULT_BASE_DIR / "enrichment"
DEFAULT_INPUT_PEOPLE_CSV = str(DEFAULT_BASE_DIR / "merged" / PEOPLE_FILE)
CACHE_HITS_CSV = str(DEFAULT_ARTIFACT_DIR / CACHE_HITS_FILE)
CACHE_MISSES_CSV = str(DEFAULT_ARTIFACT_DIR / CACHE_MISSES_FILE)
RECENT_FAILURES_CSV = str(DEFAULT_ARTIFACT_DIR / RECENT_FAILURES_FILE)
PROVIDER_ENRICHED_CSV = str(DEFAULT_ARTIFACT_DIR / PROVIDER_ENRICHED_FILE)
ENRICHED_PEOPLE_CSV = str(DEFAULT_ARTIFACT_DIR / PEOPLE_FILE)


def emit_progress(message: str) -> None:
    """Write one progress line to stderr, tagged for the enrich-people chain."""
    _emit_progress(message, "[enrich-people]")


class EnrichQueuePrepare(Node):
    """Step 1. Route the input rows, then split the LinkedIn-provider ones by
    local profile-cache state into hits / misses / recent failures.

    Owns its three fixed output paths and records what it contributed on
    `self.artifacts` / `self.counts` for the store's manifest. The miss count IS
    the spend estimate: it is the number of RapidAPI fetches step 2 would bill
    for, and the store reads it before constructing any client.

    Rows that route to `needs_resolution` (no LinkedIn identifier) or to a
    `skip_*` reason are counted, not written to a file of their own — every one
    of them still comes out of step 3 in people.csv, stamped `skipped`."""

    name = "enrich_prepare_queue"
    inputs = (Artifact(path=DEFAULT_INPUT_PEOPLE_CSV, row_model=PeopleRow),)
    outputs = (
        Artifact(path=CACHE_HITS_CSV, row_model=EnrichCacheRow, writes="full_rewrite"),
        Artifact(path=CACHE_MISSES_CSV, row_model=EnrichCacheRow, writes="full_rewrite"),
        Artifact(path=RECENT_FAILURES_CSV, row_model=EnrichRecentFailureRow, writes="full_rewrite"),
    )
    payload = PrepareQueueSummary
    manifest = ""  # reports into EnrichPeople's one stage manifest.json

    def __init__(self, cfg: EnrichConfig) -> None:
        self.cfg = cfg
        self.artifact_dir = cfg.artifact_dir
        self.cache_hits_csv = self.artifact_dir / CACHE_HITS_FILE
        self.cache_misses_csv = self.artifact_dir / CACHE_MISSES_FILE
        self.recent_failures_csv = self.artifact_dir / RECENT_FAILURES_FILE
        self.artifacts: dict[str, Any] = {}
        self.counts: dict[str, Any] = {}

    def bindings(self) -> dict[str, str]:
        """Declared path -> this run's path. Keys come from the DECLARATION, so a
        run against another artifact dir still validates the same contract."""
        return {
            DEFAULT_INPUT_PEOPLE_CSV: str(self.cfg.input_csv),
            CACHE_HITS_CSV: str(self.cache_hits_csv),
            CACHE_MISSES_CSV: str(self.cache_misses_csv),
            RECENT_FAILURES_CSV: str(self.recent_failures_csv),
        }

    def execute(self) -> PrepareQueueSummary:
        cfg = self.cfg
        rows = [normalize_people_row(row) for row in CsvIO.read_dict_rows(cfg.input_csv)]
        if cfg.limit:
            rows = rows[: int(cfg.limit)]
        cache_hits: list[dict[str, Any]] = []
        cache_misses: list[dict[str, Any]] = []
        recent_failures: list[dict[str, Any]] = []
        queue_count = 0
        skipped_count = 0
        unresolved_count = 0
        route_counts: dict[str, int] = {}
        profile_cache_dir = cfg.profile_cache_dir
        cache_index = profile_cache_index(profile_cache_dir)
        failure_retry_hours = cfg.failure_retry_hours
        routed: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            route, reason = route_row(row, force=cfg.force)
            row["enrichment_route"] = route
            row["enrichment_reason"] = reason
            route_counts[route] = route_counts.get(route, 0) + 1
            routed.append((route, row))
        provider_rows = [row for route, row in routed if route == "linkedin_provider"]
        # Classification reads cached profiles from disk, which may be a network
        # filesystem (e.g. a Modal volume) where per-file round-trip latency
        # dominates; overlap the reads. Results stay in input order.
        classifications: list[tuple[str, str, Path | None, dict[str, Any] | None]] = []
        if provider_rows:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(provider_rows))) as pool:
                classifications = list(pool.map(
                    lambda row: classify_rapidapi_cache_status(row, profile_cache_dir, failure_retry_hours, cache_index),
                    provider_rows,
                ))
        classification_iter = iter(classifications)
        for route, row in routed:
            if route == "linkedin_provider":
                queue_count += 1
                status, cache_reason, cache_path, recent_failure = next(classification_iter)
                cache_row = dict(row)
                cache_row.update({"cache_status": status, "cache_path": str(cache_path or ""), "cache_reason": cache_reason})
                if status == "hit":
                    cache_hits.append(cache_row)
                elif status == "recent_failure":
                    normalized = recent_failure.get("normalized_profile") if isinstance(recent_failure, dict) else {}
                    cache_row.update({
                        "last_checked_at": recent_failure.get("last_checked_at") or recent_failure.get("fetched_at") or "",
                        "retry_after": recent_failure.get("retry_after") or "",
                        "rapidapi_status_code": recent_failure.get("status_code") or "",
                        "rapidapi_error": recent_failure.get("error") or (normalized.get("error") if isinstance(normalized, dict) else "") or "",
                    })
                    recent_failures.append(cache_row)
                else:
                    cache_misses.append(cache_row)
            elif route == "needs_resolution":
                unresolved_count += 1
            else:
                skipped_count += 1
        CsvIO.write_dict_rows(self.cache_hits_csv, CACHE_COLUMNS, cache_hits)
        CsvIO.write_dict_rows(self.cache_misses_csv, CACHE_COLUMNS, cache_misses)
        CsvIO.write_dict_rows(self.recent_failures_csv, RECENT_FAILURE_COLUMNS, recent_failures)
        self.artifacts.update({
            "rapidapi_cache_hits_csv": str(self.cache_hits_csv),
            "rapidapi_cache_misses_csv": str(self.cache_misses_csv),
            "rapidapi_recent_failures_csv": str(self.recent_failures_csv),
        })
        self.counts.update({
            "input_rows": len(rows),
            "queue_count": queue_count,
            "cache_hit_count": len(cache_hits),
            "paid_call_count": len(cache_misses),
            "recent_failure_count": len(recent_failures),
            "unresolved_rows": unresolved_count,
            "skipped_rows": skipped_count,
        })
        emit_progress(
            "Prepared LinkedIn enrichment queue: "
            f"{queue_count} total, {len(cache_hits)} cached, {len(cache_misses)} RapidAPI fetches, "
            f"{len(recent_failures)} recent failures."
        )
        return PrepareQueueSummary(
            input_rows=len(rows),
            queue_rows=queue_count,
            cache_hit_rows=len(cache_hits),
            paid_call_rows=len(cache_misses),
            recent_failure_rows=len(recent_failures),
            unresolved_rows=unresolved_count,
            skipped_rows=skipped_count,
            route_counts=route_counts,
        )


class EnrichLinkedInProfiles(Node):
    """Step 2. Hydrate the cache hits and fetch the cache misses (rate-limited
    thread pool) into `provider_enriched.csv`.

    The paid-call count is DERIVED from the misses CSV this step was handed, not
    passed in: that file is exactly the rows step 1 classified as misses, so the
    two can never disagree. The store still gates on step 1's count before this
    node is constructed — the guard here only stops a direct caller from
    spending against a missing key."""

    name = "enrich_linkedin_profiles"
    inputs = (
        Artifact(path=CACHE_HITS_CSV, row_model=EnrichCacheRow),
        Artifact(path=CACHE_MISSES_CSV, row_model=EnrichCacheRow),
    )
    outputs = (Artifact(path=PROVIDER_ENRICHED_CSV, row_model=EnrichProviderRow, writes="full_rewrite"),)
    payload = EnrichLinkedInSummary
    manifest = ""  # reports into EnrichPeople's one stage manifest.json

    def __init__(self, cfg: EnrichConfig) -> None:
        self.cfg = cfg
        self.artifact_dir = cfg.artifact_dir
        self.cache_hits_csv = self.artifact_dir / CACHE_HITS_FILE
        self.cache_misses_csv = self.artifact_dir / CACHE_MISSES_FILE
        self.provider_enriched_csv = self.artifact_dir / PROVIDER_ENRICHED_FILE
        self.artifacts: dict[str, Any] = {}
        self.counts: dict[str, Any] = {}

    def bindings(self) -> dict[str, str]:
        return {
            CACHE_HITS_CSV: str(self.cache_hits_csv),
            CACHE_MISSES_CSV: str(self.cache_misses_csv),
            PROVIDER_ENRICHED_CSV: str(self.provider_enriched_csv),
        }

    def execute(self) -> EnrichLinkedInSummary:
        cfg = self.cfg
        hit_rows = CsvIO.read_dict_rows(self.cache_hits_csv)
        miss_rows = CsvIO.read_dict_rows(self.cache_misses_csv)
        rows = hit_rows + miss_rows
        self.artifacts["provider_enriched_csv"] = str(self.provider_enriched_csv)
        if not rows:
            CsvIO.write_dict_rows(self.provider_enriched_csv, PROVIDER_COLUMNS, [])
            emit_progress("No LinkedIn enrichment work needed.")
            return EnrichLinkedInSummary(
                processed=0, cached=0, fetched=0,
                output_file=str(self.provider_enriched_csv), providers={"rapidapi": False},
            )

        paid_call_count = len(miss_rows)
        client = RapidApiClient()
        # Defensive: EnrichPeople.run gates on this before constructing us, but
        # keep the guard so a direct caller cannot silently spend against a
        # missing key. One client is shared across the pool below (it is
        # stateless beyond its key/retry).
        if paid_call_count > 0 and not client.api_key:
            raise PipelineFailed("POWERSET_API_KEY is not set")

        profile_cache_dir = cfg.profile_cache_dir
        max_workers = max(1, int(cfg.max_workers or DEFAULT_RAPIDAPI_MAX_WORKERS))
        max_rpm = cfg.max_rpm
        sleep_seconds = cfg.sleep_seconds
        rate_limiter = StartRateLimiter(max_rpm, sleep_seconds)
        cache_rows = sum(1 for row in rows if row.get("cache_status") == "hit")
        emit_progress(
            "Starting LinkedIn profile enrichment: "
            f"{len(rows)} profiles, {cache_rows} cached, {paid_call_count} to fetch, "
            f"max {max_workers} workers, {max_rpm:g} rpm."
        )

        def enrich_one(row: dict[str, str]) -> tuple[dict[str, Any], bool, int, str]:
            public_identifier = row.get("public_identifier") or extract_public_identifier(row.get("linkedin_url") or "")
            linkedin_url = normalize_linkedin_url(row.get("linkedin_url") or (f"https://www.linkedin.com/in/{public_identifier}" if public_identifier else ""))
            if not public_identifier and linkedin_url:
                public_identifier = extract_public_identifier(linkedin_url)
            is_cache_hit = row.get("cache_status") == "hit"
            if is_cache_hit:
                cached_payload = cached_profile_from_row(row)
                normalized = normalize_linkedin_profile(cached_payload) if cached_payload else None
                if cached_payload and normalized and normalized.get("success") is True:
                    rapid = {"status_code": 200, "data": cached_payload, "error": "", "from_cache": True, "normalized_profile": normalized, "attempts": 1}
                else:
                    cache_path = Path(row.get("cache_path") or "") if row.get("cache_path") else profile_cache_path(profile_cache_dir, public_identifier)
                    cached = read_usable_cached_profile(cache_path)
                    if cached:
                        rapid = {
                            "status_code": 200,
                            "data": cached.get("raw_response"),
                            "error": "",
                            "from_cache": True,
                            "normalized_profile": cached.get("normalized_profile"),
                            "attempts": 1,
                        }
                    else:
                        rapid = {
                            "status_code": 0,
                            "data": None,
                            "error": "cache entry unusable",
                            "from_cache": True,
                            "normalized_profile": {"success": False, "error": "cache entry unusable"},
                            "attempts": 1,
                        }
            else:
                rapid = client.get_profile(
                    public_identifier,
                    linkedin_url,
                    cache_dir=profile_cache_dir,
                    wait_for_attempt=rate_limiter.wait,
                )
                rapid.setdefault("error", str(rapid.get("detail") or ""))
            attempts = max(1, int(rapid.get("attempts") or 1))
            status_code = int(rapid.get("status_code") or 0)
            retry_outcome = "none"
            if attempts > 1:
                retry_outcome = "success" if status_code == 200 else "failed"
            out = dict(row)
            out.update({
                "public_identifier": public_identifier,
                "linkedin_url": linkedin_url,
                "rapidapi_status_code": rapid.get("status_code", ""),
                "rapidapi_error": rapid.get("error", ""),
                "rapidapi_attempts": attempts,
                "rapidapi_retry_outcome": retry_outcome,
                "rapidapi_response_enriched": json.dumps(rapid.get("data")) if rapid.get("data") else "",
                "rapidapi_from_cache": "true" if rapid.get("from_cache") else "false",
                "provider_enriched_at": now_iso(),
            })
            return out, is_cache_hit, attempts, retry_outcome

        enriched_by_index: dict[int, dict[str, Any]] = {}
        cached_count = 0
        fetched_count = 0
        retried_count = 0
        retry_success_count = 0
        retry_failure_count = 0
        processed_count = 0
        last_progress = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_index = {executor.submit(enrich_one, row): index for index, row in enumerate(rows)}
            for future in concurrent.futures.as_completed(future_to_index):
                index = future_to_index[future]
                out, was_cache_hit, attempts, retry_outcome = future.result()
                enriched_by_index[index] = out
                if was_cache_hit:
                    cached_count += 1
                else:
                    fetched_count += 1
                    if attempts > 1:
                        retried_count += 1
                        if retry_outcome == "success":
                            retry_success_count += 1
                        elif retry_outcome == "failed":
                            retry_failure_count += 1
                processed_count += 1
                now = time.monotonic()
                if (
                    processed_count == len(rows)
                    or processed_count % DEFAULT_PROGRESS_INTERVAL_ROWS == 0
                    or now - last_progress >= DEFAULT_PROGRESS_INTERVAL_SECONDS
                ):
                    emit_progress(
                        "LinkedIn profile enrichment progress: "
                        f"{processed_count}/{len(rows)} processed "
                        f"({cached_count} cached, {fetched_count} fetched)."
                    )
                    last_progress = now
        enriched = [enriched_by_index[index] for index in range(len(rows))]
        CsvIO.write_dict_rows(self.provider_enriched_csv, PROVIDER_COLUMNS, enriched)
        self.counts["provider_processed"] = len(enriched)
        emit_progress(f"LinkedIn profile enrichment finished: {len(enriched)} profiles processed.")
        return EnrichLinkedInSummary(
            processed=len(enriched),
            cached=cached_count,
            fetched=fetched_count,
            output_file=str(self.provider_enriched_csv),
            providers={"rapidapi": True},
            max_workers=max_workers,
            max_rpm=max_rpm,
            retried=retried_count,
            retry_successes=retry_success_count,
            retry_failures=retry_failure_count,
        )


class EnrichedPeopleMerge(Node):
    """Step 3. Merge provider profiles back into the input rows and write the
    canonical `people.csv` — EVERY input row, each stamped with its enrichment
    outcome.

    A row the provider could not hydrate keeps its identity columns and comes out
    `enrichment_status=failed` with the provider reason in `enrichment_error`,
    instead of being deleted. Deleting it made a rate-limited fetch
    byte-identical to "never attempted", so a 429 storm silently erased contacts.
    Rows suppressed by a cached prior failure are stamped from
    `rapidapi_recent_failures.csv`; rows the provider never looked at are
    `skipped`."""

    name = "enrich_merge_people"
    inputs = (
        Artifact(path=DEFAULT_INPUT_PEOPLE_CSV, row_model=PeopleRow),
        Artifact(path=PROVIDER_ENRICHED_CSV, row_model=EnrichProviderRow),
        Artifact(path=RECENT_FAILURES_CSV, row_model=EnrichRecentFailureRow),
    )
    outputs = (Artifact(path=ENRICHED_PEOPLE_CSV, row_model=PeopleRow, writes="full_rewrite"),)
    payload = EnrichMergeSummary
    manifest = ""  # reports into EnrichPeople's one stage manifest.json

    def __init__(self, cfg: EnrichConfig) -> None:
        self.cfg = cfg
        self.artifact_dir = cfg.artifact_dir
        self.provider_enriched_csv = self.artifact_dir / PROVIDER_ENRICHED_FILE
        self.recent_failures_csv = self.artifact_dir / RECENT_FAILURES_FILE
        self.people_csv = self.artifact_dir / PEOPLE_FILE
        self.artifacts: dict[str, Any] = {}
        self.counts: dict[str, Any] = {}

    def bindings(self) -> dict[str, str]:
        return {
            DEFAULT_INPUT_PEOPLE_CSV: str(self.cfg.input_csv),
            PROVIDER_ENRICHED_CSV: str(self.provider_enriched_csv),
            RECENT_FAILURES_CSV: str(self.recent_failures_csv),
            ENRICHED_PEOPLE_CSV: str(self.people_csv),
        }

    def execute(self) -> EnrichMergeSummary:
        cfg = self.cfg
        original_rows = [normalize_people_row(row) for row in CsvIO.read_dict_rows(cfg.input_csv)]
        by_key: dict[str, dict[str, Any]] = {}
        for row in original_rows:
            by_key[self._people_row_key(row)] = row
        enriched_rows = CsvIO.read_dict_rows(self.provider_enriched_csv)
        company_lookup = build_company_identity_lookup([Path(p) for p in cfg.company_corpus_jsonl])
        attempted_keys: set[str] = set()
        for row in enriched_rows:
            rapid_raw = json.loads(row["rapidapi_response_enriched"]) if row.get("rapidapi_response_enriched") else (json.loads(row["rapidapi_response"]) if row.get("rapidapi_response") else None)
            public_identifier = row.get("public_identifier") or extract_public_identifier(row.get("linkedin_url") or "")
            rapid = normalize_rapidapi(rapid_raw, public_identifier, row.get("linkedin_url", ""), company_lookup)
            # merge_provider_profile stamps the enriched/failed outcome itself.
            merged = merge_provider_profile(row, rapid, rapid_raw)
            key = self._people_row_key(row)
            attempted_keys.add(key)
            by_key[key] = merged
        # Rows whose fetch was suppressed by a cached prior failure never reach the
        # provider CSV: they were still attempted, so carry the cached reason over.
        for row in CsvIO.read_dict_rows(self.recent_failures_csv):
            key = self._people_row_key(row)
            target = by_key.get(key)
            if target is None:
                continue
            attempted_keys.add(key)
            stamp_enrichment_outcome(target, attempted=True, error=provider_failure_reason(row))
        # Everything the provider never looked at this run is `skipped` — restamped
        # from scratch, so a stale status carried in from the input never survives.
        for key, row in by_key.items():
            if key not in attempted_keys:
                stamp_enrichment_outcome(row, attempted=False)
        rows = list(by_key.values())
        CsvIO.write_dict_rows(self.people_csv, PEOPLE_SCHEMA_COLUMNS, rows)
        statuses = [str(row.get("enrichment_status") or "") for row in rows]
        enriched_count = statuses.count(ENRICHMENT_STATUS_ENRICHED)
        failed_count = statuses.count(ENRICHMENT_STATUS_FAILED)
        skipped_count = statuses.count(ENRICHMENT_STATUS_SKIPPED)
        self.artifacts["people_csv"] = str(self.people_csv)
        self.counts.update({
            "people_rows": len(rows),
            "enriched_rows": enriched_count,
            "failed_rows": failed_count,
            "skipped_rows": skipped_count,
        })
        emit_progress(
            f"Wrote people.csv with {len(rows)} rows "
            f"({enriched_count} enriched, {failed_count} failed, {skipped_count} skipped)."
        )
        return EnrichMergeSummary(
            rows=len(rows),
            enriched_rows=enriched_count,
            failed_rows=failed_count,
            skipped_rows=skipped_count,
            output_file=str(self.people_csv),
        )

    @staticmethod
    def _people_row_key(row: dict[str, Any]) -> str:
        """Identity key used to fold provider/recent-failure rows back onto their
        input row. Same recipe for every source so the three passes agree."""
        return row.get("id") or row.get("public_identifier") or row.get("linkedin_url") or short_hash(json.dumps(row, sort_keys=True))


class EnrichPeople(Node):
    """Idempotent RapidAPI people-enrichment run — the STORE for the three step
    nodes. Owns the fixed artifact dir (the one mkdir), the run order, the spend
    gate, and the single manifest.json. Each step node reports its counts and
    artifact paths, and `execute()` records per-step timing; the Node template
    writes the manifest exactly once, with this stage's declared IO stats.

    Cache hits never need approval. A run that would fetch RapidAPI cache misses
    without `cfg.approve_spend` stops at a `needs_approval` manifest before any
    client is constructed and before any fetch; with approval it proceeds (and
    fails clearly if no RAPIDAPI_* key).

    It declares the stage BOUNDARY input (the fan-in merge's people.csv) and no
    outputs: the three step nodes declare the files, and `people.csv` in
    particular is step 3's write — a second declaration of that path here would be
    a two-writer conflict describing one write. `required=False` on the input for
    the same reason the importers use it: step 1 reports a missing input as a
    typed failure naming the path, and a store-level `not_ready` would throw that
    message away."""

    name = "enrich_people"
    inputs = (Artifact(path=DEFAULT_INPUT_PEOPLE_CSV, row_model=PeopleRow, required=False),)
    outputs = ()
    payload = EnrichManifest
    manifest = str(DEFAULT_ARTIFACT_DIR / MANIFEST_FILE)

    def __init__(self, cfg: EnrichConfig) -> None:
        self.cfg = cfg
        self.artifact_dir = cfg.artifact_dir
        self.artifact_dir.mkdir(parents=True, exist_ok=True)  # the one place the dir is created
        self.manifest_path = self.artifact_dir / MANIFEST_FILE
        self.artifacts: dict[str, Any] = {}
        self.counts: dict[str, Any] = {}
        self.steps: dict[str, Any] = {}
        self.started_at = now_iso()

    def bindings(self) -> dict[str, str]:
        """Declared path -> this run's path, so a run against another artifact dir
        (linkedin/network_import's discover dir, a test's temp dir) still validates
        and stats the same declared contract."""
        return {
            DEFAULT_INPUT_PEOPLE_CSV: str(self.cfg.input_csv),
            self.manifest: str(self.manifest_path),
        }

    def execute(self) -> EnrichManifest:
        prepare = self._step("prepare_queue", EnrichQueuePrepare(self.cfg))
        if prepare.get("status") != STATUS_COMPLETED:
            return self._build(status="failed", error=self._step_error("prepare_queue", prepare))
        paid = int(self.counts.get("paid_call_count") or 0)
        if paid > 0 and not self.cfg.approve_spend:
            # RapidAPI bills every REQUEST, and a cache miss retries up to
            # DEFAULT_RAPIDAPI_RETRY_ATTEMPTS times on transient errors — so one
            # credit per miss is the floor, not the worst case. Quote both.
            attempts = max(1, DEFAULT_RAPIDAPI_RETRY_ATTEMPTS)
            max_credits = paid * attempts
            return self._build(status="needs_approval", needs_approval={
                "reason": "rapidapi_cache_misses",
                "paid_call_count": paid,
                "cache_hit_count": int(self.counts.get("cache_hit_count") or 0),
                "estimated_credits": paid,
                "estimated_credits_is_floor": True,
                "estimated_credits_max": max_credits,
                "retry_attempts": attempts,
                "message": (
                    f"{paid} LinkedIn profiles are not cached and need paid RapidAPI "
                    f"fetches: at least {paid} credits, up to {max_credits} if every "
                    f"fetch exhausts its {attempts} retry attempts (RapidAPI bills each "
                    f"request). Re-run with --approve-spend to proceed."
                ),
            })
        if paid > 0 and not RapidApiClient.resolve_key():
            return self._build(status="failed", error="POWERSET_API_KEY is not set")
        try:
            for step_id, node in (
                ("enrich_linkedin", EnrichLinkedInProfiles(self.cfg)),
                ("merge_people", EnrichedPeopleMerge(self.cfg)),
            ):
                body = self._step(step_id, node)
                if body.get("status") != STATUS_COMPLETED:
                    return self._build(status="failed", error=self._step_error(step_id, body))
        except PipelineFailed as exc:
            return self._build(status="failed", error=str(exc))
        return self._build(status="completed")

    def _step(self, step_id: str, node: Node) -> dict[str, Any]:
        """Run one step node, absorb its counts + artifact paths, and record its
        timing and typed payload in the stage manifest's `steps` block.

        Returns the step payload's DICT form: it is embedded verbatim as the step's
        `summary`, and the store only ever branches on its `status`."""
        started = now_iso()
        clock = time.monotonic()
        body = node.run().to_payload()
        self.artifacts.update(node.artifacts)
        self.counts.update(node.counts)
        self.steps[step_id] = {
            "status": body.get("status", ""),
            "started_at": started,
            "finished_at": now_iso(),
            "duration_seconds": round(time.monotonic() - clock, 3),
            "summary": body,
        }
        return body

    @staticmethod
    def _step_error(step_id: str, body: dict[str, Any]) -> str:
        """One line naming the step that did not complete and why (the Node
        template's `not_ready` payload carries the missing input paths)."""
        detail = ", ".join(body.get("missing_inputs") or ()) or str(body.get("reason") or "")
        return f"{step_id} {body.get('status', 'did not complete')}" + (f": {detail}" if detail else "")

    def _build(self, *, status: str, needs_approval: dict[str, Any] | None = None, error: str | None = None) -> EnrichManifest:
        """Assemble this stage's typed payload. The Node template writes it."""
        return EnrichManifest(
            status=status,
            artifact_dir=str(self.artifact_dir),
            input=self.cfg.manifest_input(),
            counts=self.counts,
            artifacts=self.artifacts,
            steps=self.steps,
            needs_approval=needs_approval,
            error=error,
            started_at=self.started_at,
            updated_at=now_iso(),
        )

    @staticmethod
    def command_check_keys(_: argparse.Namespace) -> int:
        """Report whether the Powerset gateway key is configured. Kept as a class surface
        (not folded into main) because `imports/linkedin/network_import.py`
        delegates its own `check-keys` command straight to it."""
        emit({
            "status": "ok",
            "provider": "rapidapi",
            "keys_present": {
                "POWERSET_API_KEY": bool(os.getenv("POWERSET_API_KEY", "").strip()),
            },
        })
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified people enrichment flow for shared people schema CSVs")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--input", required=True, help="Input shared people schema CSV, e.g. merged people CSV")
    run.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    run.add_argument("--artifact-dir", default="", help=argparse.SUPPRESS)
    run.add_argument("--approve-spend", action="store_true", help="Authorize paid RapidAPI fetches for cache misses (otherwise a run with misses stops at needs_approval)")
    run.add_argument("--force", action="store_true", help="Re-enrich rows even if they appear complete")
    run.add_argument("--profile-cache-dir", default=str(DEFAULT_BASE_DIR / "profile_cache_v2"))
    run.add_argument("--company-corpus-jsonl", action="append", default=[])
    run.add_argument("--sleep-seconds", type=float, default=0.0)
    run.add_argument("--max-workers", type=int, default=DEFAULT_RAPIDAPI_MAX_WORKERS)
    run.add_argument("--max-rpm", type=float, default=DEFAULT_RAPIDAPI_MAX_RPM)
    run.add_argument("--failure-retry-hours", type=float, default=DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS)
    run.add_argument("--limit", type=int, help=argparse.SUPPRESS)

    status = sub.add_parser("status")
    status.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    status.add_argument("--artifact-dir", default="", help=argparse.SUPPRESS)

    sub.add_parser("check-keys")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse, construct, run, emit; map the manifest status to an exit code."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check-keys":
            return EnrichPeople.command_check_keys(args)
        artifact_dir = Path(args.artifact_dir) if args.artifact_dir else Path(args.output_dir) / "enrichment"
        if args.command == "status":
            manifest = read_json(artifact_dir / MANIFEST_FILE, {}) or {}
            emit({
                "status": manifest.get("status", "unknown"),
                "artifact_dir": str(artifact_dir),
                "counts": manifest.get("counts", {}),
                "artifacts": manifest.get("artifacts", {}),
                "steps": manifest.get("steps", {}),
                "needs_approval": manifest.get("needs_approval"),
            })
            return 0
        manifest = EnrichPeople(build_config(
            input_csv=args.input,
            artifact_dir=artifact_dir,
            profile_cache_dir=args.profile_cache_dir,
            limit=args.limit,
            force=args.force,
            company_corpus_jsonl=args.company_corpus_jsonl,
            sleep_seconds=args.sleep_seconds,
            max_workers=args.max_workers,
            max_rpm=args.max_rpm,
            failure_retry_hours=args.failure_retry_hours,
            approve_spend=args.approve_spend,
        )).run()
        emit(manifest_emit_payload(manifest))
        return exit_code_for_status(manifest.status)
    except ValueError as exc:
        emit({"status": "error", "error": str(exc)})
        return 2
    except KeyboardInterrupt:
        emit({"status": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
