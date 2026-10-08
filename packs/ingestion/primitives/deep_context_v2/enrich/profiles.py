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

from pydantic import BaseModel, ConfigDict

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
        """The profile as both judges see it."""
        return {"public_identifier": self.public_identifier, "linkedin_url": self.linkedin_url,
                "full_name": self.full_name, "headline": self.headline, "experiences": list(self.experiences),
                "education": list(self.education), "location": self.location, "has_profile": True}


@dataclass(frozen=True)
class Profiles:
    found: dict[str, Profile]  # normalized URL -> its profile
    missing: int               # distinct URLs with no usable profile this run
    fetched: int               # distinct URLs fetched from RapidAPI this run


class CacheDate(BaseModel):
    year: int


class CacheExperience(BaseModel):
    """One position as the profile normalizer writes it: every key present, a value LinkedIn omitted is null."""

    title: str | None
    company_name: str | None
    starts_at: CacheDate | None
    ends_at: CacheDate | None


class CacheEducation(BaseModel):
    """One school. `school` is always written; the rest pass through from RapidAPI only when it sent them."""

    school: str | None
    starts_at: CacheDate | None = None
    ends_at: CacheDate | None = None
    schoolName: str | None = None
    degree: str | None = None
    fieldOfStudy: str | None = None


class CacheProfile(BaseModel):
    """A cache record's normalized_profile, in the keys the judges read."""

    member_id: str
    full_name: str
    headline: str
    location_str: str
    city: str
    state: str
    country: str
    experiences: list[CacheExperience]
    education: list[CacheEducation]


class ProfileRecord(BaseModel):
    """One file of the shared RapidAPI profile cache, as read_usable_cached_profile returns it."""

    model_config = ConfigDict(frozen=True)

    fetched_at: str
    normalized_profile: CacheProfile


def _text(value: str | None) -> str:
    return (value or "").strip()


def _year(value: CacheDate | None) -> str:
    if value is None:
        return ""
    return str(value.year)


def _experience(row: CacheExperience) -> str:
    line: str = f"{_text(row.title) or '?'} @ {_text(row.company_name) or '?'}"
    start: str = _year(row.starts_at)
    end: str = _year(row.ends_at)
    if start or end:
        line += f", {start}-{end or 'present'}"
    return line


def _education(row: CacheEducation) -> str:
    """"degree, field — school", from the cache's keys (schoolName, fieldOfStudy, degree)."""
    degree: list[str] = []
    for value in (row.degree, row.fieldOfStudy):
        if _text(value):
            degree.append(_text(value))
    school: str = _text(row.schoolName) or _text(row.school)
    if degree and school:
        return ", ".join(degree) + " — " + school
    if degree:
        return ", ".join(degree)
    return school


def profile_from_record(url: str, record: ProfileRecord) -> Profile:
    """A cache record as a Profile. The identity is the URL asked for; a renamed address answers with the
    same member id."""
    profile: CacheProfile = record.normalized_profile
    experiences: list[str] = []
    for row in profile.experiences:
        experiences.append(_experience(row))
    education: list[str] = []
    for school in profile.education:
        line: str = _education(school)
        if line:
            education.append(line)
    location: str = _text(profile.location_str)
    if not location:
        parts: list[str] = []
        for value in (profile.city, profile.state, profile.country):
            if _text(value):
                parts.append(_text(value))
        location = ", ".join(parts)
    return Profile(url, extract_public_identifier(url), _text(profile.member_id), _text(profile.full_name),
                   _text(profile.headline), location, tuple(experiences), tuple(education),
                   _text(record.fetched_at))


def read_profile_record(cache_dir: Path, public_id: str) -> ProfileRecord | None:
    """The cached record for one public identifier, parsed; None when the cache has no usable profile."""
    cached: dict[str, Any] | None = read_usable_cached_profile(profile_cache_path(cache_dir, public_id))
    if cached is None:
        return None
    return ProfileRecord.model_validate(cached)


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
        record: ProfileRecord | None = read_profile_record(cache_dir, public_id)
        if record is None and fetch:
            # The client writes what it fetched into the same cache; read it back from there.
            if client is None:
                load_env()  # the RapidAPI key
                client = RapidApiClient()
            client.get_profile(public_id, url, cache_dir=cache_dir)
            fetched += 1
            record = read_profile_record(cache_dir, public_id)
        if record is None or not _text(record.normalized_profile.member_id):
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
