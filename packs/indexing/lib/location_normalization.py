"""Deterministic location normalization for local indexing.

This is the Powerpacks-local port of the deterministic pieces from
`network-search-api/data_pipeline_v2`:

- `pipelines/people/lib/location_fixes.py`
- `pipelines/location/country_mapping.py`

It intentionally stays local and file-backed. No geocoding, LLM calls, or
network lookups happen here.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "location"
LOCATION_MAPPING_FILE = DATA_DIR / "location_normalization_map.json"
COUNTRY_MACRO_REGION_FILE = DATA_DIR / "country_macro_region.csv"

US_STATE_ABBREV_TO_FULL = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
    "PR": "Puerto Rico",
    "VI": "Virgin Islands",
    "GU": "Guam",
    "AS": "American Samoa",
    "MP": "Northern Mariana Islands",
}

CANADA_PROVINCE_ABBREV_TO_FULL = {
    "AB": "Alberta",
    "BC": "British Columbia",
    "MB": "Manitoba",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia",
    "NT": "Northwest Territories",
    "NU": "Nunavut",
    "ON": "Ontario",
    "PE": "Prince Edward Island",
    "QC": "Quebec",
    "SK": "Saskatchewan",
    "YT": "Yukon",
}

AUSTRALIA_STATE_ABBREV_TO_FULL = {
    "NSW": "New South Wales",
    "VIC": "Victoria",
    "QLD": "Queensland",
    "WA": "Western Australia",
    "SA": "South Australia",
    "TAS": "Tasmania",
    "ACT": "Australian Capital Territory",
    "NT": "Northern Territory",
}

METRO_PATTERN = re.compile(r"(?:metropolitan|metro|greater |bay area| area$)", re.IGNORECASE)

METRO_DISPLAY_OVERRIDES = {
    "bay area": "San Francisco Bay Area",
    "san francisco bay area": "San Francisco Bay Area",
    "new york city metropolitan area": "New York Metropolitan Area",
    "new york metropolitan area": "New York Metropolitan Area",
    "greater new york area": "New York Metropolitan Area",
    "los angeles metropolitan area": "Los Angeles Metropolitan Area",
    "greater seattle area": "Seattle Metropolitan Area",
    "greater boston": "Boston Metropolitan Area",
    "greater chicago area": "Chicago Metropolitan Area",
    "chicago metropolitan area": "Chicago Metropolitan Area",
    "denver metropolitan area": "Denver Metropolitan Area",
}

COUNTRY_ALIASES = {
    "u.s.": "United States",
    "u.s.a.": "United States",
    "us": "United States",
    "usa": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
}

_mapping_cache: dict[str, Any] | None = None
_country_cache: dict[str, tuple[str, str]] | None = None
_metro_inverse_cache: dict[tuple[str, str, str], str] | None = None
_city_metro_cache: (
    tuple[
        dict[tuple[str, str, str], list[str]],
        dict[str, list[tuple[str, str, list[str]]]],
    ]
    | None
) = None


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _norm_key(value: Any) -> str:
    return re.sub(r"\s+", " ", _clean(value).lower()).strip()


def _load_mapping() -> dict[str, Any]:
    global _mapping_cache
    if _mapping_cache is not None:
        return _mapping_cache
    try:
        _mapping_cache = json.loads(LOCATION_MAPPING_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _mapping_cache = {
            "city_overrides": {},
            "metro_to_city": {},
            "city_to_metro": {},
            "location_raw_overrides": {},
            "state_expansions": {},
        }
    return _mapping_cache


def _load_country_mapping() -> dict[str, tuple[str, str]]:
    global _country_cache
    if _country_cache is not None:
        return _country_cache
    mapping: dict[str, tuple[str, str]] = {}
    with COUNTRY_MACRO_REGION_FILE.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            country = _clean(row.get("country_name"))
            macro = _clean(row.get("macro_region"))
            if not country:
                continue
            canonical = COUNTRY_ALIASES.get(_norm_key(country), country)
            for value in [country, row.get("iso2"), row.get("iso3")]:
                key = _norm_key(value)
                if key:
                    mapping[key] = (canonical, macro)
    for alias, canonical in COUNTRY_ALIASES.items():
        canonical_row = mapping.get(_norm_key(canonical))
        if canonical_row:
            mapping[alias] = canonical_row
    _country_cache = mapping
    return mapping


def normalize_country(country: Any) -> str:
    value = _clean(country)
    if not value:
        return ""
    return _load_country_mapping().get(_norm_key(value), (value, ""))[0]


def get_macro_region(country: Any) -> str:
    value = _clean(country)
    if not value:
        return ""
    return _load_country_mapping().get(_norm_key(value), ("", ""))[1]


def _expand_state(state: str, country: str) -> str:
    if not state:
        return ""
    mapping = _load_mapping()
    upper = state.upper()
    if upper in mapping.get("state_expansions", {}):
        return _clean(mapping["state_expansions"][upper])
    if upper in US_STATE_ABBREV_TO_FULL:
        return US_STATE_ABBREV_TO_FULL[upper]
    if upper in CANADA_PROVINCE_ABBREV_TO_FULL:
        return CANADA_PROVINCE_ABBREV_TO_FULL[upper]
    if upper in AUSTRALIA_STATE_ABBREV_TO_FULL:
        return AUSTRALIA_STATE_ABBREV_TO_FULL[upper]
    return state


def _canonical_metro_name(key: str, source: str = "") -> str:
    lower = _norm_key(key)
    if lower in METRO_DISPLAY_OVERRIDES:
        return METRO_DISPLAY_OVERRIDES[lower]
    source = _clean(source)
    if source and source.lower() != source:
        return source
    return lower.title()


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [_clean(item) for item in value if _clean(item)]
    if isinstance(value, tuple):
        return [_clean(item) for item in value if _clean(item)]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [_clean(item) for item in parsed if _clean(item)]
        except json.JSONDecodeError:
            pass
        return [text]
    return [_clean(value)]


def _append_unique(values: list[str], value: str) -> None:
    value = _clean(value)
    if not value:
        return
    seen = {_norm_key(item) for item in values}
    if _norm_key(value) not in seen:
        values.append(value)


def _apply_fix(fix: dict[str, Any], city: str, state: str, country: str) -> tuple[str, str, str]:
    city = _clean(fix.get("city")) or city
    state = _clean(fix.get("state")) or state
    country = normalize_country(fix.get("country")) or country
    return city, state, country


def _resolve_metro(value: str) -> tuple[dict[str, Any], str] | None:
    lower = _norm_key(value)
    if not lower:
        return None
    metro_map = _load_mapping().get("metro_to_city", {})
    if lower in metro_map:
        return metro_map[lower], _canonical_metro_name(lower, value)
    for key, fix in metro_map.items():
        if lower.startswith(_norm_key(key)):
            return fix, _canonical_metro_name(key, value)
    return None


def _metro_inverse() -> dict[tuple[str, str, str], str]:
    global _metro_inverse_cache
    if _metro_inverse_cache is not None:
        return _metro_inverse_cache
    inverse: dict[tuple[str, str, str], str] = {}
    for metro_key, fix in (_load_mapping().get("metro_to_city", {}) or {}).items():
        if not isinstance(fix, dict):
            continue
        city = _norm_key(fix.get("city"))
        state = _norm_key(_expand_state(_clean(fix.get("state")), _clean(fix.get("country"))))
        country = _norm_key(normalize_country(fix.get("country")))
        if city and country:
            inverse.setdefault((city, state, country), _canonical_metro_name(metro_key))
    _metro_inverse_cache = inverse
    return inverse


def _infer_metro_from_city(city: str, state: str, country: str) -> str:
    if not city:
        return ""
    key = (_norm_key(city), _norm_key(state), _norm_key(normalize_country(country)))
    inverse = _metro_inverse()
    if key in inverse:
        return inverse[key]
    loose_key = (_norm_key(city), "", _norm_key(normalize_country(country)))
    return inverse.get(loose_key, "")


def _city_metro_index() -> tuple[
    dict[tuple[str, str, str], list[str]],
    dict[str, list[tuple[str, str, list[str]]]],
]:
    """Index the `city_to_metro` map section.

    Returns `(exact, by_city)` where `exact` keys are normalized
    `(city, state, country)` triples and `by_city` groups entries per city for
    ambiguity-aware partial matching.
    """
    global _city_metro_cache
    if _city_metro_cache is not None:
        return _city_metro_cache
    exact: dict[tuple[str, str, str], list[str]] = {}
    by_city: dict[str, list[tuple[str, str, list[str]]]] = {}
    for key, metros in (_load_mapping().get("city_to_metro", {}) or {}).items():
        parts = [part.strip() for part in str(key).split("|")]
        if len(parts) != 3:
            continue
        city_key, state_key, country_key = (_norm_key(part) for part in parts)
        metro_list = _as_list(metros)
        if not city_key or not metro_list:
            continue
        exact[(city_key, state_key, country_key)] = metro_list
        by_city.setdefault(city_key, []).append((state_key, country_key, metro_list))
    _city_metro_cache = (exact, by_city)
    return _city_metro_cache


def _metros_for_city(city: str, state: str, country: str) -> list[str]:
    """Derive metro areas from a city via the `city_to_metro` map.

    Matching is conservative: an exact (city, state, country) hit always wins;
    a partial city+state or city+country hit is only used when the missing
    component is empty on the input AND the map has exactly one candidate for
    that city, so an ambiguous city name never picks a metro on its own.
    """
    if not city:
        return []
    city_key = _norm_key(city)
    state_key = _norm_key(state)
    country_key = _norm_key(normalize_country(country))
    exact, by_city = _city_metro_index()
    hit = exact.get((city_key, state_key, country_key))
    if hit:
        return list(hit)
    entries = by_city.get(city_key, [])
    if not entries:
        return []
    if state_key and not country_key:
        matches = [entry for entry in entries if entry[0] == state_key]
        if len(matches) == 1:
            return list(matches[0][2])
        return []
    if country_key and not state_key:
        matches = [entry for entry in entries if entry[1] == country_key]
        if len(matches) == 1:
            return list(matches[0][2])
        return []
    # City alone, or a state/country combination not in the map: never guess.
    return []


def normalize_location_fields(
    *,
    city: Any = "",
    state: Any = "",
    country: Any = "",
    location_raw: Any = "",
    metro_areas: Any = None,
    macro_region: Any = "",
) -> dict[str, Any]:
    """Normalize a person/company location into search-index fields."""

    city_text = _clean(city)
    state_text = _clean(state)
    country_text = normalize_country(country)
    raw_text = _clean(location_raw)
    metros = _as_list(metro_areas)

    mapping = _load_mapping()
    raw_lower = _norm_key(raw_text)
    raw_fix = mapping.get("location_raw_overrides", {}).get(raw_lower)
    if isinstance(raw_fix, dict):
        city_text, state_text, country_text = _apply_fix(raw_fix, city_text, state_text, country_text)
    else:
        metro = _resolve_metro(raw_text)
        if metro:
            city_text, state_text, country_text = _apply_fix(metro[0], city_text, state_text, country_text)
            _append_unique(metros, metro[1])

    override = mapping.get("city_overrides", {}).get(f"{city_text}|{state_text}")
    if override:
        if isinstance(override, str):
            city_text = override
        elif isinstance(override, dict):
            city_text, state_text, country_text = _apply_fix(override, city_text, state_text, country_text)

    if city_text and METRO_PATTERN.search(city_text):
        metro = _resolve_metro(city_text)
        if metro:
            _append_unique(metros, metro[1])
            city_text, state_text, country_text = _apply_fix(metro[0], city_text, state_text, country_text)

    state_text = _expand_state(state_text, country_text)
    country_text = normalize_country(country_text)

    inferred_metro = _infer_metro_from_city(city_text, state_text, country_text)
    if inferred_metro:
        _append_unique(metros, inferred_metro)
    for mapped_metro in _metros_for_city(city_text, state_text, country_text):
        _append_unique(metros, mapped_metro)

    macro_text = _clean(macro_region) or get_macro_region(country_text)
    return {
        "city": city_text,
        "state": state_text,
        "country": country_text,
        "location_raw": raw_text,
        "metro_areas": metros,
        "macro_region": macro_text,
    }


def normalize_city(city: str, state: str, country: str, metro_areas: list[str] | None = None) -> tuple[str, str, str, list[str]]:
    location = normalize_location_fields(city=city, state=state, country=country, metro_areas=metro_areas)
    return location["city"], location["state"], location["country"], location["metro_areas"]


def normalize_location_raw(city: str, state: str, country: str, location_raw: str) -> tuple[str, str, str]:
    location = normalize_location_fields(city=city, state=state, country=country, location_raw=location_raw)
    return location["city"], location["state"], location["country"]
