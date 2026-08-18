#!/usr/bin/env python3
"""Hydrate the distinct LinkedIn profiles needed by the review queue."""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_queue
from packs.ingestion.primitives.deep_context.db.people_views import ParentViewRow
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.enrich.profiles.models import (
    ProfileResult,
    ProfileTarget,
)
from packs.ingestion.primitives.deep_context.enrich.profiles.projection import (
    hydrate_profiles,
    profile_payloads,
)
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    PROFILE_CACHE_DIR,
    emit,
    load_env,
)
from packs.ingestion.schemas.people_schema import extract_public_identifier

RAPIDAPI_RPM_DEFAULT = 300
DEFAULT_FETCH_CONCURRENCY = 40


@dataclass(frozen=True)
class ProfileQueue:
    fetch: tuple[ProfileTarget, ...]
    cached: tuple[ProfileTarget, ...]


@dataclass(frozen=True)
class ProfilePrefetchCounts:
    attempted: int
    fetched: int
    from_cache: int
    failed: int
    skipped_no_key: int
    network_calls: int


@dataclass(frozen=True)
class ProfilePrefetchResult:
    status: str
    queue_links: int
    distinct_profiles: int
    cache_misses: int
    already_cached: int
    estimated_rapidapi_calls: int
    remaining_misses: int
    duration_seconds: float
    counts: ProfilePrefetchCounts | None = None
    note: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "source": "profile-prefetch",
            "queue_links": self.queue_links,
            "distinct_profiles": self.distinct_profiles,
            "cache_misses": self.cache_misses,
            "already_cached": self.already_cached,
            "estimated_rapidapi_calls": self.estimated_rapidapi_calls,
            "remaining_misses": self.remaining_misses,
            "duration_seconds": self.duration_seconds,
        }
        if self.counts:
            payload["counts"] = {
                "attempted": self.counts.attempted,
                "fetched": self.counts.fetched,
                "from_cache": self.counts.from_cache,
                "failed": self.counts.failed,
                "skipped_no_key": self.counts.skipped_no_key,
                "network_calls": self.counts.network_calls,
            }
        if self.note:
            payload["note"] = self.note
        return payload


def review_queue_links(parents: list[ParentViewRow]) -> list[ProfileTarget]:
    """Return every real candidate; hydration deduplicates shared profiles."""
    links: list[ProfileTarget] = []
    for parent in parents:
        for candidate in parent.candidates:
            if candidate.synthetic:
                continue
            url = candidate.url.strip()
            public_identifier = (
                candidate.profile_pub.strip().lower()
                or extract_public_identifier(url).lower()
                or candidate.pub.strip().lower()
            )
            if not public_identifier or public_identifier.startswith("candidate:"):
                continue
            links.append(
                ProfileTarget(
                    public_identifier,
                    url or f"https://www.linkedin.com/in/{public_identifier}",
                    candidate.row_key.lower(),
                    parent.parent_id,
                )
            )
    return links


def classify_queue(
    links: list[ProfileTarget],
    profiles: dict[str, ProfileResult],
) -> ProfileQueue:
    """Partition candidate links from projected typed profile payloads."""
    fetch: list[ProfileTarget] = []
    cached: list[ProfileTarget] = []
    for link in links:
        profile = profiles.get(link.candidate_key)
        (cached if profile and profile.normalized_profile.present else fetch).append(link)
    return ProfileQueue(tuple(fetch), tuple(cached))


def prefetch(
    misses: tuple[ProfileTarget, ...] | list[ProfileTarget],
    cache_dir: Path,
    *,
    db: Db | None = None,
    concurrency: int = DEFAULT_FETCH_CONCURRENCY,
    rpm: int = RAPIDAPI_RPM_DEFAULT,
) -> ProfilePrefetchCounts:
    """Hydrate each distinct public identifier once and report actual calls."""
    hydration = hydrate_profiles(
        misses,
        cache_dir,
        db=db,
        max_workers=concurrency,
        max_per_minute=rpm,
    )
    profiles = tuple(hydration.profiles.values())
    return ProfilePrefetchCounts(
        attempted=hydration.wanted,
        fetched=sum(
            bool(result.normalized_profile.present and not result.from_cache)
            for result in profiles
        ),
        from_cache=sum(
            bool(result.normalized_profile.present and result.from_cache)
            for result in profiles
        ),
        failed=hydration.failed,
        skipped_no_key=hydration.skipped_no_key,
        network_calls=sum(bool(result.fetched) for result in profiles),
    )


@dataclass(frozen=True)
class PrefetchProfiles:
    """Construct and run one typed profile-cache preparation stage."""

    db: Db
    profile_cache_dir: Path = PROFILE_CACHE_DIR
    fetch: bool = False
    fetch_concurrency: int = DEFAULT_FETCH_CONCURRENCY
    rapidapi_rpm: int = RAPIDAPI_RPM_DEFAULT

    def run(self) -> ProfilePrefetchResult:
        started = time.monotonic()
        links = review_queue_links(linkedin_queue(self.db))
        before = classify_queue(links, profile_payloads(self.db))
        misses = before.fetch
        distinct = len({link.public_identifier for link in links})
        distinct_misses = len({link.public_identifier for link in misses})

        if not self.fetch:
            return ProfilePrefetchResult(
                status="dry_run",
                queue_links=len(links),
                distinct_profiles=distinct,
                cache_misses=len(misses),
                already_cached=len(before.cached),
                estimated_rapidapi_calls=distinct_misses,
                remaining_misses=len(misses),
                duration_seconds=round(time.monotonic() - started, 2),
                note=(
                    f"dry run: {distinct_misses} distinct profile miss(es) "
                    "would call RapidAPI"
                ),
            )

        counts = prefetch(
            misses,
            self.profile_cache_dir,
            db=self.db,
            concurrency=max(1, self.fetch_concurrency),
            rpm=self.rapidapi_rpm,
        )
        remaining = len(classify_queue(links, profile_payloads(self.db)).fetch)
        if counts.skipped_no_key:
            status = "blocked_no_key"
            note = "RAPIDAPI_LINKEDIN_KEY / RAPIDAPI_KEY is not configured"
        elif counts.failed:
            status = "completed_with_failures"
            note = "profile prefetch completed with failures"
        else:
            status = "completed"
            note = None
        return ProfilePrefetchResult(
            status=status,
            queue_links=len(links),
            distinct_profiles=distinct,
            cache_misses=len(misses),
            already_cached=len(before.cached),
            estimated_rapidapi_calls=distinct_misses,
            remaining_misses=remaining,
            duration_seconds=round(time.monotonic() - started, 2),
            counts=counts,
            note=note,
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-cache-dir", default=str(PROFILE_CACHE_DIR))
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--fetch-concurrency", type=int, default=DEFAULT_FETCH_CONCURRENCY)
    parser.add_argument("--rapidapi-rpm", type=int, default=RAPIDAPI_RPM_DEFAULT)
    args = parser.parse_args(argv)
    load_env()
    result = PrefetchProfiles(
        db=open_existing_db(args.db),
        profile_cache_dir=Path(args.profile_cache_dir),
        fetch=args.fetch,
        fetch_concurrency=args.fetch_concurrency,
        rapidapi_rpm=args.rapidapi_rpm,
    ).run()
    emit(result.to_payload())


if __name__ == "__main__":
    main()
