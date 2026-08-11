"""One typed writer for the fixed Deep Context enrichment receipt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.imports.common import write_manifest


@dataclass(frozen=True)
class EnrichmentReceipt:
    """Write one fresh display-only enrichment receipt."""

    path: Path

    def __post_init__(self) -> None:
        if self.path.name != "manifest.json":
            raise ValueError("enrichment manifest path must end in manifest.json")

    def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        body.pop("updated_at", None)
        body.pop("created_at", None)
        body.pop("artifacts", None)
        body.pop("selection", None)
        body.pop("approval", None)
        written = write_manifest(
            self.path.parent.name,
            body,
            import_dir=self.path.parent.parent,
        )
        return written
