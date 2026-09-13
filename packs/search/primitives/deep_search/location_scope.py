"""Canonical location filters and readable query labels."""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packs.indexing.lib.location_normalization import (  # noqa: E402
    AUSTRALIA_STATE_ABBREV_TO_FULL,
    CANADA_PROVINCE_ABBREV_TO_FULL,
    COUNTRY_MACRO_REGION_FILE,
    LOCATION_MAPPING_FILE,
    US_STATE_ABBREV_TO_FULL,
    get_macro_region,
    normalize_country,
    normalize_location_fields,
    unambiguous_metro_areas_for_city,
)
LOCATION_FILTER_FIELDS = ("cities", "states", "countries", "metro_areas", "macro_regions")
CITY_ALIASES = {
    "sf": "San Francisco",
    "nyc": "New York",
    "new york city": "New York",
    "washington dc": "Washington",
    "washington d.c.": "Washington",
}
MACRO_REGIONS = {
    "apac": "APAC",
    "americas": "Americas",
    "eurasia": "Eurasia",
    "middle east": "Middle East",
    "south asia": "South Asia",
    "sub saharan africa": "Sub-Saharan Africa",
    "western europe": "Western Europe",
}
METRO_ALIASES = {
    "bay area": "San Francisco Bay Area",
    "silicon valley": "San Francisco Bay Area",
    "dmv": "Washington D.C. Metropolitan Area",
    "dc metro": "Washington D.C. Metropolitan Area",
    "washington dc metropolitan area": "Washington D.C. Metropolitan Area",
    "nyc metro": "New York Metropolitan Area",
    "tri state area": "New York Metropolitan Area",
}


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _metro_vocabulary() -> dict[str, str]:
    """Map accepted corpus aliases to the exact metro values written by local normalization."""
    mapping = json.loads(LOCATION_MAPPING_FILE.read_text(encoding="utf-8"))
    aliases: dict[str, str] = {}
    for raw in (mapping.get("metro_to_city") or {}):
        normalized = normalize_location_fields(location_raw=raw)
        metros = normalized.get("metro_areas") or []
        if len(metros) == 1:
            aliases[_norm(raw)] = metros[0]
            aliases.setdefault(_norm(metros[0]), metros[0])
    # Exact city-to-metro outputs are authoritative and must remain idempotent.
    for raw_values in (mapping.get("city_to_metro") or {}).values():
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        for value in values:
            if isinstance(value, str) and value.strip():
                aliases[_norm(value)] = value.strip()
    for alias, canonical in METRO_ALIASES.items():
        aliases[_norm(alias)] = aliases.get(_norm(canonical), canonical)
    return aliases


METRO_VOCABULARY = _metro_vocabulary()


def _continent_countries() -> dict[str, list[str]]:
    by_continent: dict[str, list[str]] = {}
    with COUNTRY_MACRO_REGION_FILE.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            country = normalize_country(row.get("country_name"))
            continent = str(row.get("continent") or "").strip()
            if not country or not continent:
                continue
            countries = by_continent.setdefault(continent, [])
            if country not in countries:
                countries.append(country)
    return by_continent


CONTINENT_COUNTRIES = _continent_countries()
LATIN_AMERICA_COUNTRIES = frozenset({
    "Argentina", "Bolivia", "Brazil", "Chile", "Colombia", "Costa Rica", "Cuba",
    "Dominican Republic", "Ecuador", "El Salvador", "Guatemala", "Haiti", "Honduras",
    "Mexico", "Nicaragua", "Panama", "Paraguay", "Peru", "Puerto Rico", "Uruguay",
    "Venezuela",
})


def _clean_filters(raw_filters: Any) -> dict[str, list[str]]:
    if not isinstance(raw_filters, dict):
        raise ValueError("location_filters must be an object")
    unknown = sorted(set(raw_filters) - set(LOCATION_FILTER_FIELDS))
    if unknown:
        raise ValueError(f"location_filters has unsupported fields: {unknown}")
    filters: dict[str, list[str]] = {}
    for field in LOCATION_FILTER_FIELDS:
        values = raw_filters.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(f"location_filters.{field} must be a list of non-empty strings")
        if values:
            filters[field] = list(dict.fromkeys(value.strip() for value in values))
    return filters


STATE_MAPS = {
    "United States": US_STATE_ABBREV_TO_FULL,
    "Canada": CANADA_PROVINCE_ABBREV_TO_FULL,
    "Australia": AUSTRALIA_STATE_ABBREV_TO_FULL,
}


