#!/usr/bin/env python3
"""Fill the SQLite review queue's local LinkedIn profile cache.

The review UI hydrates its cards from the cached normalized profile itself, so
this stage only ensures that cache entry exists.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.imports.common import write_manifest
from packs.ingestion.primitives.deep_context.common import (
    ENRICH_MANIFEST,
    CANONICAL_DB,
    PROFILE_CACHE_DIR,
    ROOT,
    emit,
    load_env,
)
from packs.ingestion.primitives.deep_context.db.identity_views import linkedin_review
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.enrichment_receipt import EnrichmentReceipt
from packs.ingestion.primitives.deep_context.profile_projection import (
    hydrate_profiles,
    profile_payloads,
    provider_key_available,
)
from packs.ingestion.schemas.people_schema import extract_public_identifier

STAGE = "profile-prefetch"
RAPIDAPI_RPM_DEFAULT = 300
DEFAULT_FETCH_CONCURRENCY = 40


def review_queue_links(parents: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen, links = set(), []
    for parent in parents:
        for candidate in parent.get("candidates") or []:
            if candidate.get("synthetic"):
                continue
            url = str(candidate.get("url") or "").strip()
            pub = (str(candidate.get("profile_pub") or "").strip().lower()
                   or extract_public_identifier(url).lower()
                   or str(candidate.get("pub") or "").strip().lower())
            if not pub or pub.startswith("candidate:") or pub in seen:
                continue
            seen.add(pub)
            links.append({
                "public_identifier": pub,
                "linkedin_url": url or f"https://www.linkedin.com/in/{pub}",
                "name": str(parent.get("name") or ""),
                "parent_id": str(parent.get("parent_id") or ""),
                "candidate_key": str(candidate.get("key") or "").lower(),
            })
    return links


def _pub(link: dict[str, str]) -> str:
    return str(link.get("public_identifier") or "").strip().lower()


def classify_queue(
    links: list[dict[str, str]], profiles: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    """Partition links from projected profile payloads, never cache files."""
    buckets: dict[str, list[dict[str, str]]] = {"fetch": [], "cached": [], "no_pub": []}
    for link in links:
        pub = _pub(link)
        if not pub:
            buckets["no_pub"].append(link)
        elif (
            profiles.get(link["candidate_key"], {}).get("normalized_profile") or {}
        ).get("success") is True:
            buckets["cached"].append(link)
        else:
            buckets["fetch"].append(link)
    return buckets


def prefetch(
    misses: list[dict[str, str]], cache_dir: Path, *, db: Db | None = None, limit: int = 0,
    concurrency: int = DEFAULT_FETCH_CONCURRENCY, rpm: int = RAPIDAPI_RPM_DEFAULT,
    on_result: Callable[[dict[str, str], dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    targets = misses[:limit] if limit else misses
    counts = {"fetched": 0, "from_cache": 0, "failed": 0, "attempted": len(targets)}
    if not targets:
        return counts
    def record(_link: dict[str, str], result: dict[str, Any]) -> None:
        if on_result is not None:
            on_result(_link, result)
        if (result.get("normalized_profile") or {}).get("success") is True:
            counts["from_cache" if result.get("from_cache") else "fetched"] += 1
        else:
            counts["failed"] += 1

    hydrate_profiles(
        targets,
        cache_dir,
        db=db,
        max_workers=concurrency,
        max_per_minute=rpm,
        on_result=record,
    )
    return counts


class PrefetchProfiles:
    """Fetch missing review profiles, then finish the enrichment receipt."""

    name = "deep_prefetch"

    def __init__(
        self, *, db: Db, profile_cache_dir: Path | None = None,
        fetch: bool = False, limit: int = 0,
        fetch_concurrency: int = DEFAULT_FETCH_CONCURRENCY,
        rapidapi_rpm: int = RAPIDAPI_RPM_DEFAULT,
        enrichment_manifest: Path | None = None,
    ) -> None:
        self.db, self.profile_cache_dir = db, Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.fetch, self.limit = fetch, limit
        self.fetch_concurrency, self.rapidapi_rpm = fetch_concurrency, rapidapi_rpm
        self.enrichment_manifest = Path(enrichment_manifest or ENRICH_MANIFEST)

    def run(self) -> dict[str, Any]:
        payload = self.execute()
        write_manifest(STAGE, payload, import_dir=ROOT)
        return payload

    def execute(self) -> dict[str, Any]:
        started = time.monotonic()
        cache = self.profile_cache_dir
        links = review_queue_links(linkedin_review(self.db, "queue"))
        before = classify_queue(links, profile_payloads(canonical_snapshot(self.db)))
        fetch_misses, no_pub = before["fetch"], before["no_pub"]
        payload: dict[str, Any] = {
            "status": "", "source": STAGE, "queue_links": len(links),
            "cache_misses": len(fetch_misses), "already_cached": len(before["cached"]),
            "no_public_identifier": len(no_pub),
            "estimated_rapidapi_calls": len(fetch_misses),
            "missing_public_identifiers": sorted(_pub(link) for link in fetch_misses),
            "fetch_concurrency": max(1, self.fetch_concurrency), "rapidapi_rpm": self.rapidapi_rpm,
            "profile_cache_dir": str(cache),
            "privacy": {"message_bodies_read": False, "network_called": bool(self.fetch),
                        "paid_provider_called": bool(self.fetch)},
        }
        if not self.fetch:
            payload["status"] = "dry_run"
            payload["note"] = (
                f"dry run: {len(fetch_misses)} fetch miss(es) would cost ~{len(fetch_misses)} "
                "RapidAPI call(s); rerun with --fetch to spend"
            )
        elif fetch_misses and not provider_key_available():
            payload["status"] = "blocked_no_key"
            payload["privacy"].update(network_called=False, paid_provider_called=False)
            payload["note"] = "RAPIDAPI_LINKEDIN_KEY / RAPIDAPI_KEY not configured; nothing fetched"
        else:
            counts = prefetch(
                fetch_misses,
                cache,
                db=self.db,
                limit=self.limit,
                concurrency=max(1, self.fetch_concurrency),
                rpm=self.rapidapi_rpm,
            )
            counts["already_cached"] = payload["already_cached"]
            payload["counts"] = counts
            after = classify_queue(links, profile_payloads(canonical_snapshot(self.db)))
            payload["remaining_misses"] = len(after["fetch"])
            payload["status"] = "completed_with_failures" if counts["failed"] else "completed"
        payload["duration_seconds"] = round(time.monotonic() - started, 2)
        if self.fetch and payload["status"] in {"completed", "completed_with_failures"}:
            failed = payload["status"] == "completed_with_failures"
            receipt = {
                "stage": "enrich",
                "status": "completed_with_errors" if failed else "completed",
                "phase": "profiles_complete", "prefetch": payload,
            }
            if failed:
                receipt["error"] = "profile prefetch completed with failures"
            EnrichmentReceipt(self.enrichment_manifest).write(receipt)
        return payload


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-cache-dir", default=str(PROFILE_CACHE_DIR))
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--fetch-concurrency", type=int, default=DEFAULT_FETCH_CONCURRENCY)
    parser.add_argument("--rapidapi-rpm", type=int, default=RAPIDAPI_RPM_DEFAULT)
    args = parser.parse_args(argv)
    load_env()
    payload = PrefetchProfiles(
        db=open_existing_db(args.db), profile_cache_dir=Path(args.profile_cache_dir),
        fetch=args.fetch, limit=args.limit, fetch_concurrency=args.fetch_concurrency,
        rapidapi_rpm=args.rapidapi_rpm,
    ).run()
    emit(payload)


if __name__ == "__main__":
    main()
