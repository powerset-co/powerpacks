"""Write enrichment progress while retaining its Parallel provider receipt."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.imports.common import write_manifest


@dataclass(frozen=True)
class EnrichmentReceipt:
    """Write fresh progress and retain the provider receipt."""

    path: Path

    def __post_init__(self) -> None:
        if self.path.name != "manifest.json":
            raise ValueError("enrichment manifest path must end in manifest.json")

    def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        if self.path.exists():
            existing = json.loads(self.path.read_text())
            if "parallel" in existing:
                body["parallel"] = existing["parallel"]
        body.pop("updated_at", None)
        body.pop("created_at", None)
        body.pop("artifacts", None)
        body.pop("approval", None)
        written = write_manifest(
            self.path.parent.name,
            body,
            import_dir=self.path.parent.parent,
        )
        return written
