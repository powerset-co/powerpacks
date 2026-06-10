"""Fetch company details from RapidAPI LinkedIn endpoint.

Usage:
    from packs.indexing.primitives.enrich_companies_checkpointed.rapidapi_company import fetch_company_details

    details = fetch_company_details("1441", api_key="...")
"""
from __future__ import annotations

import http.client
import json
import os
import time
from typing import Any


DEFAULT_HOST = "professional-network-data.p.rapidapi.com"
DEFAULT_TIMEOUT = 30


def _api_key() -> str:
    return (
        os.getenv("RAPIDAPI_LINKEDIN_KEY", "").strip()
        or os.getenv("RAPIDAPI_KEY", "").strip()
    )


def fetch_company_details(
    company_id: str,
    *,
    api_key: str | None = None,
    host: str = DEFAULT_HOST,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Fetch company details by RapidAPI LinkedIn company ID.

    Returns the parsed JSON response or {"error": "..."} on failure.
    """
    key = api_key or _api_key()
    if not key:
        return {"error": "no RAPIDAPI_LINKEDIN_KEY or RAPIDAPI_KEY set"}

    conn = http.client.HTTPSConnection(host, timeout=timeout)
    headers = {
        "x-rapidapi-key": key,
        "x-rapidapi-host": host,
        "Content-Type": "application/json",
    }
    try:
        conn.request("GET", f"/get-company-details-by-id?id={company_id}", headers=headers)
        res = conn.getresponse()
        raw = res.read().decode("utf-8")
        if res.status != 200:
            return {"error": f"HTTP {res.status}", "body": raw[:500]}
        return json.loads(raw)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        conn.close()


def fetch_company_details_batch(
    company_ids: list[str],
    *,
    api_key: str | None = None,
    rpm_limit: float = 300,
    host: str = DEFAULT_HOST,
    timeout: int = DEFAULT_TIMEOUT,
    max_workers: int = 20,
) -> dict[str, dict[str, Any]]:
    """Fetch multiple company details concurrently with rate limiting.

    Returns {company_id: response_dict}.
    """
    import concurrent.futures
    import threading

    key = api_key or _api_key()
    if not key:
        return {cid: {"error": "no API key"} for cid in company_ids}

    min_interval = 60.0 / rpm_limit if rpm_limit > 0 else 0
    lock = threading.Lock()
    last_request_time = [0.0]

    def _fetch_one(cid: str) -> tuple[str, dict[str, Any]]:
        with lock:
            now = time.monotonic()
            wait = max(0, min_interval - (now - last_request_time[0]))
            if wait > 0:
                time.sleep(wait)
            last_request_time[0] = time.monotonic()
        return cid, fetch_company_details(cid, api_key=key, host=host, timeout=timeout)

    results: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, cid): cid for cid in company_ids}
        for future in concurrent.futures.as_completed(futures):
            cid, resp = future.result()
            results[cid] = resp
    return results


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
