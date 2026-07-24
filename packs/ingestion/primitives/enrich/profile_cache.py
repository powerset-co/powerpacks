#!/usr/bin/env python3
"""Local LinkedIn profile cache: slugs, lookups, TTL'd failures, classification.

The one home for the on-disk profile cache the RapidAPI enrichment path reads
and seeds (default dir `.powerpacks/network-import/profile_cache_v2`; the
fetch-and-write side lives in `rapidapi_client.rapidapi_profile`).

Cache seeding format: one JSON file per sanitized LinkedIn public identifier,
e.g. `profile_cache_v2/jane-example.json` containing `fetched_at`,
`public_identifier`, `linkedin_url`, `raw_response`, and
`normalized_profile: {"success": true}`. Usable entries enrich without
RAPIDAPI_* keys. Failed lookups are cached with `last_checked_at` and retried
only after the TTL (`recent_cached_failure`).

- `cache_slug_candidates` / `profile_cache_path` / `indexed_profile_cache_path` —
  identifier -> cache-file paths, including the legacy byte-escaped slug form.
- `profile_cache_index` — one-shot directory listing so per-row existence checks
  don't stat a (possibly network) filesystem file-by-file.
- `read_usable_cached_profile` — a successful entry, re-normalizing legacy
  raw-payload files on the fly.
- `recent_cached_failure` — a failed entry still inside its retry TTL.
- `cached_profile_from_row` — a successful payload embedded in a people row's
  `rapidapi_response*` columns.
- `classify_rapidapi_cache_status` — hit / miss / recent_failure for one row.
- `count_rapidapi_cache_misses` — row count of a cache-misses CSV (spend gate
  estimates).

Changelog:
  2026-07-23 (audit decomposition): split out of enrich_people.py verbatim,
    minus dead weight: `cached_profile_from_row` dropped its two unused
    public_identifier/linkedin_url parameters.
"""

from __future__ import annotations

import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import read_json  # noqa: E402
from packs.ingestion.schemas.linkedin_profile_normalizer import normalize_linkedin_profile  # noqa: E402
from packs.ingestion.schemas.people_schema import extract_public_identifier, parse_jsonish  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def safe_cache_slug(public_identifier: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in public_identifier.lower().strip())
    return cleaned.strip("._")


def legacy_byte_cache_slug(public_identifier: str) -> str:
    parts: list[str] = []
    for ch in public_identifier.lower().strip():
        if ch.isascii() and (ch.isalnum() or ch in {"-", "_", "."}):
            parts.append(ch)
        elif ch.isascii():
            parts.append("_")
        else:
            parts.extend(f"_{byte:02x}" for byte in ch.encode("utf-8"))
    return "".join(parts).strip("._")


def cache_slug_candidates(public_identifier: str) -> list[str]:
    values = [
        public_identifier,
        urllib.parse.unquote(public_identifier or ""),
    ]
    slugs: list[str] = []
    for value in values:
        for slug in (value.lower().strip(), safe_cache_slug(value), legacy_byte_cache_slug(value)):
            if slug and slug not in slugs:
                slugs.append(slug)
    return slugs


def profile_cache_index(cache_dir: Path | str | None) -> set[str]:
    if not cache_dir:
        return set()
    root = Path(cache_dir)
    if not root.exists() or not root.is_dir():
        return set()
    return {path.stem for path in root.glob("*.json") if path.name != "_metadata.json"}


def profile_cache_path(cache_dir: Path | str | None, public_identifier: str) -> Path | None:
    slug = cache_slug_candidates(public_identifier)[0] if public_identifier else ""
    if not cache_dir or not slug:
        return None
    return Path(cache_dir) / f"{slug}.json"


def indexed_profile_cache_path(cache_dir: Path | str | None, public_identifier: str, cache_index: set[str] | None) -> Path | None:
    if not cache_dir:
        return None
    if cache_index is not None:
        for slug in cache_slug_candidates(public_identifier):
            if slug in cache_index:
                return Path(cache_dir) / f"{slug}.json"
        return profile_cache_path(cache_dir, public_identifier)
    return profile_cache_path(cache_dir, public_identifier)


def read_usable_cached_profile(cache_path: Path | None) -> dict[str, Any] | None:
    if not cache_path or not cache_path.exists():
        return None
    cached = read_json(cache_path, None)
    if not isinstance(cached, dict):
        return None
    normalized = cached.get("normalized_profile")
    raw = cached.get("raw_response")
    if isinstance(normalized, dict) and normalized.get("success") is True and isinstance(raw, dict):
        return cached
    normalized = normalize_linkedin_profile(cached)
    if normalized.get("success") is True:
        return {
            "fetched_at": cached.get("fetched_at") or cached.get("last_checked_at") or "",
            "last_checked_at": cached.get("last_checked_at") or cached.get("fetched_at") or "",
            "public_identifier": cached.get("public_identifier") or normalized.get("public_identifier") or cache_path.stem,
            "linkedin_url": cached.get("linkedin_url") or normalized.get("linkedin_url") or "",
            "raw_response": cached,
            "normalized_profile": normalized,
        }
    return None


def recent_cached_failure(cache_path: Path | None, retry_hours: float) -> dict[str, Any] | None:
    if not cache_path or not cache_path.exists() or retry_hours <= 0:
        return None
    cached = read_json(cache_path, None)
    if not isinstance(cached, dict):
        return None
    normalized = cached.get("normalized_profile")
    if not isinstance(normalized, dict) or normalized.get("success") is not False:
        return None
    checked_at = parse_iso(str(cached.get("last_checked_at") or cached.get("fetched_at") or ""))
    if checked_at is None:
        return None
    retry_after = checked_at + timedelta(hours=retry_hours)
    if datetime.now(timezone.utc) >= retry_after:
        return None
    result = dict(cached)
    result["retry_after"] = retry_after.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return result


def cached_profile_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    for col in ("rapidapi_response_enriched", "rapidapi_response"):
        parsed = parse_jsonish(row.get(col), None)
        if isinstance(parsed, dict) and normalize_linkedin_profile(parsed).get("success") is True:
            return parsed
    return None


def classify_rapidapi_cache_status(
    row: dict[str, str],
    profile_cache_dir: Path,
    refresh_cache: bool,
    retry_hours: float,
    cache_index: set[str] | None = None,
) -> tuple[str, str, Path | None, dict[str, Any] | None]:
    public_identifier = row.get("public_identifier") or extract_public_identifier(row.get("linkedin_url") or "")
    cache_path = indexed_profile_cache_path(profile_cache_dir, public_identifier, cache_index)
    if refresh_cache:
        return "miss", "refresh requested", cache_path, None
    if cached_profile_from_row(row) is not None:
        return "hit", "input rapidapi_response", cache_path, None
    cached_file_exists = bool(cache_path and cache_path.exists())
    if cache_index is not None:
        cached_file_exists = any(slug in cache_index for slug in cache_slug_candidates(public_identifier))
    if cached_file_exists and read_usable_cached_profile(cache_path):
        return "hit", "profile cache", cache_path, None
    recent_failure = recent_cached_failure(cache_path, retry_hours)
    if recent_failure:
        return "recent_failure", "recent provider failure", cache_path, recent_failure
    if cached_file_exists:
        return "miss", "cache entry unusable", cache_path, None
    return "miss", "no usable cache", cache_path, None


def count_rapidapi_cache_misses(cache_misses_csv: Path) -> int:
    if not cache_misses_csv.exists():
        return 0
    return len(CsvIO.read_dict_rows(cache_misses_csv))
