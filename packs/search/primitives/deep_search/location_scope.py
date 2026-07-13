"""Deterministic matching for an approved deep-search location constraint."""
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
)
UNSCOPED_LOCATIONS = {"", "global", "remote", "remote only", "worldwide", "anywhere"}
LOCATION_FILTER_FIELDS = ("cities", "states", "countries", "metro_areas", "macro_regions")
LOCATION_ALIASES = {
    "sf": "San Francisco, CA",
    "nyc": "New York, NY",
    "new york city": "New York, NY",
}
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


def _geographic_label(value: Any) -> str:
    return re.sub(
        r"^(?:remote|distributed)(?:\s+(?:in|within))?\s*[-:/]?\s*",
        "",
        str(value or "").strip(),
        flags=re.I,
    )


def _location_mapping() -> dict[str, Any]:
    return json.loads(LOCATION_MAPPING_FILE.read_text(encoding="utf-8"))


def _metro_vocabulary() -> dict[str, str]:
    """Map accepted corpus aliases to the exact metro values written by local normalization."""
    mapping = _location_mapping()
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


def _city_contexts() -> dict[str, list[dict[str, str]]]:
    contexts: dict[str, list[dict[str, str]]] = {}
    mapping = _location_mapping()
    for key, metros in (mapping.get("city_to_metro") or {}).items():
        parts = [part.strip() for part in str(key).split("|")]
        if len(parts) != 3:
            continue
        city, state, country = parts
        values = metros if isinstance(metros, list) else [metros]
        context = {
            "city": city,
            "state": state,
            "country": normalize_country(country),
            "metro": str(values[0] if values else ""),
        }
        bucket = contexts.setdefault(_norm(city), [])
        if context not in bucket:
            bucket.append(context)
    for fix in (mapping.get("metro_to_city") or {}).values():
        if not isinstance(fix, dict) or not fix.get("city"):
            continue
        context = {
            "city": str(fix.get("city") or "").strip(),
            "state": str(fix.get("state") or "").strip(),
            "country": normalize_country(fix.get("country")),
            "metro": "",
        }
        bucket = contexts.setdefault(_norm(context["city"]), [])
        if context not in bucket:
            bucket.append(context)
    return contexts


