"""Fetch company details from RapidAPI LinkedIn endpoint.

Results are cached to disk under a configurable directory so repeated
pipeline runs don't re-fetch.

Usage:
    from packs.indexing.primitives.enrich_companies_checkpointed.rapidapi_company import fetch_company_details

    details = fetch_company_details("1441", api_key="...")
"""
from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
import random
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from packs.shared.rate_limiter import StartRateLimiter


DEFAULT_HOST = "professional-network-data.p.rapidapi.com"
DEFAULT_TIMEOUT = 30
DEFAULT_CACHE_DIR = ".powerpacks/rapidapi-company-cache"
DEFAULT_CACHE_READ_WORKERS = 32

# RapidAPI company-details rate limit. The 50-way fan-out below would otherwise
# burst and trip RapidAPI's 429s (each one costs a retry + backoff), so we pace
# request *starts* to a steady rpm via the shared StartRateLimiter — the same
# pacer profile enrichment uses to sustain a clean 300 rpm.
# Override with POWERPACKS_RAPIDAPI_COMPANY_MAX_RPM; 0 disables pacing.
DEFAULT_COMPANY_MAX_RPM = float(os.getenv("POWERPACKS_RAPIDAPI_COMPANY_MAX_RPM", "300"))

# Shared by the id- and slug-based fetches so their combined RapidAPI rate stays
# within the limit.
_RATE_LIMITER = StartRateLimiter(DEFAULT_COMPANY_MAX_RPM)


def _api_key() -> str:
    return (
        os.getenv("RAPIDAPI_LINKEDIN_KEY", "").strip()
        or os.getenv("RAPIDAPI_KEY", "").strip()
    )


def _cache_path(company_id: str, cache_dir: str | Path | None = None) -> Path:
    d = Path(cache_dir or os.getenv("POWERPACKS_RAPIDAPI_COMPANY_CACHE", DEFAULT_CACHE_DIR))
    return d / f"{company_id}.json"


