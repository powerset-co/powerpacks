#!/usr/bin/env python3
"""Offline RapidAPI profile prefetch for the Check-Profile review queue.

The review UI is cache-only: it renders whatever the local profile cache holds
and never calls a provider. This stage fills that cache ahead of review — it
scans exactly the population the Check-Profile stage will render (attached /
kept links plus pending retarget proposals), diffs against the profile cache,
and fetches each miss ONCE through the same cache-first RapidAPI primitive
apply_retargets uses (the primitive writes the cache, so reruns are idempotent
and each person costs at most one paid call ever).

Default is a spend-free dry run reporting miss counts and the estimated number
of RapidAPI calls; pass ``--fetch`` to actually fetch (``--limit N`` to cap).
Output is this stage's fixed manifest — no ledgers, no run ids.

Run: uv run --project . python -m packs.ingestion.primitives.deep_context.prefetch_profiles
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    FACTS_DIR,
    LINKEDIN_OVERRIDES_CSV,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    ROOT,
    VERDICTS_JSONL,
    emit,
    load_env,
)
from packs.ingestion.primitives.deep_context.reconcile_review_web import (
    SYNTHETIC_PEOPLE_CSV,
    _all_review_parents,
    pending_linkedin_candidates,
)
from packs.ingestion.primitives.enrich_people.enrich_people import (
    profile_cache_path,
    rapidapi_key,
    rapidapi_profile,
    read_usable_cached_profile,
)
from packs.ingestion.primitives.import_contacts_pipeline.common import write_manifest
from packs.ingestion.schemas.people_schema import extract_public_identifier

STAGE = "profile-prefetch"


def review_queue_links(parents: list[dict[str, Any]]) -> list[dict[str, str]]:
    """One (pub, url, name) per real LinkedIn the Check-Profile queue will show.

    Mirrors the review UI's own queue: every pending identity candidate of every
    queued parent, skipping synthetic profiles (no LinkedIn to fetch) and bare
    import-candidate ids (not LinkedIn public identifiers)."""
    seen: set[str] = set()
    links: list[dict[str, str]] = []
    for parent in parents:
        for candidate in pending_linkedin_candidates(parent):
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
            })
    return links


def cache_misses(links: list[dict[str, str]], cache_dir: Path) -> list[dict[str, str]]:
    """Queue links with no usable cached profile (the fetch population)."""
    return [link for link in links
            if not read_usable_cached_profile(
                profile_cache_path(cache_dir, link["public_identifier"]))]


def prefetch(misses: list[dict[str, str]], cache_dir: Path, api_key: str,
             *, limit: int = 0) -> dict[str, int]:
    """Fetch each miss once via the cache-first primitive (which writes the
    cache); counts only — the cache files are the durable output."""
    counts = {"fetched": 0, "from_cache": 0, "failed": 0, "attempted": 0}
    for link in (misses[:limit] if limit else misses):
        counts["attempted"] += 1
        result = rapidapi_profile(link["public_identifier"], link["linkedin_url"],
                                  api_key, cache_dir=cache_dir)
        if (result.get("normalized_profile") or {}).get("success") is True:
            counts["from_cache" if result.get("from_cache") else "fetched"] += 1
        else:
            counts["failed"] += 1
    return counts


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    cache_dir = Path(args.profile_cache_dir)
    parents = _all_review_parents(
        Path(args.verdicts), Path(args.review), Path(args.synthetic_people),
        Path(args.facts_dir), Path(args.people_csv),
        Path(args.parents_dir), Path(args.dossier_dir), cache_dir)
    links = review_queue_links(parents)
    misses = cache_misses(links, cache_dir)
    payload: dict[str, Any] = {
        "queue_links": len(links),
        "cache_misses": len(misses),
        "estimated_rapidapi_calls": len(misses),
        "missing_public_identifiers": sorted(link["public_identifier"] for link in misses),
        "profile_cache_dir": str(cache_dir),
        "privacy": {"message_bodies_read": False,
                    "network_called": bool(args.fetch),
                    "paid_provider_called": bool(args.fetch)},
    }
    if not args.fetch:
        payload["status"] = "dry_run"
        payload["note"] = (f"dry run: {len(misses)} cache miss(es) would cost "
                           f"~{len(misses)} RapidAPI call(s); rerun with --fetch to spend")
    elif not rapidapi_key():
        payload["status"] = "blocked_no_key"
        payload["privacy"]["network_called"] = False
        payload["privacy"]["paid_provider_called"] = False
        payload["note"] = "RAPIDAPI_LINKEDIN_KEY / RAPIDAPI_KEY not configured; nothing fetched"
    else:
        counts = prefetch(misses, cache_dir, rapidapi_key(), limit=args.limit)
        payload["counts"] = counts
        payload["remaining_misses"] = len(cache_misses(links, cache_dir))
        payload["status"] = "completed" if not counts["failed"] else "completed_with_failures"
    payload["duration_seconds"] = round(time.monotonic() - started, 2)
    manifest = write_manifest(STAGE, payload, import_dir=ROOT)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verdicts", default=str(VERDICTS_JSONL))
    parser.add_argument("--review", default=str(LINKEDIN_OVERRIDES_CSV))
    parser.add_argument("--synthetic-people", default=str(SYNTHETIC_PEOPLE_CSV))
    parser.add_argument("--facts-dir", default=str(FACTS_DIR))
    parser.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    parser.add_argument("--parents-dir", default=str(PARENTS_DIR))
    parser.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    parser.add_argument("--profile-cache-dir", default=str(PROFILE_CACHE_DIR))
    parser.add_argument("--fetch", action="store_true",
                        help="actually fetch cache misses (spends RapidAPI credits); "
                             "default is a spend-free dry run")
    parser.add_argument("--limit", type=int, default=0,
                        help="cap the number of fetches (0 = all misses)")
    args = parser.parse_args()
    load_env()
    emit(run(args))


if __name__ == "__main__":
    main()
