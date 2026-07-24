#!/usr/bin/env python3
"""RapidAPI LinkedIn client: keys, HTTP, retry/backoff, cache-aware fetch.

The one home for talking to the professional-network-data RapidAPI host. Reads
RAPIDAPI_LINKEDIN_KEY / RAPIDAPI_KEY (in that order) from the environment,
seeding them from the repo `.env` at import time without overriding the shell.

- `DEFAULT_RAPIDAPI_*` — env-tunable throughput/retry knobs (workers, RPM,
  failure-retry TTL, retry attempts/backoff).
- `http_json` — one JSON-over-HTTP call; returns (status, payload, error-text).
- `rapidapi_key` — the configured key, preferring RAPIDAPI_LINKEDIN_KEY.
- `rapidapi_profile` — cache-aware profile fetch: serves a usable cache entry
  unless `refresh_cache`, otherwise fetches with exponential backoff on
  429/5xx/network errors and writes the (success OR failure) result back to the
  profile cache. Cache format and readers live in `profile_cache.py`.

Changelog:
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

RAPIDAPI_BASE_URL = "https://professional-network-data.p.rapidapi.com"
DEFAULT_RAPIDAPI_MAX_WORKERS = int(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_MAX_WORKERS", "64"))
DEFAULT_RAPIDAPI_MAX_RPM = float(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_MAX_RPM", "300"))
DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS = float(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_FAILURE_RETRY_HOURS", "24"))
DEFAULT_RAPIDAPI_RETRY_ATTEMPTS = int(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_RETRY_ATTEMPTS", "3"))
DEFAULT_RAPIDAPI_RETRY_BACKOFF_SECONDS = float(os.environ.get("POWERPACKS_RAPIDAPI_LINKEDIN_RETRY_BACKOFF_SECONDS", "1.0"))


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
