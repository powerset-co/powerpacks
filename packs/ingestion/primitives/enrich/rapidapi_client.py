#!/usr/bin/env python3
"""RapidAPI LinkedIn client: keys, HTTP, retry/backoff, cache-aware fetch.

`RapidApiClient` is the one home for talking to the professional-network-data
RapidAPI host. An instance holds the API key + retry policy and exposes
`fetch_profile` (cache-aware profile fetch). Key resolution reads
RAPIDAPI_LINKEDIN_KEY then RAPIDAPI_KEY from the environment, seeded from the
repo `.env` at import time without overriding the shell.

- `RapidApiClient(api_key=None, *, retry_attempts=None, retry_backoff_seconds=None)`
  — omit `api_key` to resolve it from the env; the retry knobs default to the
  module `DEFAULT_RAPIDAPI_*` constants.
- `RapidApiClient.resolve_key()` — the configured key, preferring
  RAPIDAPI_LINKEDIN_KEY.
- `RapidApiClient.http_json(...)` — one JSON-over-HTTP call; returns
  (status, payload, error-text).
- `client.fetch_profile(public_identifier, linkedin_url, *, cache_dir=None,
  refresh_cache=False, wait_for_attempt=None)` — serves a usable cache entry
  unless `refresh_cache`, otherwise fetches with exponential backoff on
  429/5xx/network errors. A success is always cached. A failure is cached ONLY
  when `is_permanent_failure` says so (404/410, or an HTTP 200 the provider
  marked `success: false`) AND no usable successful entry is already on disk.
  Cache format and readers live in `profile_cache.py`.
- `RETRYABLE_STATUS_CODES` / `PERMANENT_FAILURE_STATUS_CODES` /
  `is_permanent_failure` — the retry set and the cacheable-failure set.
- `DEFAULT_RAPIDAPI_*` — env-tunable throughput/retry knobs (workers, RPM,
  failure-retry TTL, retry attempts/backoff), kept module-level so config-only
  consumers (models, network_import, run_linkedin) import them without the
  client.
- `rapidapi_key()` / `rapidapi_profile(...)` — thin module-level convenience
  wrappers over the class (resolve_key / a one-shot client's fetch_profile), the
  `requests.get`-vs-`requests.Session` split. Simple one-call sites use these;
  the enrichment orchestrator holds a reused `RapidApiClient` directly.

Changelog:
  2026-07-24: failure caching narrowed to PERMANENT failures only. Every
    non-success path used to write an identical failure record, so a 429, a
    timeout and a genuine 404 all earned the same 24h retry suppression and a
    rate-limit storm silently erased contacts for a day. A failure write also
    now refuses to overwrite an existing usable (already paid for) profile,
    which `--refresh-cache` previously allowed.
  2026-07-23 (audit oo-client): the module internals became a RapidApiClient
    class (rapidapi_key -> resolve_key, rapidapi_profile -> fetch_profile,
    RAPIDAPI_BASE_URL -> BASE_URL, load_dotenv/http_json now staticmethods). The
    module keeps thin rapidapi_key/rapidapi_profile convenience wrappers that
    delegate to the class, so one-shot call sites are unchanged. DEFAULT_RAPIDAPI_*
    stay module constants. Behavior and the cache read/write contract are
    unchanged.
  2026-07-23 (audit decomposition): split out of enrich_people.py verbatim
    (constants, load_dotenv + .env seeding, http_json, rapidapi_key,
    rapidapi_profile).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import now_iso, write_json  # noqa: E402
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path, read_usable_cached_profile  # noqa: E402
from packs.ingestion.schemas.linkedin_profile_normalizer import normalize_linkedin_profile  # noqa: E402

DEFAULT_RAPIDAPI_MAX_WORKERS = int(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_MAX_WORKERS", "64"))
DEFAULT_RAPIDAPI_MAX_RPM = float(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_MAX_RPM", "300"))
DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS = float(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_FAILURE_RETRY_HOURS", "24"))
DEFAULT_RAPIDAPI_RETRY_ATTEMPTS = int(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_RETRY_ATTEMPTS", "3"))
DEFAULT_RAPIDAPI_RETRY_BACKOFF_SECONDS = float(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_RETRY_BACKOFF_SECONDS", "1.0"))

# HTTP statuses worth another attempt within a single fetch. 0 is our own
# "no HTTP answer" code from `http_json` (network error, timeout, or a body that
# failed to parse as JSON).
RETRYABLE_STATUS_CODES = frozenset({0, 429, 500, 502, 503, 504})
# The ONLY statuses whose failure is PERMANENT for a profile: the profile is gone
# or withheld, so the next fetch buys the same answer. Everything else — 0, 429,
# 5xx — is transient and must NOT be written to the failure cache: a cached
# failure suppresses retries for DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS, which
# turns a rate-limit storm into a day of unenrichable contacts. A provider
# `success: false` body on an HTTP 200 counts as permanent too (see
# `is_permanent_failure`) — that is the provider saying the profile is not there.
PERMANENT_FAILURE_STATUS_CODES = frozenset({404, 410})


class RapidApiClient:
    """Cache-aware RapidAPI LinkedIn client: holds the API key + retry policy and
    fetches profiles through `fetch_profile`. Stateless per call beyond those
    immutable attributes, so one instance is safe to share across a thread pool."""

    BASE_URL = "https://professional-network-data.p.rapidapi.com"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        retry_attempts: int | None = None,
        retry_backoff_seconds: float | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else self.resolve_key()
        self.retry_attempts = DEFAULT_RAPIDAPI_RETRY_ATTEMPTS if retry_attempts is None else retry_attempts
        self.retry_backoff_seconds = DEFAULT_RAPIDAPI_RETRY_BACKOFF_SECONDS if retry_backoff_seconds is None else retry_backoff_seconds

    @staticmethod
    def resolve_key() -> str:
        return os.getenv("RAPIDAPI_LINKEDIN_KEY", "").strip() or os.getenv("RAPIDAPI_KEY", "").strip()

    @staticmethod
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

    @staticmethod
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

    def fetch_profile(
        self,
        public_identifier: str,
        linkedin_url: str,
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

        attempts = max(1, self.retry_attempts)
        status = 0
        data: dict[str, Any] | None = None
        error = ""
        for attempt in range(1, attempts + 1):
            if wait_for_attempt:
                wait_for_attempt()
            status, data, error = self.http_json(
                "GET",
                f"{self.BASE_URL}/get-profile-data-by-url",
                headers={"x-rapidapi-host": "professional-network-data.p.rapidapi.com", "x-rapidapi-key": self.api_key},
                params={"url": linkedin_url or f"https://www.linkedin.com/in/{public_identifier}"},
                timeout=90,
            )
            if status not in RETRYABLE_STATUS_CODES or attempt == attempts:
                break
            sleep_for = self.retry_backoff_seconds * (2 ** (attempt - 1))
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
        elif cache_path and self.is_permanent_failure(status, normalized) and not read_usable_cached_profile(cache_path):
            # Only permanent failures are cached, and never over an entry we
            # already paid for: `refresh_cache` skips the READ above, so without
            # this guard one transient error during a refresh would overwrite a
            # good profile with a failure record and bill us to get it back.
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

    @staticmethod
    def is_permanent_failure(status: int, normalized: dict[str, Any]) -> bool:
        """True when a non-success result will not change on a later retry, i.e.
        it is safe to remember as a cached failure. That is HTTP 404/410, or an
        HTTP 200 whose body the provider marked `success: false` (no such
        profile). Transient statuses — 0 (network/timeout/unparseable body), 429,
        and 5xx — are never permanent, so they leave the cache untouched and the
        next run retries instead of silently dropping the person."""
        if status in PERMANENT_FAILURE_STATUS_CODES:
            return True
        return status == 200 and normalized.get("success") is False


def rapidapi_key() -> str:
    """Convenience wrapper: the configured key (prefers RAPIDAPI_LINKEDIN_KEY)."""
    return RapidApiClient.resolve_key()


def rapidapi_profile(
    public_identifier: str,
    linkedin_url: str,
    api_key: str,
    *,
    cache_dir: Path | str | None = None,
    refresh_cache: bool = False,
    wait_for_attempt: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Convenience wrapper: one cache-aware fetch through a one-shot client.

    For simple one-call sites; the enrichment orchestrator instead holds a reused
    `RapidApiClient` across its thread pool."""
    return RapidApiClient(api_key).fetch_profile(
        public_identifier,
        linkedin_url,
        cache_dir=cache_dir,
        refresh_cache=refresh_cache,
        wait_for_attempt=wait_for_attempt,
    )


# Seed RAPIDAPI_* from the repo .env at import (without overriding the shell), so
# `resolve_key()` finds keys placed only in .env.
RapidApiClient.load_dotenv(Path(__file__).resolve().parents[4] / ".env", {"RAPIDAPI_LINKEDIN_KEY", "RAPIDAPI_KEY"})