def _state_candidates(value: str) -> list[tuple[str, str]]:
    upper = value.upper()
    normalized = _norm(value)
    candidates: list[tuple[str, str]] = []
    for country, mapping in STATE_MAPS.items():
        for abbreviation, canonical in mapping.items():
            if upper == abbreviation or normalized == _norm(canonical):
                pair = (country, canonical)
                if pair not in candidates:
                    candidates.append(pair)
    return candidates


def _canonical_state(value: str, countries: list[str]) -> str:
    candidates = _state_candidates(value)
    if len(countries) == 1:
        country = countries[0]
        for candidate_country, canonical in candidates:
            if candidate_country == country:
                return canonical
        if candidates:
            raise ValueError(f"state {value!r} is not valid for country {country!r}")
        return value.strip()
    if len(candidates) == 1:
        return candidates[0][1]
    if len(candidates) > 1:
        raise ValueError(f"ambiguous state abbreviation {value!r}; add one country qualifier")
    return value.strip()


def _canonical_filter_value(field: str, value: str, *, countries: list[str] | None = None) -> str:
    if field == "macro_regions":
        canonical = MACRO_REGIONS.get(_norm(value))
    elif field == "countries":
        country = normalize_country(value)
        canonical = country if get_macro_region(country) else None
    elif field == "cities":
        canonical = CITY_ALIASES.get(_norm(value), value.strip())
    elif field == "states":
        canonical = _canonical_state(value, countries or [])
    elif field == "metro_areas":
        canonical = METRO_VOCABULARY.get(_norm(value))
    else:
        canonical = None
    if not canonical:
        raise ValueError(f"unsupported {field} location value: {value!r}")
    return canonical


def canonicalize_location_filters(raw_filters: Any) -> dict[str, list[str]]:
    """Normalize aliases into the exact vocabulary stored by search backends."""
    cleaned = _clean_filters(raw_filters)
    countries = list(dict.fromkeys(
        _canonical_filter_value("countries", value)
        for value in cleaned.get("countries", [])
    ))
    return {
        field: list(dict.fromkeys(
            _canonical_filter_value(field, value, countries=countries) for value in values
        ))
        for field, values in cleaned.items()
    }


def prefer_metro_area_filters(raw_filters: Any) -> dict[str, list[str]]:
    """Prefer canonical metro-only retrieval for wholly unambiguous city scopes.

    The conversion is all-or-nothing: every city must resolve to exactly one
    indexed metro under the supplied country. Otherwise the canonical exact
    city scope is returned unchanged. This prevents mixed city/metro OR
    broadening and makes the helper idempotent for already-canonical metros.
    """
    canonical = canonicalize_location_filters(raw_filters)
    cities = canonical.get("cities")
    if not cities:
        return canonical
    if set(canonical) not in ({"cities"}, {"cities", "countries"}):
        return canonical
    countries = canonical.get("countries", [])
    if len(countries) > 1:
        return canonical
    country = countries[0] if countries else ""
    metros: list[str] = []
    for city in cities:
        mapped = unambiguous_metro_areas_for_city(city, country=country)
        if len(mapped) != 1:
            return canonical
        if mapped[0] not in metros:
            metros.append(mapped[0])
    return {"metro_areas": metros}


def canonical_location_label(filters: dict[str, list[str]]) -> str:
    """Build a compact label from location filters."""
    if set(filters) == {"countries"}:
        countries = set(filters["countries"])
        if countries == LATIN_AMERICA_COUNTRIES:
            return "Latin America"
        for continent in ("Africa", "Oceania"):
            if countries == set(CONTINENT_COUNTRIES[continent]):
                return continent
        return " or ".join(filters["countries"])
    if set(filters) == {"macro_regions"}:
        if set(filters["macro_regions"]) == {"Western Europe", "Eurasia"}:
            return "Europe"
        return " or ".join(filters["macro_regions"])
    if set(filters) == {"metro_areas"}:
        return " or ".join(filters["metro_areas"])
    if set(filters) in ({"cities", "countries"}, {"states", "countries"}):
        field = "cities" if "cities" in filters else "states"
        return f"{' or '.join(filters[field])}, {filters['countries'][0]}"
    return " and ".join(
        f"{' or '.join(values)} ({field})" for field, values in filters.items()
    )


def query_location_label(filters: dict[str, list[str]]) -> str:
    """Keep every location alternative in the recruiter query."""
    return canonical_location_label(prefer_metro_area_filters(filters))
