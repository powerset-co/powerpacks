"""Canonical seniority-band parsing for pinned retrieval filters.

The JD/profile search flow (packs/search/skills/search-profile) derives
canonical seniority bands from a job description's explicit level language and
pins them on every profile search so retrieval — local DuckDB and prod
TurboPuffer alike — only returns in-band candidates instead of relying on the
final evaluation gate.

Canonical band values must match VALID_SENIORITY_BANDS in
packs/indexing/primitives/enrich_roles_checkpointed/enrich_roles_checkpointed.py
(hyphenated spellings). Normalization mirrors _normalize_seniority_bands in
packs/search/primitives/expand_search_request/parallel_extractors.py: lowercase,
collapse separators to underscores, then map the two underscore aliases back to
the index's hyphenated forms. The local index also contains legacy bootstrap
values `senior_ic` and `ic`; the expansion validator only warns on those, so we
accept them when explicitly requested but never emit them ourselves.

This module is dependency-free on purpose so both pipeline orchestrators can
import it without pulling in TurboPuffer/OpenAI client code.
"""

from __future__ import annotations

from typing import Any

CANONICAL_SENIORITY_BANDS = [
    "owner", "partner", "c-suite", "vice-president", "director",
    "principal", "staff", "manager", "senior", "mid", "junior", "entry", "trainee",
]

# Bootstrap-era band values still present in indexed positions. Accepted when a
# caller pins them explicitly; not part of the canonical vocabulary.
LEGACY_INDEX_SENIORITY_BANDS = ["senior_ic", "ic"]

# Underscore spellings normalize to the index's hyphenated canonical values —
# same mapping as _SENIORITY_CANONICAL in expand_search_request.
_CANONICAL_ALIASES = {
    "c_suite": "c-suite",
    "vice_president": "vice-president",
}

_ACCEPTED = set(CANONICAL_SENIORITY_BANDS) | set(LEGACY_INDEX_SENIORITY_BANDS)


def normalize_seniority_band(value: Any) -> str:
    """Collapse one band spelling to its canonical index value.

    Raises ValueError for values not in the canonical list (or the documented
    legacy index values), so typos fail loudly instead of silently filtering
    to zero results.
    """
    collapsed = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    canonical = _CANONICAL_ALIASES.get(collapsed, collapsed)
    if canonical not in _ACCEPTED:
        raise ValueError(
            f"unknown seniority band {value!r}; valid bands: "
            f"{', '.join(CANONICAL_SENIORITY_BANDS)} "
            f"(legacy index values also accepted: {', '.join(LEGACY_INDEX_SENIORITY_BANDS)})"
        )
    return canonical


def parse_pinned_seniority_bands(raw: str) -> list[str]:
    """Parse a comma-separated --seniority-bands value into canonical bands."""
    bands: list[str] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        band = normalize_seniority_band(part)
        if band not in bands:
            bands.append(band)
    if not bands:
        raise ValueError("--seniority-bands needs at least one band (or omit the flag for no seniority filter)")
    return bands


def pin_payload_seniority_bands(payload: dict[str, Any], bands: list[str]) -> dict[str, Any]:
    """Return a copy of an expand_search_request payload with pinned bands.

    Override semantics: the pinned bands REPLACE whatever query expansion
    emitted. The pin comes from the JD's explicit level language (a hard hiring
    constraint), while expansion infers bands from one profile query's phrasing;
    intersecting the two could silently produce an empty filter and zero
    retrieval. `seniority_bands_pinned` marks the filter so role shortcuts
    (founder queries normally drop seniority bands for recall) keep the pin.
    """
    out = dict(payload)
    filters = out.get("role_search_filters")
    filters = dict(filters) if isinstance(filters, dict) else {}
    previous = filters.get("seniority_bands")
    filters["seniority_bands"] = list(bands)
    filters["seniority_bands_pinned"] = True
    out["role_search_filters"] = filters
    notes = out.get("notes")
    notes = list(notes) if isinstance(notes, list) else []
    note = f"seniority_bands pinned via --seniority-bands: {', '.join(bands)}"
    if previous and list(previous) != list(bands):
        note += f" (replaced expansion bands: {', '.join(str(item) for item in previous)})"
    if note not in notes:
        notes.append(note)
    out["notes"] = notes
    return out
