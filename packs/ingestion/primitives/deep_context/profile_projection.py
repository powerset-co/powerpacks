"""Project and hydrate RapidAPI profile payloads once for all consumers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactRow,
    CanonicalSnapshot,
    ProjectionStatus,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.enrich.profile_cache import profile_cache_path


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