def _read_cache(company_id: str, cache_dir: str | Path | None = None) -> dict[str, Any] | None:
    p = _cache_path(company_id, cache_dir)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and not data.get("error"):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _read_cached_values(
    values: list[str],
    *,
    cache_key: Callable[[str], str],
    cache_dir: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Read independent file-cache entries concurrently, preserving input order."""
    unique_values = list(dict.fromkeys(values))
    if not unique_values:
        return {}

    def _read_one(value: str) -> tuple[str, dict[str, Any] | None]:
        return value, _read_cache(cache_key(value), cache_dir)

    workers = min(DEFAULT_CACHE_READ_WORKERS, len(unique_values))
    if workers == 1:
        pairs = map(_read_one, unique_values)
        return {value: cached for value, cached in pairs if cached is not None}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        pairs = pool.map(_read_one, unique_values)
        return {value: cached for value, cached in pairs if cached is not None}


def _write_cache(company_id: str, data: dict[str, Any], cache_dir: str | Path | None = None) -> None:
    p = _cache_path(company_id, cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def fetch_company_details(
    company_id: str,
    *,
    api_key: str | None = None,
    host: str = DEFAULT_HOST,
    timeout: int = DEFAULT_TIMEOUT,
    cache_dir: str | Path | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Fetch company details by RapidAPI LinkedIn company ID.

    Returns the parsed JSON response or {"error": "..."} on failure.
    Successful responses are cached to disk; error results are never cached.

    Transient failures (HTTP 429, HTTP 5xx, connection/timeout exceptions)
    are retried up to *max_attempts* times with exponential backoff plus
    jitter. For 429 responses with a numeric ``Retry-After`` header, that
    value (capped at 10s) is used instead of the computed backoff. Other
    4xx responses (e.g. 400, 404) are permanent and returned immediately.
    """
    cached = _read_cache(company_id, cache_dir)
    if cached is not None:
        return cached

    key = api_key or _api_key()
    if not key:
        return {"error": "no RAPIDAPI_LINKEDIN_KEY or RAPIDAPI_KEY set"}

    headers = {
        "x-rapidapi-key": key,
        "x-rapidapi-host": host,
        "Content-Type": "application/json",
    }
    last_error: dict[str, Any] = {"error": "no fetch attempts made"}
    for attempt in range(max_attempts):
        conn = http.client.HTTPSConnection(host, timeout=timeout)
        retry_after: float | None = None
        try:
            conn.request("GET", f"/get-company-details-by-id?id={company_id}", headers=headers)
            res = conn.getresponse()
            raw = res.read().decode("utf-8")
            if res.status == 200:
                result = json.loads(raw)
                _write_cache(company_id, result, cache_dir)
                return result
            last_error = {"error": f"HTTP {res.status}", "body": raw[:500]}
            if res.status != 429 and not 500 <= res.status < 600:
                # Other 4xx are permanent for this company ID; do not retry.
                return last_error
            if res.status == 429:
                header_val = res.getheader("Retry-After")
                if header_val is not None:
                    try:
                        retry_after = min(float(header_val), 10.0)
                    except (TypeError, ValueError):
                        retry_after = None
        except Exception as exc:
            last_error = {"error": str(exc)}
        finally:
            conn.close()
        if attempt < max_attempts - 1:
            if retry_after is not None:
                delay = retry_after
            else:
                delay = 1.0 * (2 ** attempt) + random.uniform(0, 0.5)
            time.sleep(delay)
    return last_error


def fetch_company_details_batch(
    company_ids: list[str],
    *,
    api_key: str | None = None,
    host: str = DEFAULT_HOST,
    timeout: int = DEFAULT_TIMEOUT,
    max_workers: int = 50,
    cache_dir: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch multiple company details concurrently.

    Cached results are returned instantly. Only cache misses hit the network.
    Concurrency is bounded by *max_workers* (default 50).

    Returns {company_id: response_dict}.
    """
    key = api_key or _api_key()
    if not key:
        return {cid: {"error": "no API key"} for cid in company_ids}

    # Serve cache hits immediately, collect misses for network fetch.
    unique_ids = list(dict.fromkeys(company_ids))
    results = _read_cached_values(unique_ids, cache_key=lambda cid: cid, cache_dir=cache_dir)
    need_fetch = [cid for cid in unique_ids if cid not in results]

    if not need_fetch:
        return results

    def _fetch_one(cid: str) -> tuple[str, dict[str, Any]]:
        _RATE_LIMITER.wait()  # pace the 50-way fan-out to a steady rpm
        return cid, fetch_company_details(cid, api_key=key, host=host, timeout=timeout, cache_dir=cache_dir)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, cid): cid for cid in need_fetch}
        for future in concurrent.futures.as_completed(futures):
            cid, resp = future.result()
            results[cid] = resp
    return results


def load_cached_company_details(
    company_ids: list[str],
    *,
    cache_dir: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Return cached company responses only; never hits the network.

    Cache misses are simply omitted from the result.
    """
    return _read_cached_values(company_ids, cache_key=lambda cid: cid, cache_dir=cache_dir)


def _slug_cache_key(slug: str) -> str:
    """Slug-namespaced cache key so slug entries never collide with numeric ids."""
    return f"slug__{slug.lower()}"


def fetch_company_details_by_slug(
    slug: str,
    *,
    api_key: str | None = None,
    host: str = DEFAULT_HOST,
    timeout: int = DEFAULT_TIMEOUT,
    cache_dir: str | Path | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Fetch company details by LinkedIn company slug (vanity username).

    For companies that carry a LinkedIn slug but no RapidAPI company id — the
    common long-tail case (companies LinkedIn knows but Harmonic doesn't). Same
    retry/backoff contract as fetch_company_details; cached under a slug-namespaced
    key so the (volume) cache dir persists it for reuse across runs and operators.
    """
    key_name = _slug_cache_key(slug)
    cached = _read_cache(key_name, cache_dir)
    if cached is not None:
        return cached

    key = api_key or _api_key()
    if not key:
        return {"error": "no RAPIDAPI_LINKEDIN_KEY or RAPIDAPI_KEY set"}

    headers = {
        "x-rapidapi-key": key,
        "x-rapidapi-host": host,
        "Content-Type": "application/json",
    }
    last_error: dict[str, Any] = {"error": "no fetch attempts made"}
    for attempt in range(max_attempts):
        conn = http.client.HTTPSConnection(host, timeout=timeout)
        retry_after: float | None = None
        try:
            conn.request("GET", f"/get-company-details?username={urllib.parse.quote(slug)}", headers=headers)
            res = conn.getresponse()
            raw = res.read().decode("utf-8")
            if res.status == 200:
                result = json.loads(raw)
                _write_cache(key_name, result, cache_dir)
                return result
            last_error = {"error": f"HTTP {res.status}", "body": raw[:500]}
            if res.status != 429 and not 500 <= res.status < 600:
                return last_error
            if res.status == 429:
                header_val = res.getheader("Retry-After")
                if header_val is not None:
                    try:
                        retry_after = min(float(header_val), 10.0)
                    except (TypeError, ValueError):
                        retry_after = None
        except Exception as exc:
            last_error = {"error": str(exc)}
        finally:
            conn.close()
        if attempt < max_attempts - 1:
            delay = retry_after if retry_after is not None else 1.0 * (2 ** attempt) + random.uniform(0, 0.5)
            time.sleep(delay)
    return last_error


def fetch_company_details_batch_by_slug(
    slugs: list[str],
    *,
    api_key: str | None = None,
    host: str = DEFAULT_HOST,
    timeout: int = DEFAULT_TIMEOUT,
    max_workers: int = 50,
    cache_dir: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch multiple companies by slug concurrently; cache hits served instantly."""
    key = api_key or _api_key()
    if not key:
        return {s: {"error": "no API key"} for s in slugs}

    unique_slugs = list(dict.fromkeys(slugs))
    results = _read_cached_values(unique_slugs, cache_key=_slug_cache_key, cache_dir=cache_dir)
    need_fetch = [slug for slug in unique_slugs if slug not in results]
    if not need_fetch:
        return results

    def _fetch_one(s: str) -> tuple[str, dict[str, Any]]:
        _RATE_LIMITER.wait()  # pace the 50-way fan-out to a steady rpm
        return s, fetch_company_details_by_slug(s, api_key=key, host=host, timeout=timeout, cache_dir=cache_dir)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, s): s for s in need_fetch}
        for future in concurrent.futures.as_completed(futures):
            s, resp = future.result()
            results[s] = resp
    return results


def load_cached_company_details_by_slug(
    slugs: list[str],
    *,
    cache_dir: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Return cached by-slug company responses only; never hits the network."""
    return _read_cached_values(slugs, cache_key=_slug_cache_key, cache_dir=cache_dir)


def extract_company_context(response: dict[str, Any]) -> dict[str, Any]:
    """Extract useful fields from RapidAPI company response for enrichment context."""
    if response.get("error"):
        return {}

    data = response.get("data", response)
    if not isinstance(data, dict):
        return {}

    # Extract fields that help with entity_type / sector_type classification.
    result: dict[str, Any] = {}

    for key in ("description", "tagline"):
        val = data.get(key)
        if val and isinstance(val, str) and val.strip():
            result["description"] = val.strip()
            break

    for key in ("staffCount", "staff_count", "employeeCount", "employee_count"):
        val = data.get(key)
        if val:
            try:
                result["headcount"] = int(val)
            except (TypeError, ValueError):
                pass
            break

    for key in ("founded", "foundedOn", "founded_on", "foundedYear", "founded_year"):
        val = data.get(key)
        if val:
            if isinstance(val, dict):
                val = val.get("year")
            try:
                result["founded_year"] = int(val)
            except (TypeError, ValueError):
                pass
            break

    for key in ("headquarter", "headquarters", "hq"):
        val = data.get(key)
        if isinstance(val, dict):
            city = val.get("city", "")
            state = val.get("geographicArea", val.get("state", ""))
            country = val.get("country", "")
            if city or country:
                result["city"] = city
                result["state"] = state
                result["country"] = country
            break

    for key in ("industries", "industry"):
        val = data.get(key)
        if isinstance(val, list) and val:
            result["industries"] = val
            break
        if isinstance(val, str) and val.strip():
            result["industries"] = [val.strip()]
            break

    for key in ("companyType", "company_type", "type"):
        val = data.get(key)
        if isinstance(val, dict):
            val = val.get("localizedName", val.get("name", ""))
        if val and isinstance(val, str):
            result["company_type_raw"] = val.strip()
            break

    for key in ("specialities", "specialties"):
        val = data.get(key)
        if isinstance(val, list) and val:
            result["specialties"] = val
            break

    website = data.get("companyPageUrl") or data.get("website") or data.get("url")
    if website and isinstance(website, str):
        result["website"] = website.strip()

    return result
