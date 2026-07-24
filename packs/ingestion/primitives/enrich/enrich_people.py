#!/usr/bin/env python3
"""Unified local people enrichment flow (RapidAPI-only).

Self-contained Powerpacks RapidAPI enrichment implementation. No imports from
the legacy app or hosted search API.

This file is the orchestrator + CLI only. The stage is decomposed into sibling
modules, one concern each — import from the defining module, not through here:

- `models.py` — EnrichConfig/build_config, EnrichManifest, the stage CSV
  columns, PipelineFailed.
- `rapidapi_client.py` — the RapidApiClient class: key/env handling, http_json,
  retry/backoff, the cache-aware `fetch_profile`, DEFAULT_RAPIDAPI_* knobs.
- `profile_cache.py` — profile-cache slugs/paths/reads, failure TTL,
  cache-status classification, and the cache seeding format documentation.
- `profile_transforms.py` — pure row transforms: route_row, normalize_rapidapi,
  merge_provider_profile, confirmed_people_row.

Consumers:
- Primary whole-pipeline owner: ``imports/linkedin/network_import.py``.
- Shared-library consumers (deep-context profile hydration/reconciliation,
  search's ``fetch_person_profile``) import the sibling modules directly.

Input: a shared people schema CSV, usually merge_network_sources output.
Output: enriched people schema CSV plus raw provider responses.

RapidAPI LinkedIn hydration runs directly when RAPIDAPI_LINKEDIN_KEY or
RAPIDAPI_KEY is present (checked in that order). Missing keys fail clearly
instead of opening an approval step.

Contract: ONE idempotent `run` (plus `status`, which reads the stage manifest,
and `check-keys`). A run writes its output CSVs and one `manifest.json` into a
fixed artifact directory (default `.powerpacks/network-import/enrichment/`,
override with `--artifact-dir`/`--output-dir`) and overwrites in place — there
is no ledger, no `continue`, no per-step state store. Reruns are idempotent
because the output path is stable. The manifest holds status, per-step timing,
counts, and the artifact paths.

Steps (run in order inside `run`):
1. prepare_queue: routes rows with LinkedIn URLs/public identifiers and
   profile gaps to `linkedin_enrichment_queue.csv`, splits them by the local
   profile cache into `rapidapi_cache_hits.csv`, `rapidapi_cache_misses.csv`,
   and `rapidapi_recent_failures.csv`; rows without LinkedIn go to
   `needs_resolution_queue.csv`, complete-looking rows to
   `skipped_enrichment.csv`.
2. enrich_linkedin: fetches cache misses, hydrates hits + fetches into
   `provider_enriched.csv`, saves raw payloads to `raw_provider_responses/`.
3. merge_people: merges profile data back into the input rows and writes
   canonical `people.csv`.

Spend gate: cache hits never need approval. If prepare_queue finds RapidAPI
cache misses (paid fetches) and `--approve-spend` was not passed, `run` writes a
`needs_approval` manifest with the miss count + credit estimate and exits
nonzero-but-clean (code 20) BEFORE any fetch. With `--approve-spend` it proceeds
(and still fails clearly if no RAPIDAPI_* key is set).

Usage:
    enrich_people.py run --input .powerpacks/network-import/merged/people.csv [--approve-spend]
    enrich_people.py status | check-keys

Options: `--profile-cache-dir` (default
`.powerpacks/network-import/profile_cache_v2`), `--refresh-cache` (force
RapidAPI calls despite cache entries), `--company-corpus-jsonl` (repeatable;
company metadata by RapidAPI company ID or LinkedIn company slug),
`--max-workers`/`--max-rpm` (defaults 64 workers / 300 RPM, env-overridable),
`--failure-retry-hours` (skip recently failed lookups; default 24h),
`--approve-spend` (authorize paid RapidAPI fetches for cache misses), `--force`
(re-enrich complete-looking rows), hidden `--limit` for tiny smoke tests only.

Cache seeding format is documented in `profile_cache.py`. Company identity
field behavior is documented in `profile_transforms.py`.

Changelog:
  2026-07-23 (audit oo-cli): the CLI command handlers (command_run/status/
    check_keys) moved onto EnrichPeople so the class is the single entry point:
    command_run is a @classmethod that instantiates + runs the orchestrator;
    status/check_keys are @staticmethods. build_parser/main dispatch to
    EnrichPeople.command_*. RapidAPI access is now through the RapidApiClient
    class (resolve_key/fetch_profile) instead of the module rapidapi_key/
    rapidapi_profile functions.
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
  2026-07-23 (audit): replaced the per-step ledger runner (load_ledger/
    save_ledger/mark_step/next_pending_step/approval_id/is_approved/
    block_for_approval/PIPELINE_STEPS/execute_step/ensure_keys/
    run_until_blocked_or_done/command_continue/command_approve) with an
    EnrichPeople orchestrator that owns the fixed artifact dir, the three
    steps, and one manifest.json. Spend is now gated by an explicit
    `--approve-spend` flag (a needs_approval manifest + clean nonzero exit on
    cache misses) instead of the dead approval machinery; `continue`/`approve`
    are gone. The pure helpers and the cache seeding / failure-TTL behavior are
    unchanged.
  2026-07-23 (audit): dropped the local byte-identical read_csv/write_csv for
    the shared CsvIO.read_dict_rows / CsvIO.write_dict_rows; `import csv`
    dropped with them.
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
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.gates import EXIT_NEEDS_APPROVAL, exit_code_for_status, manifest_emit_payload  # noqa: E402
from packs.ingestion.primitives.common.jsonio import emit, now_iso, read_json, short_hash, write_json  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR  # noqa: E402
from packs.ingestion.primitives.common.proc import emit_progress as _emit_progress  # noqa: E402
from packs.ingestion.primitives.enrich.models import (  # noqa: E402
    CACHE_COLUMNS,
    EnrichConfig,
    EnrichManifest,
    PROVIDER_COLUMNS,
    PipelineFailed,
    QUEUE_COLUMNS,
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
    confirmed_people_row,
    merge_provider_profile,
    normalize_rapidapi,
    route_row,
)
from packs.ingestion.primitives.enrich.rapidapi_client import (  # noqa: E402
    DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS,
    DEFAULT_RAPIDAPI_MAX_RPM,
    DEFAULT_RAPIDAPI_MAX_WORKERS,
    RapidApiClient,
)
from packs.ingestion.schemas.company_identity import build_company_identity_lookup  # noqa: E402
from packs.ingestion.schemas.linkedin_profile_normalizer import normalize_linkedin_profile  # noqa: E402
from packs.ingestion.schemas.people_schema import (  # noqa: E402
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


def emit_progress(message: str) -> None:
    """Write one progress line to stderr, tagged for the enrich-people chain."""
    _emit_progress(message, "[enrich-people]")


class EnrichPeople:
    """Idempotent RapidAPI people-enrichment run. Owns the fixed artifact dir,
    the prepare_queue -> enrich_linkedin -> merge_people steps, the spend gate,
    and the single manifest.json. The steps mutate self.artifacts / self.counts;
    `run` records per-step timing and writes the manifest exactly once.

    Cache hits never need approval. A run that would fetch RapidAPI cache misses
    without `cfg.approve_spend` stops at a `needs_approval` manifest before any
    fetch; with approval it proceeds (and fails clearly if no RAPIDAPI_* key)."""

    def __init__(self, cfg: EnrichConfig) -> None:
        self.cfg = cfg
        self.artifact_dir = cfg.artifact_dir
        self.artifact_dir.mkdir(parents=True, exist_ok=True)  # the one place the dir is created
        self.manifest_path = self.artifact_dir / "manifest.json"
        self.artifacts: dict[str, Any] = {}
        self.counts: dict[str, Any] = {}
        self.steps: dict[str, Any] = {}
        self.started_at = now_iso()

    def run(self) -> EnrichManifest:
        self._timed("prepare_queue", self.prepare_queue)
        paid = int(self.counts.get("paid_call_count") or 0)
        if paid > 0 and not self.cfg.approve_spend:
            return self._write(status="needs_approval", needs_approval={
                "reason": "rapidapi_cache_misses",
                "paid_call_count": paid,
                "cache_hit_count": int(self.counts.get("cache_hit_count") or 0),
                # RapidAPI bills one credit per profile fetch (cache misses only).
                "estimated_credits": paid,
                "message": (
                    f"{paid} LinkedIn profiles are not cached and need paid RapidAPI "
                    f"fetches (~{paid} credits). Re-run with --approve-spend to proceed."
                ),
            })
        if paid > 0 and not RapidApiClient.resolve_key():
            return self._write(status="failed", error="RAPIDAPI_LINKEDIN_KEY/RAPIDAPI_KEY is not set")
        try:
            self._timed("enrich_linkedin", self.enrich_linkedin)
            self._timed("merge_people", self.merge_people)
        except PipelineFailed as exc:
            return self._write(status="failed", error=str(exc))
        return self._write(status="completed")

    def _timed(self, step_id: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        started = now_iso()
        clock = time.monotonic()
        summary = fn()
        self.steps[step_id] = {
            "status": "completed",
            "started_at": started,
            "finished_at": now_iso(),
            "duration_seconds": round(time.monotonic() - clock, 3),
            "summary": summary,
        }
        return summary

    def _write(self, *, status: str, needs_approval: dict[str, Any] | None = None, error: str | None = None) -> EnrichManifest:
        manifest = EnrichManifest(
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
        write_json(self.manifest_path, manifest.to_dict())
        return manifest

    def prepare_queue(self) -> dict[str, Any]:
        """Route input rows and split the LinkedIn-provider rows by local cache
        state into queue / cache_hits / cache_misses / recent_failures CSVs;
        record counts (incl. paid_call_count = cache misses) and artifact paths."""
        cfg = self.cfg
        rows = [normalize_people_row(row) for row in CsvIO.read_dict_rows(cfg.input_csv)]
        if cfg.limit:
            rows = rows[: int(cfg.limit)]
        queue: list[dict[str, Any]] = []
        cache_hits: list[dict[str, Any]] = []
        cache_misses: list[dict[str, Any]] = []
        recent_failures: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        route_counts: dict[str, int] = {}
        profile_cache_dir = cfg.profile_cache_dir
        refresh_cache = cfg.refresh_cache
        cache_index = set() if refresh_cache else profile_cache_index(profile_cache_dir)
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
                    lambda row: classify_rapidapi_cache_status(row, profile_cache_dir, refresh_cache, failure_retry_hours, cache_index),
                    provider_rows,
                ))
        classification_iter = iter(classifications)
        for route, row in routed:
            if route == "linkedin_provider":
                queue.append(row)
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
                unresolved.append(row)
            else:
                skipped.append(row)
        run_dir = self.artifact_dir
        queue_path = run_dir / "linkedin_enrichment_queue.csv"
        cache_hits_path = run_dir / "rapidapi_cache_hits.csv"
        cache_misses_path = run_dir / "rapidapi_cache_misses.csv"
        recent_failures_path = run_dir / "rapidapi_recent_failures.csv"
        unresolved_path = run_dir / "needs_resolution_queue.csv"
        skipped_path = run_dir / "skipped_enrichment.csv"
        CsvIO.write_dict_rows(queue_path, QUEUE_COLUMNS, queue)
        CsvIO.write_dict_rows(cache_hits_path, CACHE_COLUMNS, cache_hits)
        CsvIO.write_dict_rows(cache_misses_path, CACHE_COLUMNS, cache_misses)
        CsvIO.write_dict_rows(recent_failures_path, RECENT_FAILURE_COLUMNS, recent_failures)
        CsvIO.write_dict_rows(unresolved_path, QUEUE_COLUMNS, unresolved)
        CsvIO.write_dict_rows(skipped_path, QUEUE_COLUMNS, skipped)
        self.artifacts.update({
            "linkedin_enrichment_queue_csv": str(queue_path),
            "rapidapi_cache_hits_csv": str(cache_hits_path),
            "rapidapi_cache_misses_csv": str(cache_misses_path),
            "rapidapi_recent_failures_csv": str(recent_failures_path),
            "needs_resolution_queue_csv": str(unresolved_path),
            "skipped_enrichment_csv": str(skipped_path),
        })
        self.counts.update({
            "input_rows": len(rows),
            "queue_count": len(queue),
            "cache_hit_count": len(cache_hits),
            "paid_call_count": len(cache_misses),
            "recent_failure_count": len(recent_failures),
            "unresolved_rows": len(unresolved),
            "skipped_rows": len(skipped),
        })
        emit_progress(
            "Prepared LinkedIn enrichment queue: "
            f"{len(queue)} total, {len(cache_hits)} cached, {len(cache_misses)} RapidAPI fetches, "
            f"{len(recent_failures)} recent failures."
        )
        return {
            "input_rows": len(rows),
            "queue_rows": len(queue),
            "cache_hit_rows": len(cache_hits),
            "paid_call_rows": len(cache_misses),
            "recent_failure_rows": len(recent_failures),
            "unresolved_rows": len(unresolved),
            "skipped_rows": len(skipped),
            "route_counts": route_counts,
        }

    def enrich_linkedin(self) -> dict[str, Any]:
        """Hydrate cache hits + fetch cache misses (rate-limited thread pool) into
        provider_enriched.csv, saving raw payloads under raw_provider_responses/."""
        cfg = self.cfg
        hit_path_text = self.artifacts.get("rapidapi_cache_hits_csv") or ""
        miss_path_text = self.artifacts.get("rapidapi_cache_misses_csv") or ""
        hit_path = Path(hit_path_text) if hit_path_text else None
        miss_path = Path(miss_path_text) if miss_path_text else None
        rows = []
        if hit_path and hit_path.is_file():
            rows.extend(CsvIO.read_dict_rows(hit_path))
        if miss_path and miss_path.is_file():
            rows.extend(CsvIO.read_dict_rows(miss_path))
        if not rows:
            out_path = self.artifact_dir / "provider_enriched.csv"
            CsvIO.write_dict_rows(out_path, PROVIDER_COLUMNS, [])
            self.artifacts["provider_enriched_csv"] = str(out_path)
            emit_progress("No LinkedIn enrichment work needed.")
            return {"processed": 0, "cached": 0, "fetched": 0, "output_file": str(out_path), "providers": {"rapidapi": False}}

        paid_call_count = int(self.counts.get("paid_call_count") or 0)
        client = RapidApiClient()
        # Defensive: run() gates on this before calling us, but keep the guard so
        # a direct caller cannot silently spend against a missing key. One client
        # is shared across the pool below (it is stateless beyond its key/retry).
        if paid_call_count > 0 and not client.api_key:
            raise PipelineFailed("RAPIDAPI_LINKEDIN_KEY/RAPIDAPI_KEY is not set")

        profile_cache_dir = cfg.profile_cache_dir
        refresh_cache = cfg.refresh_cache
        max_workers = max(1, int(cfg.max_workers or DEFAULT_RAPIDAPI_MAX_WORKERS))
        max_rpm = cfg.max_rpm
        sleep_seconds = cfg.sleep_seconds
        rate_limiter = StartRateLimiter(max_rpm, sleep_seconds)
        raw_dir = self.artifact_dir / "raw_provider_responses"
        raw_dir.mkdir(parents=True, exist_ok=True)
        cache_rows = sum(1 for row in rows if row.get("cache_status") == "hit")
        emit_progress(
            "Starting LinkedIn profile enrichment: "
            f"{len(rows)} profiles, {cache_rows} cached, {paid_call_count} to fetch, "
            f"max {max_workers} workers, {max_rpm:g} rpm."
        )

        def enrich_one(row: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any], bool, int, str]:
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
                rapid = client.fetch_profile(
                    public_identifier,
                    linkedin_url,
                    cache_dir=profile_cache_dir,
                    refresh_cache=refresh_cache,
                    wait_for_attempt=rate_limiter.wait,
                )
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
            raw_payload = {"input": row, "rapidapi": rapid, "cache_hit": bool(rapid.get("from_cache"))}
            return out, raw_payload, is_cache_hit, attempts, retry_outcome

        enriched_by_index: dict[int, dict[str, Any]] = {}
        raw_by_index: dict[int, dict[str, Any]] = {}
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
                out, raw_payload, was_cache_hit, attempts, retry_outcome = future.result()
                enriched_by_index[index] = out
                raw_by_index[index] = raw_payload
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
        enriched: list[dict[str, Any]] = []
        for index in range(len(rows)):
            out = enriched_by_index[index]
            raw_payload = raw_by_index[index]
            public_identifier = out.get("public_identifier") or extract_public_identifier(out.get("linkedin_url") or "")
            write_json(raw_dir / f"{public_identifier or short_hash(out.get('linkedin_url') or out.get('id',''))}.json", raw_payload)
            enriched.append(out)
        out_path = self.artifact_dir / "provider_enriched.csv"
        CsvIO.write_dict_rows(out_path, PROVIDER_COLUMNS, enriched)
        self.artifacts.update({"provider_enriched_csv": str(out_path), "raw_provider_responses_dir": str(raw_dir)})
        self.counts["provider_processed"] = len(enriched)
        emit_progress(f"LinkedIn profile enrichment finished: {len(enriched)} profiles processed.")
        return {
            "processed": len(enriched),
            "cached": cached_count,
            "fetched": fetched_count,
            "output_file": str(out_path),
            "providers": {"rapidapi": True},
            "max_workers": max_workers,
            "max_rpm": max_rpm,
            "retried": retried_count,
            "retry_successes": retry_success_count,
            "retry_failures": retry_failure_count,
        }

    def merge_people(self) -> dict[str, Any]:
        """Merge provider profiles back into the input rows and write the
        canonical people.csv (confirmed rows only)."""
        cfg = self.cfg
        original_rows = [normalize_people_row(row) for row in CsvIO.read_dict_rows(cfg.input_csv)]
        by_key: dict[str, dict[str, Any]] = {}
        for row in original_rows:
            key = row.get("id") or row.get("public_identifier") or row.get("linkedin_url") or short_hash(json.dumps(row, sort_keys=True))
            by_key[key] = row
        provider_path = Path(self.artifacts.get("provider_enriched_csv") or self.artifacts.get("linkedin_enrichment_queue_csv"))
        enriched_rows = CsvIO.read_dict_rows(provider_path) if provider_path and provider_path.exists() else []
        company_lookup = build_company_identity_lookup([Path(p) for p in cfg.company_corpus_jsonl])
        for row in enriched_rows:
            rapid_raw = json.loads(row["rapidapi_response_enriched"]) if row.get("rapidapi_response_enriched") else (json.loads(row["rapidapi_response"]) if row.get("rapidapi_response") else None)
            public_identifier = row.get("public_identifier") or extract_public_identifier(row.get("linkedin_url") or "")
            rapid = normalize_rapidapi(rapid_raw, public_identifier, row.get("linkedin_url", ""), company_lookup)
            merged = merge_provider_profile(row, rapid, rapid_raw)
            key = row.get("id") or row.get("public_identifier") or row.get("linkedin_url") or short_hash(json.dumps(row, sort_keys=True))
            by_key[key] = merged
        output = self.artifact_dir / "people.csv"
        unfiltered_rows = list(by_key.values())
        rows = [row for row in unfiltered_rows if confirmed_people_row(row)]
        CsvIO.write_dict_rows(output, PEOPLE_SCHEMA_COLUMNS, rows)
        self.artifacts["people_csv"] = str(output)
        self.counts["people_rows"] = len(rows)
        filtered_rows = len(unfiltered_rows) - len(rows)
        emit_progress(f"Wrote people.csv with {len(rows)} confirmed rows.")
        return {"rows": len(rows), "unfiltered_rows": len(unfiltered_rows), "filtered_rows": filtered_rows, "output_file": str(output)}

    # ---- CLI command handlers ----
    # The class is the single entry point for running enrichment: `command_run`
    # builds a config and instantiates + runs this orchestrator; `command_status`
    # and `command_check_keys` are read-only queries that need no instance. The
    # module `build_parser`/`main` wire argparse to these.
    @classmethod
    def command_run(cls, args: argparse.Namespace) -> int:
        artifact_dir = Path(args.artifact_dir) if args.artifact_dir else Path(args.output_dir) / "enrichment"
        cfg = build_config(
            input_csv=args.input,
            artifact_dir=artifact_dir,
            profile_cache_dir=args.profile_cache_dir,
            limit=args.limit,
            force=args.force,
            refresh_cache=args.refresh_cache,
            company_corpus_jsonl=args.company_corpus_jsonl,
            sleep_seconds=args.sleep_seconds,
            max_workers=args.max_workers,
            max_rpm=args.max_rpm,
            failure_retry_hours=args.failure_retry_hours,
            approve_spend=args.approve_spend,
        )
        manifest = cls(cfg).run()
        emit(manifest_emit_payload(manifest))
        return exit_code_for_status(manifest.status)

    @staticmethod
    def command_status(args: argparse.Namespace) -> int:
        artifact_dir = Path(args.artifact_dir) if args.artifact_dir else Path(args.output_dir) / "enrichment"
        manifest = read_json(artifact_dir / "manifest.json", {}) or {}
        emit({
            "status": manifest.get("status", "unknown"),
            "artifact_dir": str(artifact_dir),
            "counts": manifest.get("counts", {}),
            "artifacts": manifest.get("artifacts", {}),
            "steps": manifest.get("steps", {}),
            "needs_approval": manifest.get("needs_approval"),
        })
        return 0

    @staticmethod
    def command_check_keys(_: argparse.Namespace) -> int:
        emit({
            "status": "ok",
            "provider": "rapidapi",
            "keys_present": {
                "RAPIDAPI_KEY": bool(os.getenv("RAPIDAPI_KEY", "").strip()),
                "RAPIDAPI_LINKEDIN_KEY": bool(os.getenv("RAPIDAPI_LINKEDIN_KEY", "").strip()),
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
    run.add_argument("--refresh-cache", action="store_true", help="Force RapidAPI calls even when a successful local cache entry exists")
    run.add_argument("--company-corpus-jsonl", action="append", default=[])
    run.add_argument("--sleep-seconds", type=float, default=0.0)
    run.add_argument("--max-workers", type=int, default=DEFAULT_RAPIDAPI_MAX_WORKERS)
    run.add_argument("--max-rpm", type=float, default=DEFAULT_RAPIDAPI_MAX_RPM)
    run.add_argument("--failure-retry-hours", type=float, default=DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS)
    run.add_argument("--limit", type=int, help=argparse.SUPPRESS)
    run.set_defaults(func=EnrichPeople.command_run)

    status = sub.add_parser("status")
    status.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    status.add_argument("--artifact-dir", default="", help=argparse.SUPPRESS)
    status.set_defaults(func=EnrichPeople.command_status)

    keys = sub.add_parser("check-keys")
    keys.set_defaults(func=EnrichPeople.command_check_keys)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        emit({"status": "error", "error": str(exc)})
        return 2
    except KeyboardInterrupt:
        emit({"status": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
