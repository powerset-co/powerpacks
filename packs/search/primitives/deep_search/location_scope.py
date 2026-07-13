"""Deterministic matching for an approved deep-search location constraint."""
from __future__ import annotations

import csv
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
    US_STATE_ABBREV_TO_FULL,
    get_macro_region,
    normalize_country,
    normalize_location_fields,
)

UNSCOPED_LOCATIONS = {"", "global", "remote", "remote only", "worldwide", "anywhere"}
LOCATION_FILTER_FIELDS = ("cities", "states", "countries", "metro_areas", "macro_regions")
LOCATION_ALIASES = {
    "sf": "San Francisco, CA",
    "nyc": "New York, NY",
    "new york city": "New York, NY",
}
CITY_ALIASES = {"new york city": "New York"}
MACRO_REGIONS = {
    "apac": "APAC",
    "americas": "Americas",
    "eurasia": "Eurasia",
    "middle east": "Middle East",
    "south asia": "South Asia",
    "sub saharan africa": "Sub-Saharan Africa",
    "western europe": "Western Europe",
}
DISPLAY_REGION_FILTERS = {
    "apac": ("macro_regions", {"APAC"}),
    "asia pacific": ("macro_regions", {"APAC"}),
    "americas": ("macro_regions", {"Americas"}),
    "eurasia": ("macro_regions", {"Eurasia"}),
    "europe": ("macro_regions", {"Western Europe", "Eurasia"}),
    "middle east": ("macro_regions", {"Middle East"}),
    "south asia": ("macro_regions", {"South Asia"}),
    "sub saharan africa": ("macro_regions", {"Sub-Saharan Africa"}),
    "western europe": ("macro_regions", {"Western Europe"}),
}
METRO_ALIASES = {
    "london metropolitan area": "London Area",
}


def _location_vocabularies() -> tuple[
    dict[str, list[str]],
    dict[str, list[str]],
]:
    by_continent: dict[str, list[str]] = {}
    by_macro: dict[str, list[str]] = {}
    with COUNTRY_MACRO_REGION_FILE.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            country = normalize_country(row.get("country_name"))
            continent = str(row.get("continent") or "").strip()
            if not country or not continent:
                continue
            countries = by_continent.setdefault(continent, [])
            if country not in countries:
                countries.append(country)
            macro = str(row.get("macro_region") or "").strip()
            macro_countries = by_macro.setdefault(macro, [])
            if macro and country not in macro_countries:
                macro_countries.append(country)
    return by_continent, by_macro


CONTINENT_COUNTRIES, MACRO_COUNTRIES = _location_vocabularies()
DISPLAY_REGION_FILTERS.update({
    "africa": ("countries", set(CONTINENT_COUNTRIES["Africa"])),
    "oceania": ("countries", set(CONTINENT_COUNTRIES["Oceania"])),
})


