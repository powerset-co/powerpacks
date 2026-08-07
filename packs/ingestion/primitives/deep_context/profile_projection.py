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
from packs.ingestion.primitives.deep_context.profile_models import (
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
    """Read normalized profiles from projected profile artifacts only."""
    profiles: dict[str, ProfileResult] = {}
    selected_keys = tuple(candidate_keys) if candidate_keys is not None else None
    for artifact in queries.artifacts(
        db,
        kind=ArtifactKind.PROFILE.value,
        candidate_keys=selected_keys,
    ):
        if (
            artifact.kind != ArtifactKind.PROFILE.value
            or artifact.status != ProjectionStatus.PROJECTED.value
            or not artifact.candidate_key
        ):
            continue
        try:
            payload = json.loads(artifact.payload_json or "")
        except json.JSONDecodeError:
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


def canonical_profile_result(
    public_identifier: str,
    linkedin_url: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Stamp one profile vocabulary at the RapidAPI boundary."""
    return ProfileResult.from_payload(public_identifier, linkedin_url, result).to_payload()


def project_profile_results(
    db: Db,
    results: Iterable[tuple[ProfileTarget, ProfileResult]],
    cache_dir: Path,
) -> None:
    artifacts = []
    for target, result in results:
        if not target.public_identifier or not target.candidate_key or not target.parent_id:
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
    grouped: dict[str, list[ProfileTarget]] = {}
    for target in targets:
        public_identifier = target.public_identifier
        if public_identifier:
            grouped.setdefault(public_identifier, []).append(target)
    profiles: dict[str, ProfileResult] = {}

    def receive(public_identifier: str, _url: str, result: dict[str, Any]) -> None:
        parsed: ProfileResult = ProfileResult.from_payload(public_identifier, _url, result)
        profiles[public_identifier] = parsed
        rows = grouped.get(public_identifier, [])
        if db is not None:
            project_profile_results(db, ((row, parsed) for row in rows), cache_dir)
        if on_result:
            for row in rows:
                on_result(row, parsed)

    items = [(public_identifier, rows[0].linkedin_url or "") for public_identifier, rows in grouped.items()]
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
