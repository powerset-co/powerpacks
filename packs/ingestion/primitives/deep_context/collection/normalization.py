"""Normalize projected message bundles to one durable bundle per parent."""

from __future__ import annotations

from pathlib import Path

from packs.ingestion.primitives.common.jsonio import parse_json_object, write_json
from packs.ingestion.primitives.deep_context.collection.models import CollectionBundle
from packs.ingestion.primitives.deep_context.collection.planning import union_bundles
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactReplacement,
    ArtifactRow,
)
from packs.ingestion.primitives.deep_context.db.projectors import project_parent_source_bundle
from packs.ingestion.primitives.deep_context.db.queries import (
    artifacts as artifact_rows,
    parents,
)
from packs.ingestion.primitives.deep_context.db.store import Db


def normalize_cached_bundles(db: Db, out_dir: Path) -> int:
    """Collapse legacy child projections using cached payloads only."""
    names = {row.parent_id: row.display_name or "" for row in parents(db)}
    source_artifacts = artifact_rows(db, kind=ArtifactKind.SOURCE_BUNDLE.value)
    parent_owned = {artifact.parent_id for artifact in source_artifacts if artifact.person_id is None}
    grouped: dict[str, list[ArtifactRow]] = {}
    for artifact in source_artifacts:
        if artifact.person_id:
            grouped.setdefault(artifact.parent_id, []).append(artifact)
    if not grouped:
        return 0

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    migrated = 0
    for parent_id, child_artifacts in sorted(grouped.items()):
        path = out_dir / f"{parent_id}.json"
        if parent_id not in parent_owned:
            bundles = [
                bundle
                for artifact in child_artifacts
                if (bundle := CollectionBundle.from_payload(parse_json_object(artifact.payload_json))) is not None
            ]
            if not bundles:
                continue
            write_json(
                path,
                union_bundles(
                    parent_id,
                    names.get(parent_id, ""),
                    bundles,
                ).to_payload(),
            )
            project_parent_source_bundle(db, path, parent_id)
        for artifact in child_artifacts:
            db.project_rows(
                (
                    ArtifactReplacement(
                        ArtifactKind.SOURCE_BUNDLE.value,
                        (),
                        person_id=artifact.person_id,
                    ),
                )
            )
            old = Path(artifact.path)
            if old.parent.resolve() == out_dir.resolve() and old != path:
                old.unlink(missing_ok=True)
        migrated += 1
    return migrated
