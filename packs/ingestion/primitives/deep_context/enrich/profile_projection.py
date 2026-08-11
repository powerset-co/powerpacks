"""Project and hydrate RapidAPI profile payloads once for all consumers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    ProjectionStatus,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.enrich.profile_models import (
    ProfileHydration,
    ProfileResult,
    ProfileTarget,
)
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path
from packs.ingestion.primitives.enrich import rapidapi_client

PROFILE_CONTENT = rapidapi_client.PROFILE_CONTENT
PROFILE_EMPTY = rapidapi_client.PROFILE_EMPTY
PROFILE_ERROR = rapidapi_client.PROFILE_ERROR


def provider_key_available() -> bool:
    """Whether paid profile hydration can run in this process."""
    return bool(rapidapi_client.RapidApiClient.resolve_key())


def profile_payloads(
    db: Db,
    candidate_keys: Iterable[str] | None = None,
) -> dict[str, ProfileResult]:
    """Read normalized profiles from projected profile artifacts only.

    This is the SQLite copy of a fact the on-disk `profile_cache_v2/*.json`
    cache (read by `rapidapi_client`) also holds; `project_profile_results`
    below is what keeps the two in sync after a fetch."""
    profiles: dict[str, ProfileResult] = {}
    selected_keys = tuple(candidate_keys) if candidate_keys is not None else None
    # kind AND status filter in SQL — the loop used to re-test both in Python
    # (plus candidate_key, which project_profile_results raises on rather than
    # ever writing empty), which read as three guards over a query that had
    # already answered one of them.
    for artifact in queries.artifacts(
        db,
        kind=ArtifactKind.PROFILE.value,
        status=ProjectionStatus.PROJECTED.value,
        candidate_keys=selected_keys,
    ):
        try:
            payload = json.loads(artifact.payload_json or "")
        except json.JSONDecodeError:
            # A corrupt payload silently drops this one candidate's cache
            # entry rather than failing every other profile in the queue —
            # it re-enters classify_queue's fetch list on the next pass.
            continue
        if not isinstance(payload, dict):
            continue
        profile: object = payload.get("normalized_profile")
        profile = profile if isinstance(profile, dict) else {}
        profiles[artifact.candidate_key] = ProfileResult.from_payload(
            str(payload.get("public_identifier") or profile.get("public_identifier") or ""),
            str(payload.get("linkedin_url") or profile.get("linkedin_url") or ""),
            payload,
        )
    return profiles


def project_profile_results(
    db: Db,
    results: Iterable[tuple[ProfileTarget, ProfileResult]],
    cache_dir: Path,
) -> None:
    """Project one artifact per (candidate_key, result) pair — NOT deduped by
    public_identifier. The same real profile fetched for two candidate rows
    (e.g. one person in two parent families) yields two SQLite artifacts with
    identical payload_json; only the on-disk cache file at `profile_cache_path`
    (keyed by public_identifier) is actually shared across them."""
    artifacts = []
    for target, result in results:
        if not target.public_identifier or not target.candidate_key or not target.parent_id:
            # Fail loudly rather than write a partially-keyed artifact.
            raise ValueError("projected profile targets require identity and parent keys")
        payload = json.dumps(result.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        path = profile_cache_path(cache_dir, target.public_identifier)
        artifacts.append(
            ArtifactRow(
                f"profile:{target.candidate_key}",
                ArtifactKind.PROFILE.value,
                target.parent_id,
                str(path.resolve()),
                hashlib.sha256(payload.encode()).hexdigest(),
                ProjectionStatus.PROJECTED.value,
                candidate_key=target.candidate_key,
                payload_json=payload,
                projected_at=now_iso(),
            )
        )
    db.project_rows(tuple(artifacts))


def hydrate_profiles(
    targets: Iterable[ProfileTarget],
    cache_dir: Path,
    *,
    db: Db | None = None,
    max_workers: int = 8,
    max_per_minute: int | None = None,
    fresh: bool = False,
    on_result: Callable[[ProfileTarget, ProfileResult], None] | None = None,
) -> ProfileHydration:
    """Apply the one cache/empty/project policy for every profile consumer."""
    # Fetch at most once per DISTINCT public_identifier — the core cost
    # control. Two candidate rows (even across different parents) that
    # resolve to the same profile share one fetch/cache lookup and fan out
    # from the same result below.
    grouped: dict[str, list[ProfileTarget]] = {}
    for target in targets:
        public_identifier = target.public_identifier
        if public_identifier:
            grouped.setdefault(public_identifier, []).append(target)
    profiles: dict[str, ProfileResult] = {}

    def receive(public_identifier: str, _url: str, result: dict[str, Any]) -> None:
        # Stamp the raw/cache result into the one canonical ProfileResult
        # exactly once here, then fan the SAME parsed value out to every
        # target sharing this pub (project + caller callback).
        parsed: ProfileResult = ProfileResult.from_payload(public_identifier, _url, result)
        profiles[public_identifier] = parsed
        rows = grouped.get(public_identifier, [])
        if db is not None:
            project_profile_results(db, ((row, parsed) for row in rows), cache_dir)
        if on_result:
            for row in rows:
                on_result(row, parsed)

    # First target's URL stands for the whole group — only matters if two
    # callers passed slightly different URL strings for the same pub.
    items = [(public_identifier, rows[0].linkedin_url or "") for public_identifier, rows in grouped.items()]
    # Keyless path: a plain sequential loop straight over the client (cache
    # reads / recorded-empty answers only, nothing to rate-limit); a key
    # present routes to rapidapi_client's concurrent, rate-limited fetcher
    # below instead.
    if not provider_key_available():
        counts = {
            "wanted": len(items),
            "ok": 0,
            "failed": 0,
            "skipped_no_key": 0,
        }
        for public_identifier, linkedin_url in items:
            result: dict[str, Any] = rapidapi_client.rapidapi_profile(
                public_identifier,
                linkedin_url,
                cache_dir=cache_dir,
                fresh=fresh,
            )
            state: object = result.get("state")
            if state == PROFILE_CONTENT:
                counts["ok"] += 1
            elif state == PROFILE_EMPTY:
                counts["failed"] += 1
            else:
                counts["skipped_no_key"] += 1
            receive(
                public_identifier,
                linkedin_url,
                result,
            )
        return ProfileHydration(profiles=profiles, **counts)
    kwargs: dict[str, Any] = {
        "max_workers": max_workers,
        "fresh": fresh,
        "on_result": receive,
    }
    if max_per_minute is not None:
        kwargs["max_per_minute"] = max_per_minute
    counts = rapidapi_client.hydrate_profiles(items, cache_dir, **kwargs)
    return ProfileHydration(profiles=profiles, **counts)