CITY_CONTEXTS = _city_contexts()


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
LATIN_AMERICA_COUNTRIES = frozenset({
    "Argentina", "Bolivia", "Brazil", "Chile", "Colombia", "Costa Rica", "Cuba",
    "Dominican Republic", "Ecuador", "El Salvador", "Guatemala", "Haiti", "Honduras",
    "Mexico", "Nicaragua", "Panama", "Paraguay", "Peru", "Puerto Rico", "Uruguay",
    "Venezuela",
})
BROAD_LOCATION_COUNTRIES = {
    "africa": frozenset(CONTINENT_COUNTRIES["Africa"]),
    "latam": LATIN_AMERICA_COUNTRIES,
    "latin america": LATIN_AMERICA_COUNTRIES,
    "oceania": frozenset(CONTINENT_COUNTRIES["Oceania"]),
}
DISPLAY_REGION_FILTERS.update({
    "africa": ("countries", set(CONTINENT_COUNTRIES["Africa"])),
    "latam": ("countries", set(LATIN_AMERICA_COUNTRIES)),
    "latin america": ("countries", set(LATIN_AMERICA_COUNTRIES)),
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
    families = frozenset(filters)
    allowed_shapes = {
        frozenset({"cities", "countries"}),
        frozenset({"states", "countries"}),
        frozenset({"metro_areas"}),
        frozenset({"countries"}),
        frozenset({"macro_regions"}),
    }
    if families not in allowed_shapes:
        raise ValueError(f"unsupported location filter family combination: {sorted(families)}")
    _validate_display_consistency(location, filters)
    return location, filters


def required_location_from_plan(plan: dict[str, Any] | None) -> str | None:
    return location_scope_from_plan(plan)[0]


def location_filters_from_plan(plan: dict[str, Any] | None) -> dict[str, list[str]]:
    return location_scope_from_plan(plan)[1]


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


def _location_fields(value: Any, *, country_hint: str | None = None) -> dict[str, Any]:
    raw = _geographic_label(value)
    raw = LOCATION_ALIASES.get(raw.lower(), raw)
    if not raw:
        return normalize_location_fields()

    parts = [part.strip() for part in raw.split(",") if part.strip()]
    city = state = country = ""
    if len(parts) >= 3:
        city, state, country = parts[0], parts[1], ", ".join(parts[2:])
    elif len(parts) == 2:
        city, qualifier = parts
        state_candidates = _state_candidates(qualifier)
        if country_hint:
            matching = [pair for pair in state_candidates if pair[0] == country_hint]
            if matching:
                country, state = matching[0]
            elif get_macro_region(qualifier):
                country = qualifier
            else:
                state, country = qualifier, country_hint
        elif len(state_candidates) == 1:
            country, state = state_candidates[0]
        elif len(state_candidates) > 1:
            # Do not invent a country for WA/NT-style collisions. Structured retrieval
            # geo or an explicit plan country can resolve it; string-only fallback is unknown.
            state = qualifier
        elif get_macro_region(qualifier):
            country = qualifier
        else:
            state = qualifier
    else:
        normalized = normalize_location_fields(location_raw=raw)
        if any(normalized.get(field) for field in ("city", "state", "country", "metro_areas")):
            return normalized
        state_candidates = _state_candidates(raw)
        if country_hint:
            matching = [pair for pair in state_candidates if pair[0] == country_hint]
            if matching:
                return normalize_location_fields(
                    state=matching[0][1], country=country_hint, location_raw=raw,
                )
        if len(state_candidates) == 1:
            return normalize_location_fields(
                state=state_candidates[0][1], country=state_candidates[0][0], location_raw=raw,
            )
        if get_macro_region(raw):
            return normalize_location_fields(country=raw, location_raw=raw)
        return normalize_location_fields(city=raw, location_raw=raw)

    city = CITY_ALIASES.get(city.lower(), city)
    return normalize_location_fields(
        city=city,
        state=state,
        country=country or country_hint or "",
        location_raw=raw,
    )


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
    cleaned = _clean_filters(raw_filters, subject="location_filters")
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


def canonicalize_generated_location_filters(location: str, raw_filters: Any) -> dict[str, list[str]]:
    """Canonicalize a draft plan and add the country needed to disambiguate city/state scopes."""
    cleaned = _clean_filters(raw_filters, subject="location_filters")
    display_region = DISPLAY_REGION_FILTERS.get(_norm(_geographic_label(location)))
    if display_region is not None:
        field, values = display_region
        ordered = (
            ["Western Europe", "Eurasia"]
            if field == "macro_regions" and values == {"Western Europe", "Eurasia"}
            else sorted(values)
        )
        cleaned = {field: ordered}
    macro_regions: list[str] = []
    for value in cleaned.get("macro_regions") or []:
        if _norm(value) == "europe":
            macro_regions.extend(["Western Europe", "Eurasia"])
        else:
            macro_regions.append(value)
    if macro_regions:
        cleaned["macro_regions"] = list(dict.fromkeys(macro_regions))
    if any(_norm(value) in BROAD_LOCATION_COUNTRIES for value in macro_regions):
        countries = list(cleaned.get("countries") or [])
        for value in macro_regions:
            key = _norm(value)
            if key in BROAD_LOCATION_COUNTRIES:
                countries.extend(sorted(BROAD_LOCATION_COUNTRIES[key]))
            else:
                canonical = MACRO_REGIONS.get(key)
                if not canonical:
                    raise ValueError(f"unsupported macro_regions location value: {value!r}")
                countries.extend(MACRO_COUNTRIES[canonical])
        cleaned.pop("macro_regions", None)
        cleaned["countries"] = list(dict.fromkeys(countries))
    if ("cities" in cleaned or "states" in cleaned) and "countries" not in cleaned:
        country = str(_location_fields(location).get("country") or "").strip()
        if not country:
            raise ValueError("city/state location filters need a country in the extracted location")
        cleaned["countries"] = [country]
    return canonicalize_location_filters(cleaned)


def canonical_location_label(filters: dict[str, list[str]]) -> str:
    """Build a compact reviewed label directly from the authoritative filter scope."""
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


def _display_city(location: str) -> str:
    raw = _geographic_label(location)
    raw = LOCATION_ALIASES.get(raw.strip().lower(), raw.strip())
    first = raw.split(",", 1)[0].strip()
    return CITY_ALIASES.get(_norm(first), first)


def _display_alternatives(location: str) -> list[str]:
    """Split a reviewed OR label without treating commas inside a place as alternatives."""
    label = re.sub(
        r",\s*(?:and|or)\s+",
        " or ",
        _geographic_label(location),
        flags=re.I,
    )
    return [
        part.strip()
        for part in re.split(r"\s+(?:and|or)\s+", label, flags=re.I)
        if part.strip()
    ]


def _display_region_values(component: str, target_field: str) -> set[str] | None:
    """Resolve a named region into the target filter family's exact canonical set."""
    region_scope = DISPLAY_REGION_FILTERS.get(_norm(component))
    if region_scope is None:
        return None
    source_field, values = region_scope
    if source_field == target_field:
        return set(values)
    if target_field == "countries" and source_field == "macro_regions":
        return {
            country
            for macro_region in values
            for country in MACRO_COUNTRIES.get(macro_region, [])
        }
    return None


def _comma_qualifier_matches(fields: dict[str, Any], component: str) -> bool:
    parts = [part.strip() for part in component.split(",") if part.strip()]
    if len(parts) != 2:
        return len(parts) <= 1 or len(parts) >= 3
    qualifier = parts[1]
    candidates = _state_candidates(qualifier)
    matching = [state for candidate_country, state in candidates if candidate_country == fields.get("country")]
    if matching:
        return len(matching) == 1 and _norm(matching[0]) == _norm(fields.get("state"))
    country = normalize_country(qualifier)
    return bool(get_macro_region(country)) and country == fields.get("country")


def _display_metros(components: list[str], *, allow_city_aliases: bool) -> set[str] | None:
    observed: set[str] = set()
    pending = list(components)
    while pending:
        component = pending.pop(0)
        direct = METRO_VOCABULARY.get(_norm(component))
        if direct:
            observed.add(_norm(direct))
            continue
        if not allow_city_aliases:
            return None
        fields = _location_fields(component)
        if "," in component and not _comma_qualifier_matches(fields, component):
            pending[0:0] = [part.strip() for part in component.split(",") if part.strip()]
            continue
        metros = {_norm(value) for value in fields.get("metro_areas") or [] if _norm(value)}
        if not metros and "," not in component:
            contexts = CITY_CONTEXTS.get(_norm(_display_city(component)), [])
            metros = {_norm(context["metro"]) for context in contexts if context.get("metro")}
        if not metros and "," in component:
            pending[0:0] = [part.strip() for part in component.split(",") if part.strip()]
            continue
        if not metros:
            return None
        observed.update(metros)
    return observed


def _display_countries(components: list[str]) -> set[str] | None:
    observed: set[str] = set()
    pending = list(components)
    while pending:
        component = pending.pop(0)
        region_values = _display_region_values(component, "countries")
        if region_values is not None:
            observed.update(_norm(value) for value in region_values)
            continue
        country = normalize_country(component)
        if not get_macro_region(country):
            if "," in component:
                pending[0:0] = [part.strip() for part in component.split(",") if part.strip()]
                continue
            return None
        observed.add(_norm(country))
    return observed


def _display_macro_regions(components: list[str]) -> set[str] | None:
    observed: set[str] = set()
    pending = list(components)
    while pending:
        component = pending.pop(0)
        region_values = _display_region_values(component, "macro_regions")
        if region_values is not None:
            observed.update(_norm(value) for value in region_values)
            continue
        canonical = MACRO_REGIONS.get(_norm(component))
        if not canonical:
            if "," in component:
                pending[0:0] = [part.strip() for part in component.split(",") if part.strip()]
                continue
            return None
        observed.add(_norm(canonical))
    return observed


def _display_cities(components: list[str], country: str) -> set[str] | None:
    observed: set[str] = set()
    pending = list(components)
    while pending:
        component = pending.pop(0)
        fields = _location_fields(component, country_hint=country)
        city = _display_city(component)
        parsed_country = str(fields.get("country") or "")
        parsed_state = str(fields.get("state") or "")
        contexts = CITY_CONTEXTS.get(_norm(city), [])
        known_countries = {
            context["country"]
            for context in contexts
            if context.get("country")
        }
        known_states = {_norm(context["state"]) for context in contexts if context.get("state")}
        if parsed_state and known_states and _norm(parsed_state) not in known_states:
            if "," in component:
                pending[0:0] = [part.strip() for part in component.split(",") if part.strip()]
                continue
            return None
        if parsed_country and parsed_country != country:
            return None
        if known_countries and country not in known_countries:
            return None
        observed.add(_norm(city))
    return observed


def _display_states(components: list[str], country: str) -> set[str] | None:
    observed: set[str] = set()
    pending = list(components)
    while pending:
        component = pending.pop(0)
        parts = [part.strip() for part in component.split(",") if part.strip()]
        if len(parts) > 1:
            qualifier = normalize_country(parts[-1])
            if qualifier != country:
                pending[0:0] = parts
                continue
            component = ",".join(parts[:-1]).strip()
        try:
            observed.add(_norm(_canonical_state(component, [country])))
        except ValueError:
            return None
    return observed


def _validate_display_consistency(
    location: str,
    filters: dict[str, list[str]],
    *,
    allow_city_to_metro: bool = False,
) -> None:
    if _norm(location) == _norm(canonical_location_label(filters)):
        return
    region_scope = DISPLAY_REGION_FILTERS.get(_norm(_geographic_label(location)))
    if region_scope is not None:
        field, region_values = region_scope
        if set(filters.get(field) or []) != region_values or len(filters) != 1:
            raise ValueError(f"approved location {location!r} requires {field} {sorted(region_values)}")
        return

    families = set(filters)
    alternatives = _display_alternatives(location)

    if families == {"metro_areas"}:
        observed = _display_metros(alternatives, allow_city_aliases=allow_city_to_metro)
        wanted = {_norm(value) for value in filters["metro_areas"]}
        if observed == wanted:
            return
    elif families == {"cities", "countries"}:
        observed = _display_cities(alternatives, filters["countries"][0])
        wanted_cities = {_norm(value) for value in filters["cities"]}
        if observed == wanted_cities:
            return
    elif families == {"states", "countries"}:
        observed = _display_states(alternatives, filters["countries"][0])
        wanted = {_norm(value) for value in filters["states"]}
        if observed == wanted:
            return
    elif families == {"countries"}:
        observed = _display_countries(alternatives)
        wanted = {_norm(value) for value in filters["countries"]}
        if observed == wanted:
            return
    elif families == {"macro_regions"}:
        observed = _display_macro_regions(alternatives)
        wanted = {_norm(value) for value in filters["macro_regions"]}
        if observed == wanted:
            return
    raise ValueError(
        f"approved location filters conflict with or broaden the reviewed location {location!r}"
    )


def validate_generated_location_display(location: str, filters: dict[str, list[str]]) -> None:
    """Validate draft display text before replacing it with the canonical Review label."""
    _validate_display_consistency(location, filters, allow_city_to_metro=True)


def candidate_location_fields(candidate: Any) -> dict[str, Any]:
    """Prefer backend-authoritative structured geo; parse display text only for legacy unions."""
    if isinstance(candidate, dict):
        nested = candidate.get("location_fields")
        if isinstance(nested, dict):
            structured = nested
        elif any(candidate.get(field) not in (None, "", [], {}) for field in (
            "city", "state", "country", "macro_region", "metro_areas",
        )):
            structured = candidate
        else:
            return _location_fields(candidate.get("location"))
        normalized = normalize_location_fields(
            city=structured.get("city"),
            state=structured.get("state"),
            country=structured.get("country"),
            metro_areas=structured.get("metro_areas"),
        )
        normalized["macro_region"] = str(
            structured.get("macro_region") or normalized.get("macro_region") or ""
        ).strip()
        return normalized
    return _location_fields(candidate)


def location_fit(required_filters: dict[str, list[str]] | None, candidate: Any) -> str:
    """Return match, mismatch, unknown, or not_required for the approved scope."""
    if not required_filters:
        return "not_required"
    if candidate in (None, ""):
        return "unknown"

    actual = candidate_location_fields(candidate)
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
