#!/usr/bin/env python3
"""Unified local people enrichment flow (RapidAPI-only).

Self-contained Powerpacks RapidAPI enrichment implementation. No imports from
the legacy app or hosted search API.

Consumers:
- Primary whole-pipeline owner: ``discover/linkedin/network_import.py``.
- Shared-library consumers: deep-context profile hydration/reconciliation
  primitives and search's ``fetch_person_profile`` primitive.

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

Cache seeding: a cache file per sanitized LinkedIn public identifier, e.g.
`profile_cache_v2/jane-example.json` containing `fetched_at`,
`public_identifier`, `linkedin_url`, `raw_response`, and
`normalized_profile: {"success": true}`. Usable entries enrich without
RAPIDAPI_* keys. Failed lookups are cached with `last_checked_at` and retried
only after the TTL.

Company identity: work experiences preserve `rapidapi_company_id`,
`company_public_identifier`, `company_linkedin_url`, and `company_key`
(`rapidapi:{id}` preferred over `linkedin_company:{slug}`).
`current_company_urn` is a legacy shared-schema field not populated here.

Changelog:
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
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.schemas.company_identity import build_company_identity_lookup, rapidapi_experience_to_powerpacks
from packs.ingestion.schemas.linkedin_profile_normalizer import normalize_linkedin_profile
from packs.ingestion.schemas.people_schema import (
    PEOPLE_SCHEMA_COLUMNS,
    extract_public_identifier,
    generate_person_id as generate_linkedin_person_id,
    normalize_linkedin_url,
    normalize_people_row,
    parse_jsonish,
    stable_person_id_from_key,
)
from packs.ingestion.primitives.common.gates import EXIT_NEEDS_APPROVAL, exit_code_for_status, manifest_emit_payload
from packs.ingestion.primitives.common.jsonio import emit, now_iso, read_json, short_hash, write_json
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR
from packs.ingestion.primitives.common.proc import emit_progress as _emit_progress
from packs.shared.csv_io import CsvIO
from packs.shared.rate_limiter import StartRateLimiter

RAPIDAPI_BASE_URL = "https://professional-network-data.p.rapidapi.com"
DEFAULT_RAPIDAPI_MAX_WORKERS = int(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_MAX_WORKERS", "64"))
DEFAULT_RAPIDAPI_MAX_RPM = float(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_MAX_RPM", "300"))
DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS = float(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_FAILURE_RETRY_HOURS", "24"))
DEFAULT_RAPIDAPI_RETRY_ATTEMPTS = int(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_RETRY_ATTEMPTS", "3"))
DEFAULT_RAPIDAPI_RETRY_BACKOFF_SECONDS = float(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_RETRY_BACKOFF_SECONDS", "1.0"))
DEFAULT_PROGRESS_INTERVAL_SECONDS = float(os.environ.get("POWERPACKS_RAPIDAPI_PROGRESS_INTERVAL_SECONDS", "60"))
DEFAULT_PROGRESS_INTERVAL_ROWS = int(os.environ.get("POWERPACKS_RAPIDAPI_PROGRESS_INTERVAL_ROWS", "100"))
# `run` exit code when paid RapidAPI cache-miss fetches are gated behind
# --approve-spend. The value + the status->code mapping live in common/gates.py;
# kept here as a module alias for the name callers/tests already reach for.
NEEDS_APPROVAL_CODE = EXIT_NEEDS_APPROVAL

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


def load_dotenv(path: Path, keys: set[str] | None = None) -> None:
    """Load simple KEY=VALUE entries without overriding the shell env."""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ or (keys is not None and key not in keys):
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


load_dotenv(Path(__file__).resolve().parents[4] / ".env", {"RAPIDAPI_LINKEDIN_KEY", "RAPIDAPI_KEY"})


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def emit_progress(message: str) -> None:
    """Write one progress line to stderr, tagged for the enrich-people chain."""
    _emit_progress(message, "[enrich-people]")


def split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def generate_person_id(public_identifier: str, fallback: str = "") -> str:
    if public_identifier:
        return generate_linkedin_person_id(public_identifier)
    return stable_person_id_from_key(f"person:{fallback}")


def count_items(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return len(parsed) if isinstance(parsed, list) else 0
        except json.JSONDecodeError:
            return 0
    return 0


def profile_richness(experiences: Any, education: Any) -> int:
    return count_items(experiences) + count_items(education)


def _explicit_current_value(exp: dict[str, Any]) -> bool | None:
    for key in ("is_current_position", "is_current", "current"):
        if key not in exp:
            continue
        value = exp.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "y"}:
                return True
            if lowered in {"false", "0", "no", "n"}:
                return False
    return None


def current_position(experiences: list[dict[str, Any]]) -> tuple[str, str, str]:
    first_exp: dict[str, Any] | None = None
    for exp in experiences or []:
        if not isinstance(exp, dict):
            continue
        first_exp = first_exp or exp
        explicit_current = _explicit_current_value(exp)
        if explicit_current is True or (explicit_current is None and not (exp.get("ends_at") or exp.get("end_date"))):
            return (
                str(exp.get("title") or exp.get("position") or ""),
                str(exp.get("company_name") or exp.get("company") or exp.get("organization") or ""),
                "",
            )
    if first_exp:
        return (
            str(first_exp.get("title") or first_exp.get("position") or ""),
            str(first_exp.get("company_name") or first_exp.get("company") or first_exp.get("organization") or ""),
            "",
        )
    return "", "", ""


def http_json(method: str, url: str, *, headers: dict[str, str] | None = None, params: dict[str, str] | None = None, timeout: int = 60) -> tuple[int, dict[str, Any] | None, str]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else None, ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = None
        return exc.code, data, raw[:1000]
    except Exception as exc:
        return 0, None, str(exc)


def rapidapi_key() -> str:
    return os.getenv("RAPIDAPI_LINKEDIN_KEY", "").strip() or os.getenv("RAPIDAPI_KEY", "").strip()


def safe_cache_slug(public_identifier: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in public_identifier.lower().strip())
    return cleaned.strip("._")


def legacy_byte_cache_slug(public_identifier: str) -> str:
    parts: list[str] = []
    for ch in public_identifier.lower().strip():
        if ch.isascii() and (ch.isalnum() or ch in {"-", "_", "."}):
            parts.append(ch)
        elif ch.isascii():
            parts.append("_")
        else:
            parts.extend(f"_{byte:02x}" for byte in ch.encode("utf-8"))
    return "".join(parts).strip("._")


def cache_slug_candidates(public_identifier: str) -> list[str]:
    values = [
        public_identifier,
        urllib.parse.unquote(public_identifier or ""),
    ]
    slugs: list[str] = []
    for value in values:
        for slug in (value.lower().strip(), safe_cache_slug(value), legacy_byte_cache_slug(value)):
            if slug and slug not in slugs:
                slugs.append(slug)
    return slugs


def profile_cache_index(cache_dir: Path | str | None) -> set[str]:
    if not cache_dir:
        return set()
    root = Path(cache_dir)
    if not root.exists() or not root.is_dir():
        return set()
    return {path.stem for path in root.glob("*.json") if path.name != "_metadata.json"}


def profile_cache_path(cache_dir: Path | str | None, public_identifier: str) -> Path | None:
    slug = cache_slug_candidates(public_identifier)[0] if public_identifier else ""
    if not cache_dir or not slug:
        return None
    return Path(cache_dir) / f"{slug}.json"


def indexed_profile_cache_path(cache_dir: Path | str | None, public_identifier: str, cache_index: set[str] | None) -> Path | None:
    if not cache_dir:
        return None
    if cache_index is not None:
        for slug in cache_slug_candidates(public_identifier):
            if slug in cache_index:
                return Path(cache_dir) / f"{slug}.json"
        return profile_cache_path(cache_dir, public_identifier)
    return profile_cache_path(cache_dir, public_identifier)


def read_usable_cached_profile(cache_path: Path | None) -> dict[str, Any] | None:
    if not cache_path or not cache_path.exists():
        return None
    cached = read_json(cache_path, None)
    if not isinstance(cached, dict):
        return None
    normalized = cached.get("normalized_profile")
    raw = cached.get("raw_response")
    if isinstance(normalized, dict) and normalized.get("success") is True and isinstance(raw, dict):
        return cached
    normalized = normalize_linkedin_profile(cached)
    if normalized.get("success") is True:
        return {
            "fetched_at": cached.get("fetched_at") or cached.get("last_checked_at") or "",
            "last_checked_at": cached.get("last_checked_at") or cached.get("fetched_at") or "",
            "public_identifier": cached.get("public_identifier") or normalized.get("public_identifier") or cache_path.stem,
            "linkedin_url": cached.get("linkedin_url") or normalized.get("linkedin_url") or "",
            "raw_response": cached,
            "normalized_profile": normalized,
        }
    return None


def recent_cached_failure(cache_path: Path | None, retry_hours: float) -> dict[str, Any] | None:
    if not cache_path or not cache_path.exists() or retry_hours <= 0:
        return None
    cached = read_json(cache_path, None)
    if not isinstance(cached, dict):
        return None
    normalized = cached.get("normalized_profile")
    if not isinstance(normalized, dict) or normalized.get("success") is not False:
        return None
    checked_at = parse_iso(str(cached.get("last_checked_at") or cached.get("fetched_at") or ""))
    if checked_at is None:
        return None
    retry_after = checked_at + timedelta(hours=retry_hours)
    if datetime.now(timezone.utc) >= retry_after:
        return None
    result = dict(cached)
    result["retry_after"] = retry_after.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return result


def rapidapi_profile(
    public_identifier: str,
    linkedin_url: str,
    api_key: str,
    *,
    cache_dir: Path | str | None = None,
    refresh_cache: bool = False,
    wait_for_attempt: Callable[[], None] | None = None,
) -> dict[str, Any]:
    cache_path = profile_cache_path(cache_dir, public_identifier)
    if not refresh_cache:
        cached = read_usable_cached_profile(cache_path)
        if cached:
            return {
                "status_code": 200,
                "data": cached.get("raw_response"),
                "error": "",
                "from_cache": True,
                "normalized_profile": cached.get("normalized_profile"),
            }

    attempts = max(1, DEFAULT_RAPIDAPI_RETRY_ATTEMPTS)
    status = 0
    data: dict[str, Any] | None = None
    error = ""
    for attempt in range(1, attempts + 1):
        if wait_for_attempt:
            wait_for_attempt()
        status, data, error = http_json(
            "GET",
            f"{RAPIDAPI_BASE_URL}/get-profile-data-by-url",
            headers={"x-rapidapi-host": "professional-network-data.p.rapidapi.com", "x-rapidapi-key": api_key},
            params={"url": linkedin_url or f"https://www.linkedin.com/in/{public_identifier}"},
            timeout=90,
        )
        if status not in {0, 429, 500, 502, 503, 504} or attempt == attempts:
            break
        sleep_for = DEFAULT_RAPIDAPI_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
        time.sleep(sleep_for)
    normalized = normalize_linkedin_profile(data if isinstance(data, dict) else {})
    if cache_path and status == 200 and isinstance(data, dict) and normalized.get("success") is True:
        write_json(cache_path, {
            "fetched_at": now_iso(),
            "last_checked_at": now_iso(),
            "public_identifier": public_identifier,
            "linkedin_url": linkedin_url,
            "raw_response": data,
            "normalized_profile": normalized,
            "attempts": attempt,
        })
    elif cache_path:
        checked_at = now_iso()
        write_json(cache_path, {
            "fetched_at": checked_at,
            "last_checked_at": checked_at,
            "public_identifier": public_identifier,
            "linkedin_url": linkedin_url,
            "raw_response": data if isinstance(data, dict) else {},
            "normalized_profile": normalized,
            "status_code": status,
            "error": error or normalized.get("error") or "",
            "attempts": attempt,
        })
    return {"status_code": status, "data": data, "error": error, "from_cache": False, "normalized_profile": normalized, "attempts": attempt}


def normalize_rapidapi(
    data: dict[str, Any] | None,
    public_identifier: str,
    linkedin_url: str,
    company_lookup: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    profile = normalize_linkedin_profile(data)
    if profile.get("success") is not True:
        return {}

    experiences = [rapidapi_experience_to_powerpacks(exp, company_lookup) for exp in profile.get("experiences", []) if isinstance(exp, dict)]
    title, company, _legacy = current_position(experiences)
    profile_public_id = profile.get("public_identifier") or public_identifier
    profile_url = profile.get("linkedin_url") or linkedin_url or (f"https://www.linkedin.com/in/{profile_public_id}" if profile_public_id else "")
    return {
        "public_identifier": profile_public_id,
        "linkedin_url": normalize_linkedin_url(profile_url),
        "first_name": profile.get("first_name") or "",
        "last_name": profile.get("last_name") or "",
        "full_name": profile.get("full_name") or "",
        "headline": profile.get("headline") or "",
        "summary": profile.get("summary") or "",
        "city": profile.get("city") or "",
        "state": profile.get("state") or "",
        "country": profile.get("country") or "",
        "location_raw": profile.get("location_str") or "",
        "profile_picture_url": profile.get("profile_pic_url") or "",
        "work_experiences": experiences,
        "education": profile.get("education") if isinstance(profile.get("education"), list) else [],
        "current_title": title,
        "current_company": company,
    }


def row_has_profile_gaps(row: dict[str, str]) -> bool:
    if not row.get("full_name") and not (row.get("first_name") and row.get("last_name")):
        return True
    if not row.get("headline"):
        return True
    if not row.get("current_company") and not row.get("current_title"):
        return True
    if profile_richness(row.get("work_experiences"), row.get("education")) == 0:
        return True
    return False


def route_row(row: dict[str, str], force: bool = False) -> tuple[str, str]:
    linkedin_url = normalize_linkedin_url(row.get("linkedin_url") or "")
    public_identifier = row.get("public_identifier") or extract_public_identifier(linkedin_url)
    if linkedin_url or public_identifier:
        if force or row_has_profile_gaps(row):
            return "linkedin_provider", "linkedin identifier present and profile has gaps"
        return "skip_complete", "linkedin identifier present and profile appears complete"
    if row.get("primary_email") or row.get("primary_phone") or row.get("twitter_handle") or row.get("full_name"):
        return "needs_resolution", "no linkedin identifier; needs resolution/research before provider enrichment"
    return "skip_no_identifier", "no usable identifier"


def merge_provider_profile(base: dict[str, Any], rapid: dict[str, Any], rapid_raw: dict[str, Any] | None) -> dict[str, Any]:
    base = normalize_people_row(base)
    public_identifier = base.get("public_identifier") or rapid.get("public_identifier") or extract_public_identifier(base.get("linkedin_url") or rapid.get("linkedin_url") or "")
    row: dict[str, Any] = dict(base)
    row["id"] = row.get("id") or generate_person_id(public_identifier, row.get("full_name") or row.get("primary_email") or row.get("primary_phone") or "")
    row["public_identifier"] = public_identifier

    if rapid:
        for key in [
            "linkedin_url", "first_name", "last_name", "full_name", "headline", "summary",
            "city", "state", "country", "location_raw", "profile_picture_url",
            "current_title", "current_company",
        ]:
            value = rapid.get(key)
            if value not in (None, ""):
                row[key] = value
        row["linkedin_url"] = normalize_linkedin_url(row.get("linkedin_url") or (f"https://www.linkedin.com/in/{public_identifier}" if public_identifier else ""))
        if not row.get("full_name"):
            row["full_name"] = f"{row.get('first_name','')} {row.get('last_name','')}".strip()
        row["work_experiences"] = json.dumps(rapid.get("work_experiences") or parse_jsonish(row.get("work_experiences"), []))
        row["education"] = json.dumps(rapid.get("education") or parse_jsonish(row.get("education"), []))
        row["enriched_at"] = now_iso()
        row["enrichment_provider"] = "rapidapi"
        if rapid_raw:
            row["rapidapi_response"] = json.dumps(rapid_raw)
    else:
        row["linkedin_url"] = normalize_linkedin_url(row.get("linkedin_url") or (f"https://www.linkedin.com/in/{public_identifier}" if public_identifier else ""))
        row["enrichment_provider"] = row.get("enrichment_provider") or "existing_only"
    return normalize_people_row(row)


def cached_profile_from_row(row: dict[str, Any], public_identifier: str, linkedin_url: str) -> dict[str, Any] | None:
    for col in ("rapidapi_response_enriched", "rapidapi_response"):
        parsed = parse_jsonish(row.get(col), None)
        if isinstance(parsed, dict) and normalize_linkedin_profile(parsed).get("success") is True:
            return parsed
    return None


def confirmed_people_row(row: dict[str, Any]) -> bool:
    linkedin_url = normalize_linkedin_url(str(row.get("linkedin_url") or ""))
    public_identifier = str(row.get("public_identifier") or extract_public_identifier(linkedin_url) or "").strip()
    if not linkedin_url or not public_identifier:
        return False
    raw = parse_jsonish(row.get("rapidapi_response"), None)
    return isinstance(raw, dict) and normalize_linkedin_profile(raw).get("success") is True


def classify_rapidapi_cache_status(
    row: dict[str, str],
    profile_cache_dir: Path,
    refresh_cache: bool,
    retry_hours: float,
    cache_index: set[str] | None = None,
) -> tuple[str, str, Path | None, dict[str, Any] | None]:
    public_identifier = row.get("public_identifier") or extract_public_identifier(row.get("linkedin_url") or "")
    cache_path = indexed_profile_cache_path(profile_cache_dir, public_identifier, cache_index)
    if refresh_cache:
        return "miss", "refresh requested", cache_path, None
    if cached_profile_from_row(row, public_identifier, row.get("linkedin_url") or "") is not None:
        return "hit", "input rapidapi_response", cache_path, None
    cached_file_exists = bool(cache_path and cache_path.exists())
    if cache_index is not None:
        cached_file_exists = any(slug in cache_index for slug in cache_slug_candidates(public_identifier))
    if cached_file_exists and read_usable_cached_profile(cache_path):
        return "hit", "profile cache", cache_path, None
    recent_failure = recent_cached_failure(cache_path, retry_hours)
    if recent_failure:
        return "recent_failure", "recent provider failure", cache_path, recent_failure
    if cached_file_exists:
        return "miss", "cache entry unusable", cache_path, None
    return "miss", "no usable cache", cache_path, None


def count_rapidapi_cache_misses(cache_misses_csv: Path) -> int:
    if not cache_misses_csv.exists():
        return 0
    return len(CsvIO.read_dict_rows(cache_misses_csv))


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
        if paid > 0 and not rapidapi_key():
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
        rapid_key = rapidapi_key()
        # Defensive: run() gates on this before calling us, but keep the guard so
        # a direct caller cannot silently spend against a missing key.
        if paid_call_count > 0 and not rapid_key:
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
                cached_payload = cached_profile_from_row(row, public_identifier, linkedin_url)
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
                rapid = rapidapi_profile(
                    public_identifier,
                    linkedin_url,
                    rapid_key,
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


def command_run(args: argparse.Namespace) -> int:
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
    manifest = EnrichPeople(cfg).run()
    emit(manifest_emit_payload(manifest))
    return exit_code_for_status(manifest.status)


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
    run.set_defaults(func=command_run)

    status = sub.add_parser("status")
    status.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    status.add_argument("--artifact-dir", default="", help=argparse.SUPPRESS)
    status.set_defaults(func=command_status)

    keys = sub.add_parser("check-keys")
    keys.set_defaults(func=command_check_keys)
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
