"""Typed boundary for the fixed, display-only enrichment receipt."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import read_json
from packs.ingestion.primitives.imports.common import write_manifest


@dataclass(frozen=True)
class EnrichmentReceiptView:
    """Small display projection; never a selection, spend, or resume input."""

    status: str
    request_fingerprint: str
    total: int
    completed: int
    pending: int
    approved_budget_usd: float | None
    progress_json: str | None
    error: str | None

    @classmethod
    def from_payload(cls, payload: object) -> EnrichmentReceiptView | None:
        if not isinstance(payload, dict):
            return None
        counts = payload.get("counts")
        if not isinstance(counts, dict):
            return None
        fingerprint = payload.get("request_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            return None
        try:
            total = max(0, int(counts.get("total", 0)))
            completed = min(max(0, int(counts.get("completed", 0))), total)
            pending = min(max(0, int(counts.get("pending", 0))), total - completed)
            budget_value = payload.get("approved_budget_usd")
            budget = float(budget_value) if budget_value is not None else None
        except (TypeError, ValueError):
            return None
        progress = payload.get("progress")
        return cls(
            status=str(payload.get("status") or ""),
            request_fingerprint=fingerprint,
            total=total,
            completed=completed,
            pending=pending,
            approved_budget_usd=budget,
            progress_json=(
                json.dumps(progress, separators=(",", ":"))
                if isinstance(progress, dict)
                else None
            ),
            error=str(payload["error"]) if payload.get("error") else None,
        )


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
        body.pop("approval", None)
        written = write_manifest(
            self.path.parent.name,
            body,
            import_dir=self.path.parent.parent,
        )
        return written

    def read(self) -> EnrichmentReceiptView | None:
        return EnrichmentReceiptView.from_payload(read_json(self.path, None))
