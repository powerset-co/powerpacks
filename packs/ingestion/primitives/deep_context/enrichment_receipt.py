"""One typed writer for the fixed Deep Context enrichment receipt."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.db.projectors import project_manifest
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.imports.common import write_manifest
from packs.ingestion.primitives.pipeline.contract import StageManifest


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


class EnrichmentReceiptBody(StageManifest):
    """Stable receipt schema; CLI result payloads remain separate."""

    source: str | None = None
    stage: str = "enrich"
    counts: dict[str, int] | None = None
    selection: dict[str, Any] | None = None
    eligible: int | None = None
    eligible_candidates: int | None = None
    candidates_skipped_not_added: int | None = None
    would_submit: int | None = None
    reused_completed: int | None = None
    duplicate_handles: int | None = None
    processor: str | None = None
    cost_per_person_usd: float | None = None
    estimated_usd: float | None = None
    budget_usd: float | None = None
    input: dict[str, str] | None = None
    outputs: dict[str, str] | None = None
    privacy: dict[str, bool] | None = None
    result_status: str | None = None
    error: str | None = None
    artifacts: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class EnrichmentReceipt:
    """Read, mutate, write, and project one fixed ``manifest.json``."""

    path: Path
    db: Db | None = None

    def __post_init__(self) -> None:
        if self.path.name != "manifest.json":
            raise ValueError("enrichment manifest path must end in manifest.json")

    def read(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def write(self, payload: StageManifest | dict[str, Any]) -> dict[str, Any]:
        body = payload.to_payload() if isinstance(payload, StageManifest) else dict(payload)
        body.pop("updated_at", None)
        body.pop("created_at", None)
        written = write_manifest(
            self.path.parent.name,
            body,
            import_dir=self.path.parent.parent,
        )
        if self.db is not None:
            project_manifest(self.db, self.path)
        return written

    def update(
        self,
        changes: dict[str, Any],
        *,
        require_existing: bool = False,
        remove: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        current = self.read()
        if current is None and require_existing:
            return None
        payload = {**(current or {}), **changes}
        for key in remove:
            payload.pop(key, None)
        return self.write(payload)