def _clean_filters(raw_filters: Any, *, subject: str) -> dict[str, list[str]]:
    if not isinstance(raw_filters, dict):
        raise ValueError(f"{subject} must be an object")
    unknown = sorted(set(raw_filters) - set(LOCATION_FILTER_FIELDS))
    if unknown:
        raise ValueError(f"{subject} has unsupported fields: {unknown}")
    filters: dict[str, list[str]] = {}
    for field in LOCATION_FILTER_FIELDS:
        values = raw_filters.get(field, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(f"{subject}.{field} must be a list of non-empty strings")
        if values:
            filters[field] = list(dict.fromkeys(value.strip() for value in values))
    return filters


def location_scope_from_plan(plan: dict[str, Any] | None) -> tuple[str | None, dict[str, list[str]]]:
    """Return and validate the reviewed display location plus execution filters."""
    scope = (plan or {}).get("search_scope") or {}
    if "filters" not in scope:
        raise ValueError("approved search_scope.filters is required")
    raw = scope.get("location")
    filters = _clean_filters(scope.get("filters"), subject="approved search_scope.filters")

    if raw is None:
        if filters:
            raise ValueError("an unscoped null location cannot contain location filters")
        return None, {}
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("approved search_scope.location must be a non-empty string or null")
    location = raw.strip()
    if location.lower() in UNSCOPED_LOCATIONS:
        raise ValueError("use null, not a global/remote alias, for an unscoped approved location")
    if not filters:
        raise ValueError("a required location must have at least one non-empty structured filter family")
    canonical = canonicalize_location_filters(filters)
    if canonical != filters:
        raise ValueError(f"approved search_scope.filters must use canonical values: {canonical}")
    if ("cities" in filters or "states" in filters) and "countries" not in filters:
        raise ValueError("city/state location filters require a country qualifier")
    if ("cities" in filters or "states" in filters) and len(filters["countries"]) != 1:
        raise ValueError("city/state location filters require exactly one country qualifier")
    if "cities" in filters and "states" in filters:
        raise ValueError("use canonical metro areas instead of combining city and state filters")
    if sum(1 for values in filters.values() if len(values) > 1) > 1:
        raise ValueError("multi-valued location families create ambiguous cross-product scopes")
    _validate_display_consistency(location, filters)
    return location, filters


def required_location_from_plan(plan: dict[str, Any] | None) -> str | None:
    return location_scope_from_plan(plan)[0]


def location_filters_from_plan(plan: dict[str, Any] | None) -> dict[str, list[str]]:
    return location_scope_from_plan(plan)[1]


def _location_fields(value: Any) -> dict[str, Any]:
    raw = str(value or "").strip()
    raw = LOCATION_ALIASES.get(raw.lower(), raw)
    if not raw:
        return normalize_location_fields()

    parts = [part.strip() for part in raw.split(",") if part.strip()]
    city = state = country = ""
    if len(parts) >= 3:
        city, state, country = parts[0], parts[1], ", ".join(parts[2:])
    elif len(parts) == 2:
        city, qualifier = parts
        upper = qualifier.upper()
        if upper in US_STATE_ABBREV_TO_FULL:
            state, country = qualifier, "United States"
        elif upper in CANADA_PROVINCE_ABBREV_TO_FULL:
            state, country = qualifier, "Canada"
        elif upper in AUSTRALIA_STATE_ABBREV_TO_FULL:
            state, country = qualifier, "Australia"
        elif get_macro_region(qualifier) or normalize_country(qualifier) != qualifier:
            country = qualifier
        else:
            state = qualifier
    else:
        normalized = normalize_location_fields(location_raw=raw)
        if any(normalized.get(field) for field in ("city", "state", "country", "metro_areas")):
            return normalized
        upper = raw.upper()
        if upper in US_STATE_ABBREV_TO_FULL or raw in US_STATE_ABBREV_TO_FULL.values():
            return normalize_location_fields(state=raw, country="United States", location_raw=raw)
        if upper in CANADA_PROVINCE_ABBREV_TO_FULL or raw in CANADA_PROVINCE_ABBREV_TO_FULL.values():
            return normalize_location_fields(state=raw, country="Canada", location_raw=raw)
        if upper in AUSTRALIA_STATE_ABBREV_TO_FULL or raw in AUSTRALIA_STATE_ABBREV_TO_FULL.values():
            return normalize_location_fields(state=raw, country="Australia", location_raw=raw)
        if get_macro_region(raw) or normalize_country(raw) != raw:
            return normalize_location_fields(country=raw, location_raw=raw)
        return normalize_location_fields(city=raw, location_raw=raw)

    city = CITY_ALIASES.get(city.lower(), city)
    return normalize_location_fields(
        city=city,
        state=state,
        country=country,
        location_raw=raw,
    )


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _canonical_filter_value(field: str, value: str) -> str:
    if field == "macro_regions":
        canonical = MACRO_REGIONS.get(_norm(value))
    elif field == "countries":
        country = normalize_country(value)
        canonical = country if get_macro_region(country) else None
    else:
        fields = _location_fields(value)
        source = {
            "cities": "city",
            "states": "state",
            "metro_areas": "metro_areas",
        }[field]
        observed = fields.get(source)
        if field == "metro_areas":
            values = observed if isinstance(observed, list) else []
            canonical = values[0] if len(values) == 1 else METRO_ALIASES.get(_norm(value))
        else:
            canonical = str(observed or "").strip() or None
    if not canonical:
        raise ValueError(f"unsupported {field} location value: {value!r}")
    return canonical


def canonicalize_location_filters(raw_filters: Any) -> dict[str, list[str]]:
    """Normalize aliases into the exact vocabulary stored by search backends."""
    cleaned = _clean_filters(raw_filters, subject="location_filters")
    return {
        field: list(dict.fromkeys(_canonical_filter_value(field, value) for value in values))
        for field, values in cleaned.items()
    }


def canonicalize_generated_location_filters(location: str, raw_filters: Any) -> dict[str, list[str]]:
    """Canonicalize a draft plan and add the country needed to disambiguate city/state scopes."""
    cleaned = _clean_filters(raw_filters, subject="location_filters")
    macro_regions: list[str] = []
    for value in cleaned.get("macro_regions") or []:
        if _norm(value) == "europe":
            macro_regions.extend(["Western Europe", "Eurasia"])
        else:
            macro_regions.append(value)
    if macro_regions:
        cleaned["macro_regions"] = list(dict.fromkeys(macro_regions))
    if any(_norm(value) in {"africa", "oceania"} for value in macro_regions):
        countries = list(cleaned.get("countries") or [])
        for value in macro_regions:
            key = _norm(value)
            if key in {"africa", "oceania"}:
                countries.extend(CONTINENT_COUNTRIES[key.title()])
            else:
                canonical = MACRO_REGIONS.get(key)
                if not canonical:
                    raise ValueError(f"unsupported macro_regions location value: {value!r}")
                countries.extend(MACRO_COUNTRIES[canonical])
        cleaned.pop("macro_regions", None)
        cleaned["countries"] = list(dict.fromkeys(countries))
    filters = canonicalize_location_filters(cleaned)
    if ("cities" in filters or "states" in filters) and "countries" not in filters:
        country = str(_location_fields(location).get("country") or "").strip()
        if not country:
            raise ValueError("city/state location filters need a country in the extracted location")
        filters["countries"] = [_canonical_filter_value("countries", country)]
    return filters


def _validate_display_consistency(location: str, filters: dict[str, list[str]]) -> None:
    region_scope = DISPLAY_REGION_FILTERS.get(_norm(location))
    if region_scope is not None:
        field, region_values = region_scope
        if set(filters.get(field) or []) != region_values or len(filters) != 1:
            raise ValueError(f"approved location {location!r} requires {field} {sorted(region_values)}")
        return

    fields = _location_fields(location)
    expected = {
        "cities": [fields.get("city")],
        "states": [fields.get("state")],
        "countries": [fields.get("country")],
        "metro_areas": fields.get("metro_areas") or [],
        "macro_regions": [fields.get("macro_region")],
    }
    for field, wanted in filters.items():
        available = {_norm(value) for value in expected[field] if value}
        if available and not {_norm(value) for value in wanted}.issubset(available):
            raise ValueError(f"approved {field} filters conflict with location {location!r}")


def location_fit(required_filters: dict[str, list[str]] | None, candidate: Any) -> str:
    """Return match, mismatch, unknown, or not_required for the approved scope."""
    if not required_filters:
        return "not_required"
    if not str(candidate or "").strip():
        return "unknown"

    actual = _location_fields(candidate)
    source_fields = {
        "cities": "city",
        "states": "state",
        "countries": "country",
        "metro_areas": "metro_areas",
        "macro_regions": "macro_region",
    }
    missing = False
    for field, wanted_values in required_filters.items():
        observed_raw = actual.get(source_fields[field])
        observed_values = observed_raw if isinstance(observed_raw, list) else [observed_raw]
        observed = {_norm(value) for value in observed_values if _norm(value)}
        if not observed:
            missing = True
            continue
        wanted = {_norm(value) for value in wanted_values if _norm(value)}
        if not wanted & observed:
            return "mismatch"
    return "unknown" if missing else "match"


def enforce_payload_location(
    payload: dict[str, Any],
    required_filters: dict[str, list[str]],
) -> dict[str, Any]:
    """Replace model-extracted geo filters with the approved deterministic scope."""
    filters = payload.get("role_search_filters")
    if not isinstance(filters, dict):
        filters = payload
    if filters.get("hard_filters"):
        raise ValueError("prepared hard_filters would bypass the approved location scope")
    for field in (
        "cities", "states", "countries", "metro_areas", "macro_regions",
        "company_cities", "company_states", "company_countries",
        "company_metro_areas", "company_macro_regions", "location_filter_mode",
    ):
        filters.pop(field, None)
    filters.update(required_filters)
    if len(required_filters) > 1:
        filters["location_filter_mode"] = "all"
    return payload
