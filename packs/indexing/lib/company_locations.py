"""Always-on company HQ location backfill from the local RapidAPI company cache.

The local pipeline derives companies from people's work experiences, which
carry no company HQ location. The enrichment stage already collects RapidAPI
company payloads under ``.powerpacks/rapidapi-company-cache/``; the company
corpus step joins each company to its RapidAPI company id (carried from work
experiences onto the raw corpus), reads the cached payload from disk, extracts
the headquarters city/state/country, and normalizes the raw LinkedIn codes
("US", "CA") through the exact ``normalize_location_fields`` path used for
people locations so company and person location values share one value space
(full country/state names, derived ``metro_area`` and ``macro_region``).

This is strictly cache-only and file-backed: it reads previously cached JSON
payloads via ``load_cached_company_details`` and never touches the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from packs.indexing.lib.location_normalization import get_macro_region, normalize_location_fields
from packs.indexing.primitives.enrich_companies_checkpointed.rapidapi_company import (
    extract_company_context,
    load_cached_company_details,
)

COMPANY_LOCATION_FIELDS = ("city", "state", "country", "metro_area", "macro_region")

# RapidAPI uses "0" as a placeholder id for companies it could not resolve;
# joining on it would attach one bogus payload to many companies.
RAPIDAPI_SENTINEL_IDS = {"", "0"}


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_company_hq(city: Any, state: Any, country: Any) -> dict[str, str]:
    """Normalize raw RapidAPI HQ values via the shared people-location path.

    Returns the five COMPANY_LOCATION_FIELDS with country codes expanded to
    full names ("US" -> "United States"), state abbreviations expanded
    ("CA" -> "California"), ``metro_area`` derived from the city mapping, and
    ``macro_region`` derived from the country.
    """

    location = normalize_location_fields(city=city, state=state, country=country)
    metros = location.get("metro_areas") or []
    return {
        "city": _clean(location.get("city")),
        "state": _clean(location.get("state")),
        "country": _clean(location.get("country")),
        "metro_area": _clean(metros[0]) if metros else "",
        "macro_region": _clean(location.get("macro_region")),
    }


def load_company_hq_from_cache(
    rapidapi_ids: Iterable[Any],
    *,
    cache_dir: str | Path | None = None,
) -> dict[str, dict[str, str]]:
    """Map RapidAPI company id -> normalized non-empty HQ fields, cache-only.

    Cache misses and payloads without an HQ city/country are omitted. This
    never performs network calls: it only reads the on-disk payload cache.
    """

    ids = sorted({_clean(rid) for rid in rapidapi_ids} - RAPIDAPI_SENTINEL_IDS)
    lookup: dict[str, dict[str, str]] = {}
    for rid, response in load_cached_company_details(ids, cache_dir=cache_dir).items():
        context = extract_company_context(response)
        if not (_clean(context.get("city")) or _clean(context.get("country"))):
            continue
        if _clean(context.get("country")).upper() == "OO":
            # LinkedIn "Worldwide" placeholder HQ ("OO" country, "Worldwide"
            # city) carries no real location; keep the value space clean.
            continue
        location = normalize_company_hq(context.get("city"), context.get("state"), context.get("country"))
        location = {field: value for field, value in location.items() if value}
        if location:
            lookup[rid] = location
    return lookup


def backfill_company_locations_from_rapidapi(
    rows: list[dict[str, Any]],
    urn_to_rapidapi_id: dict[str, str],
    *,
    cache_dir: str | Path | None = None,
) -> dict[str, int]:
    """Fill empty HQ fields on company corpus rows in place; never overwrite.

    Each row is joined by ``company_urn`` through *urn_to_rapidapi_id* to its
    cached RapidAPI payload. Only empty ``city``/``state``/``country``/
    ``metro_area``/``macro_region`` values are filled. ``macro_region``
    additionally falls back to the country -> macro-region mapping for rows
    that already carry a country. Cache misses leave rows untouched.
    """

    urn_to_rid = {
        _clean(urn): _clean(rid)
        for urn, rid in (urn_to_rapidapi_id or {}).items()
        if _clean(urn) and _clean(rid) not in RAPIDAPI_SENTINEL_IDS
    }
    lookup = load_company_hq_from_cache(urn_to_rid.values(), cache_dir=cache_dir)
    stats = {
        "companies_with_rapidapi_id": len(urn_to_rid),
        "cached_payloads_with_hq": len(lookup),
        "matched": 0,
        "companies_filled": 0,
        "fields_filled": 0,
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        urn = _clean(row.get("company_urn") or row.get("id"))
        location = lookup.get(urn_to_rid.get(urn, ""))
        filled = 0
        if location:
            stats["matched"] += 1
            for field in COMPANY_LOCATION_FIELDS:
                if not _clean(row.get(field)) and location.get(field):
                    row[field] = location[field]
                    filled += 1
        if not _clean(row.get("macro_region")) and _clean(row.get("country")):
            macro_region = get_macro_region(row.get("country"))
            if macro_region:
                row["macro_region"] = macro_region
                filled += 1
        if filled:
            stats["companies_filled"] += 1
            stats["fields_filled"] += filled
    return stats
