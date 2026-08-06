"""One typed writer for the fixed Deep Context enrichment receipt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.db.projectors import project_artifacts
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.imports.common import write_manifest


def enrichment_counts(
    *, total: int, completed: int = 0, failed: int = 0
) -> dict[str, int]:
    total = max(0, total)
    completed = min(max(0, completed), total)
    failed = min(max(0, failed), total - completed)
    return {
        "total": total,
        "completed": completed,
        "pending": total - completed - failed,
        "failed": failed,
    }


@dataclass(frozen=True)
class EnrichmentReceipt:
    """Project completed artifacts and write one fresh display receipt."""

    path: Path
    db: Db | None = None

    def __post_init__(self) -> None:
        if self.path.name != "manifest.json":
            raise ValueError("enrichment manifest path must end in manifest.json")

    def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        body.pop("updated_at", None)
        body.pop("created_at", None)
        inventory = body.pop("artifacts", None)
        selection = body.pop("selection", None)
        body.pop("approval", None)
        selection_fingerprint = (
            str(selection.get("fingerprint") or selection.get("sha256") or "") or None
            if isinstance(selection, dict)
            else str(selection or "") or None
        )
        if self.db is not None and inventory is not None:
            if not isinstance(inventory, list):
                raise ValueError("enrichment artifacts must be an array of objects")
            project_artifacts(
                self.db,
                self.path.parent,
                inventory,
                stage=str(body.get("stage") or self.path.parent.name),
                selection=selection_fingerprint,
            )
        written = write_manifest(
            self.path.parent.name,
            body,
            import_dir=self.path.parent.parent,
        )
        return written
