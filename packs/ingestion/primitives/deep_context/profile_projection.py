"""Project and hydrate RapidAPI profile payloads once for all consumers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    CanonicalSnapshot,
    ProjectionStatus,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path
from packs.ingestion.primitives.enrich import rapidapi_client

PROFILE_CONTENT = rapidapi_client.PROFILE_CONTENT
PROFILE_EMPTY = rapidapi_client.PROFILE_EMPTY
PROFILE_ERROR = rapidapi_client.PROFILE_ERROR


def provider_key_available() -> bool:
    """Whether paid profile hydration can run in this process."""
    return bool(rapidapi_client.RapidApiClient.resolve_key())


def profile_payloads(snapshot: CanonicalSnapshot) -> dict[str, dict[str, Any]]:
    profiles = {}
    for artifact in snapshot.artifacts:
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
        if isinstance(payload, dict):
            profiles[artifact.candidate_key] = payload
    return profiles


def project_profile_results(
    db: Db,
    results: list[tuple[dict[str, str], dict[str, Any]]],
    cache_dir: Path,
) -> None:
    artifacts = []
    for target, result in results:
        payload = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        path = profile_cache_path(cache_dir, target["public_identifier"])
        artifacts.append(ArtifactRow(
            f"profile:{target['candidate_key']}",
            ArtifactKind.PROFILE.value,
            target["parent_id"],
            str(path.resolve()),
            hashlib.sha256(payload.encode()).hexdigest(),
            ProjectionStatus.PROJECTED.value,
            candidate_key=target["candidate_key"],
            payload_json=payload,
            projected_at=now_iso(),
        ))
    db.project_rows(tuple(artifacts))


def hydrate_profiles(
    targets: list[dict[str, str]],
    cache_dir: Path,
    *,
    db: Db | None = None,
    max_workers: int = 8,
    max_per_minute: int | None = None,
    fresh: bool = False,
    on_result: Callable[[dict[str, str], dict[str, Any]], None] | None = None,
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """Apply the one cache/empty/project policy for every profile consumer."""
    grouped: dict[str, list[dict[str, str]]] = {}
    for target in targets:
        public_identifier = str(target.get("public_identifier") or "").strip().lower()
        if public_identifier:
            grouped.setdefault(public_identifier, []).append(target)
    profiles: dict[str, dict[str, Any]] = {}

    def receive(public_identifier: str, _url: str, result: dict[str, Any]) -> None:
        profiles[public_identifier] = result
        rows = grouped.get(public_identifier, [])
        if db is not None:
            project_profile_results(db, [(row, result) for row in rows], cache_dir)
        if on_result:
            for row in rows:
                on_result(row, result)

    items = [
        (public_identifier, rows[0]["linkedin_url"])
        for public_identifier, rows in grouped.items()
    ]
    if not provider_key_available():
        counts = {
            "wanted": len(items), "ok": 0, "failed": 0,
            "skipped_no_key": 0,
        }
        for public_identifier, linkedin_url in items:
            result = rapidapi_client.rapidapi_profile(
                public_identifier,
                linkedin_url,
                cache_dir=cache_dir,
                fresh=fresh,
            )
            state = result.get("state")
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
        return counts, profiles
    kwargs: dict[str, Any] = {
        "max_workers": max_workers,
        "fresh": fresh,
        "on_result": receive,
    }
    if max_per_minute is not None:
        kwargs["max_per_minute"] = max_per_minute
    counts = rapidapi_client.hydrate_profiles(items, cache_dir, **kwargs)
    return counts, profiles
