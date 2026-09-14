#!/usr/bin/env python3
"""Powerset-gateway LinkedIn client: keys, HTTP, retry/backoff, and one profile door.

POLICY (one line): the client's single behavior is cache-first,
fetch-on-miss-or-unusable — callers make ONE `get_profile` call and branch on
its definitive state; render/offline surfaces that must never touch the network
read the cache files directly instead of calling the client.

`RapidApiClient` is the one home for talking to professional-network-data through
the Powerset gateway. An instance holds the API key + retry policy. Key
resolution reads POWERSET_API_KEY from the environment, seeded from the repo
`.env` at import time without overriding the shell.

- `client.get_profile(public_identifier, linkedin_url, *, cache_dir=None,
  fresh=False, wait_for_attempt=None)` — THE profile door. Resolves cache vs
  fetch internally and returns a definitive state:
    * `content` — a usable profile with decidable content (experiences or
      education; `profile_has_content`).
    * `empty`   — a fetch HAPPENED (now or recorded) and the profile is a
      shell or gone. Recorded with its check timestamp, so repeated calls
      answer `empty` without re-billing — at most one paid re-check per
      stale-empty entry per process run.
    * `error`   — network/auth/keyless with nothing recorded: UNKNOWN, not a
      verdict. Callers must never treat it as one.
  `fresh=True` is a freshness demand, not a cache bypass: deliver truth no
  older than this run (a cached `content`/stale `empty` is re-fetched once;
  repeat calls in the same run serve the recorded answer). A fresh fetch that
  fails transiently falls back to the recorded state rather than ERROR.
- `RapidApiClient.resolve_key()` — the configured Powerset gateway key.
- `RapidApiClient.http_json(...)` — one JSON-over-HTTP call; returns
  (status, payload, error-text).
- Cache writes: a success is always cached. A failure is cached ONLY when
  `is_permanent_failure` says so (404/410, or an HTTP 200 the provider marked
  `success: false`). A permanent failure over an entry we already paid for
  keeps the paid body and only bumps `last_checked_at` — recorded evidence is
  never destroyed. Cache format and readers live in `profile_cache.py`.
- `RETRYABLE_STATUS_CODES` / `PERMANENT_FAILURE_STATUS_CODES` /
  `is_permanent_failure` — the retry set and the cacheable-failure set.
- `DEFAULT_RAPIDAPI_*` — env-tunable throughput/retry knobs (workers, RPM,
  failure-retry TTL, retry attempts/backoff), kept module-level so config-only
  consumers (models, network_import, run_linkedin) import them without the
  client.
- `rapidapi_key()` / `rapidapi_profile(...)` — thin module-level convenience
  wrappers over the class (resolve_key / a one-shot client's get_profile).
  Simple one-call sites use these; orchestrators hold a reused
  `RapidApiClient` directly.
- `hydrate_profiles(...)` — bulk ensure-usable-profiles over `get_profile`.

Changelog:
  2026-08-05 (one door, three states): `fetch_profile` (public, with a
    caller-facing `refresh_cache` flag) became the internal `_fetch_fresh`;
    the public door is `get_profile` returning `content|empty|error` with the
    cache-vs-fetch resolution — including "cached entry is unusable → re-check
    it once" — INSIDE the client. Empty answers are recorded (`last_checked_at`)
    and memoized per process so repeats never re-bill. `refresh_cache` was
    removed from every caller surface (enrich CLI, LinkedIn import CLI, modal
    runner); `fresh=` is the principled replacement (accuracy demand, not a
    cache bypass).
  2026-07-24: failure caching narrowed to PERMANENT failures only. Every
    non-success path used to write an identical failure record, so a 429, a
    timeout and a genuine 404 all earned the same 24h retry suppression and a
    rate-limit storm silently erased contacts for a day.
  2026-07-23 (audit oo-client): the module internals became a RapidApiClient
    class. DEFAULT_RAPIDAPI_* stay module constants.
  2026-07-23 (audit decomposition): split out of enrich_people.py verbatim.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import now_iso, read_json, write_json  # noqa: E402
from packs.ingestion.primitives.enrich.profile_cache import (  # noqa: E402
    parse_iso,
    profile_cache_path,
    profile_has_content,
    read_usable_cached_profile,
)
from packs.ingestion.schemas.linkedin_profile_normalizer import normalize_linkedin_profile  # noqa: E402

# The three definitive answers of `get_profile` (see the module docstring).
PROFILE_CONTENT = "content"
PROFILE_EMPTY = "empty"
PROFILE_ERROR = "error"

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
    answers profile questions through `get_profile`. Stateless per call beyond
    those immutable attributes, so one instance is safe to share across a thread
    pool."""

    BASE_URL = "https://proxy.powerset.dev/vendor/professional-network-data"

    # cache-entry key -> definitive state answered by a FETCH this process
    # run. Once an entry has a definitive answer, repeat calls (fresh or not,
    # any instance) serve the recorded state instead of re-billing — at most
    # one paid check per entry per run. Class-level on purpose: the
    # convenience wrapper builds one-shot clients, and the no-rebill promise
    # is per process, not per instance. Keyed by the resolved cache path so
    # distinct stores (and tests) never share an answer.
    _definitive_this_run: dict[str, str] = {}

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
        return os.getenv("POWERSET_API_KEY", "").strip()

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

    def get_profile(
        self,
        public_identifier: str,
        linkedin_url: str,
        *,
        cache_dir: Path | str | None = None,
        fresh: bool = False,
        wait_for_attempt: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """THE profile door: one call, one definitive state (see module docstring).

        Returns {"state": content|empty|error, "normalized_profile", "data",
        "from_cache", "fetched", "status_code", "detail", "attempts"}.
        `fresh=True` demands truth no older than this run; it never suppresses
        fetching and repeat calls in the same run never re-bill."""
        pub_key = (public_identifier or "").strip().lower()
        cache_path = profile_cache_path(cache_dir, public_identifier)
        memo_key = str(cache_path) if cache_path else pub_key
        cached = read_usable_cached_profile(cache_path)
        record_exists = bool(cache_path and cache_path.exists())
        answered = self._definitive_this_run.get(memo_key, "")

        def from_record(state: str, detail: str = "") -> dict[str, Any]:
            return {
                "state": state,
                "normalized_profile": (cached or {}).get("normalized_profile") or {},
                "data": (cached or {}).get("raw_response"),
                "from_cache": True, "fetched": False,
                "status_code": 200 if cached else 0,
                "detail": detail, "attempts": 0,
            }

        # A pub already answered by a fetch this run keeps that answer — the
        # no-rebill promise ("empty" repeats especially) beats a fresh demand.
        if answered:
            return from_record(answered, "answered by a fetch earlier this run")
        if cached and profile_has_content(cached) and not fresh:
            return from_record(PROFILE_CONTENT)
        if record_exists and not (cached and profile_has_content(cached)) \
                and not fresh and self._empty_recently_checked(cache_path):
            return from_record(PROFILE_EMPTY, "recorded empty inside the retry TTL")
        if not self.api_key:
            # Keyless installs get the recorded truth (possibly stale) — an
            # unknown pub is ERROR, never a verdict.
            if cached and profile_has_content(cached):
                return from_record(PROFILE_CONTENT, "no Powerset API key; serving cached profile")
            if record_exists:
                return from_record(PROFILE_EMPTY, "no Powerset API key; serving recorded empty state")
            return {"state": PROFILE_ERROR, "normalized_profile": {}, "data": None,
                    "from_cache": False, "fetched": False, "status_code": 0,
                    "detail": "POWERSET_API_KEY is not set", "attempts": 0}

        result = self._fetch_fresh(public_identifier, linkedin_url,
                                   cache_path=cache_path, fresh=fresh,
                                   wait_for_attempt=wait_for_attempt)
        normalized = result.get("normalized_profile") or {}
        base = {"data": result.get("data"), "from_cache": False, "fetched": True,
                "status_code": int(result.get("status_code") or 0),
                "attempts": int(result.get("attempts") or 1)}
        if normalized.get("success") is True and profile_has_content({"normalized_profile": normalized}):
            self._definitive_this_run[memo_key] = PROFILE_CONTENT
            return {"state": PROFILE_CONTENT, "normalized_profile": normalized,
                    "detail": "", **base}
        if normalized.get("success") is True or self.is_permanent_failure(base["status_code"], normalized):
            # A definitive empty: the profile is a shell or gone. The fetch
            # writes recorded it (shells via the success write; permanent
            # failures via the failure write / last-checked bump).
            self._definitive_this_run[memo_key] = PROFILE_EMPTY
            return {"state": PROFILE_EMPTY, "normalized_profile": normalized,
                    "detail": result.get("error") or normalized.get("error") or "profile has no content",
                    **base}
        # Transient failure: not a verdict. Fall back to the recorded truth.
        detail = f"fetch failed ({result.get('error') or base['status_code']})"
        if cached and profile_has_content(cached):
            return from_record(PROFILE_CONTENT, f"{detail}; serving cached profile")
        if record_exists:
            return from_record(PROFILE_EMPTY, f"{detail}; serving recorded empty state")
        return {"state": PROFILE_ERROR, "normalized_profile": normalized, "data": result.get("data"),
                "from_cache": False, "fetched": True, "detail": detail, **{k: base[k] for k in ("status_code", "attempts")}}

    @staticmethod
    def _empty_recently_checked(cache_path: Path | None) -> bool:
        """True when a no-content cache entry was (re)checked inside the
        failure-retry TTL — recent enough to serve as EMPTY without re-billing."""
        if not cache_path or not cache_path.exists():
            return False
        record = read_json(cache_path, None)
        if not isinstance(record, dict):
            return False
        checked = parse_iso(str(record.get("last_checked_at") or record.get("fetched_at") or ""))
        if checked is None:
            return False
        return datetime.now(timezone.utc) < checked + timedelta(hours=DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS)

    def _fetch_fresh(
        self,
        public_identifier: str,
        linkedin_url: str,
        *,
        cache_path: Path | None,
        fresh: bool,
        wait_for_attempt: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """INTERNAL: one network fetch (with retry/backoff) plus the cache
        writes. Never reads the cache — `get_profile` owns that resolution."""
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
                headers={
                    "x-powerset-key": self.api_key,
                    "X-Freshness": "live" if fresh else "31536000",
                },
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
        elif cache_path and self.is_permanent_failure(status, normalized):
            existing_usable = read_usable_cached_profile(cache_path)
            checked_at = now_iso()
            if existing_usable:
                # Never destroy an entry we already paid for: keep the body,
                # bump last_checked_at so the empty/gone verdict is recorded.
                record = read_json(cache_path, None)
                if isinstance(record, dict):
                    record["last_checked_at"] = checked_at
                    write_json(cache_path, record)
            else:
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


def hydrate_profiles(items: "list[tuple[str, str]]", cache_dir: Path | str | None,
                     *, max_workers: int = 8) -> dict[str, int]:
    """Prefer cache, always retrieve: ensure a usable profile exists for each
    (public_identifier, linkedin_url) pair, fetching the misses.

    The ONE home for that policy — both judges that need a profile before
    judging call this (the attached-link judge in reconcile_linkedin and the
    retarget-proposal judge in reconcile_deep_research). Cache hits cost
    nothing; a miss is one RapidAPI credit and permanent failures are cached,
    so re-runs never re-bill a dead URL. A keyless install is not an error: it
    returns `skipped_no_key` and leaves the caller on whatever it already had.
    """
    items = [(pub, url) for pub, url in items if pub]
    counts = {"wanted": len(items), "ok": 0, "failed": 0, "skipped_no_key": 0}
    if not items:
        return counts
    if not RapidApiClient.resolve_key():
        counts["skipped_no_key"] = len(items)
        return counts
    client = RapidApiClient()

    def one(item: "tuple[str, str]") -> bool:
        pub, url = item
        return client.get_profile(pub, url, cache_dir=cache_dir)["state"] == PROFILE_CONTENT

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(items)))) as pool:
        for ok in pool.map(one, items):
            counts["ok" if ok else "failed"] += 1
    return counts


def rapidapi_key() -> str:
    """Convenience wrapper: the configured Powerset gateway key."""
    return RapidApiClient.resolve_key()


def rapidapi_profile(
    public_identifier: str,
    linkedin_url: str,
    *,
    cache_dir: Path | str | None = None,
    fresh: bool = False,
    wait_for_attempt: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Convenience wrapper: one `get_profile` call through a one-shot client.

    For simple one-call sites; orchestrators instead hold a reused
    `RapidApiClient` across their thread pool."""
    return RapidApiClient().get_profile(
        public_identifier,
        linkedin_url,
        cache_dir=cache_dir,
        fresh=fresh,
        wait_for_attempt=wait_for_attempt,
    )


# Seed POWERSET_API_KEY from the repo .env at import (without overriding the
# shell), so `resolve_key()` finds keys placed only in .env.
RapidApiClient.load_dotenv(Path(__file__).resolve().parents[4] / ".env", {"POWERSET_API_KEY"})
