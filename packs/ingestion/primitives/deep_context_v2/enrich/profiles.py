"""Step 3, profiles: the LinkedIn profile behind each URL a judge will see, cache first.

The cache is the shared RapidAPI profile cache, one JSON file per public identifier. A cached profile
that LinkedIn answered gives the member id: LinkedIn's own id, the same behind every address the person
has had. A URL with no usable cached profile is fetched once when fetching is on; otherwise, or when the
fetch fails, it is not judged this run and is counted as missing.

Created: 2026-10-07
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.enrich import proposals
from packs.ingestion.primitives.deep_context_v2.openai import load_env
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path, read_usable_cached_profile
from packs.ingestion.primitives.enrich.rapidapi_client import RapidApiClient
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url

CACHE_RELATIVE_DIR = Path("network-import") / "profile_cache_v2"


@dataclass(frozen=True)
class Profile:
    """One fetched LinkedIn profile, in the fields the judges read."""

    linkedin_url: str
    public_identifier: str
    member_id: str
    full_name: str
    headline: str
    location: str
    experiences: tuple[str, ...]  # "title @ company, start-end", newest first as LinkedIn lists them
    education: tuple[str, ...]    # "degree, field — school"
    fetched_at: str

    @property
    def real(self) -> bool:
        """A real profile: a name, and positions or a location."""
        return bool(self.full_name and (self.experiences or self.location))

    def judge_view(self) -> dict[str, Any]:
        return {"public_identifier": self.public_identifier, "linkedin_url": self.linkedin_url,
                "full_name": self.full_name, "headline": self.headline, "experiences": list(self.experiences),
                "education": list(self.education), "location": self.location, "has_profile": True}


@dataclass(frozen=True)
class Profiles:
    found: dict[str, Profile]  # normalized URL -> its profile
    missing: int               # distinct URLs with no usable profile this run
    fetched: int               # distinct URLs fetched from RapidAPI this run


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _year(value: object) -> str:
    if isinstance(value, dict):
        return _text(value.get("year"))
    return ""


def _experience(row: dict[str, Any]) -> str:
    line: str = f"{_text(row.get('title')) or '?'} @ {_text(row.get('company_name')) or '?'}"
    start: str = _year(row.get("starts_at"))
    end: str = _year(row.get("ends_at"))
    if start or end:
        line += f", {start}-{end or 'present'}"
    return line


def _education(row: dict[str, Any]) -> str:
    """"degree, field — school", from the cache's keys (schoolName, fieldOfStudy, degree)."""
    degree: list[str] = []
    for value in (row.get("degree"), row.get("fieldOfStudy")):
        if _text(value):
            degree.append(_text(value))
    school: str = _text(row.get("schoolName")) or _text(row.get("school"))
    if degree and school:
        return ", ".join(degree) + " — " + school
    if degree:
        return ", ".join(degree)
    return school


def profile_from_record(url: str, record: dict[str, Any]) -> Profile:
    """A cache record as a Profile. The identity is the URL asked for; a renamed address answers with the
    same member id."""
    profile: dict[str, Any] = record["normalized_profile"]
    experiences: list[str] = []
    for row in profile.get("experiences") or []:
        experiences.append(_experience(row))
    education: list[str] = []
    for row in profile.get("education") or []:
        line: str = _education(row)
        if line:
            education.append(line)
    location: str = _text(profile.get("location_str"))
    if not location:
        parts: list[str] = []
        for value in (profile.get("city"), profile.get("state"), profile.get("country")):
            if _text(value):
                parts.append(_text(value))
        location = ", ".join(parts)
    return Profile(url, extract_public_identifier(url), _text(profile.get("member_id")), _text(profile.get("full_name")),
                   _text(profile.get("headline")), location, tuple(experiences), tuple(education),
                   _text(record.get("fetched_at")))


def load_profiles(data_root: Path, urls: list[str], fetch: bool) -> Profiles:
    """Every URL's profile from the cache; with `fetch`, one RapidAPI call per URL the cache lacks."""
    cache_dir: Path = data_root / CACHE_RELATIVE_DIR
    distinct: list[str] = []
    for url in urls:
        normalized: str = normalize_linkedin_url(url)
        if normalized not in distinct:
            distinct.append(normalized)
    found: dict[str, Profile] = {}
    missing: int = 0
    fetched: int = 0
    client: RapidApiClient | None = None  # None = fetching is off, or nothing missed the cache yet
    for url in distinct:
        public_id: str = extract_public_identifier(url)
        record: dict[str, Any] | None = read_usable_cached_profile(profile_cache_path(cache_dir, public_id))
        if record is None and fetch:
            # The client writes what it fetched into the same cache; read it back from there.
            if client is None:
                load_env()  # the RapidAPI key
                client = RapidApiClient()
            client.get_profile(public_id, url, cache_dir=cache_dir)
            fetched += 1
            record = read_usable_cached_profile(profile_cache_path(cache_dir, public_id))
        if record is None or not _text(record["normalized_profile"].get("member_id")):
            missing += 1
            continue
        found[url] = profile_from_record(url, record)
    return Profiles(found, missing, fetched)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="07 Enrich step 3: profiles for every URL a judge will see.")
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--dry-run", action="store_true", help="count cache hits and misses; no RapidAPI call")
    parser.add_argument("--limit", type=int, default=None, help="fetch only the first N URLs the cache lacks")
    args = parser.parse_args(argv)
    urls: list[str] = proposals.all_urls(proposals.derive(open_store(store_path(args.data_root))))
    cached: Profiles = load_profiles(args.data_root, urls, fetch=False)
    if args.dry_run:
        print(json.dumps({"urls": len(cached.found) + cached.missing, "cached": len(cached.found),
                          "missing": cached.missing}, indent=2))
        return 0
    # The paid run: fetch the misses, the first --limit of them.
    misses: list[str] = []
    for url in urls:
        if normalize_linkedin_url(url) not in cached.found:
            misses.append(url)
    if args.limit is not None:
        misses = misses[: args.limit]
    result: Profiles = load_profiles(args.data_root, misses, fetch=True)
    print(json.dumps({"fetched": result.fetched, "found": len(result.found), "missing": result.missing}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
