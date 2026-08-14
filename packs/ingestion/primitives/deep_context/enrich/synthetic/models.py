"""Frozen CSV/JSON row for one assembled synthetic profile."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from packs.ingestion.primitives.deep_context.shared.coerce import compact_json, text


@dataclass(frozen=True)
class SyntheticCsvRow:
    """CSV/JSON round-trip wrapper for one assembled row. Only the fields call
    sites actually branch on are materialized as typed attributes; the rest
    of the ~30 people-schema columns live solely in `_payload_json`, reached
    through `to_payload()`."""

    public_identifier: str
    approved: str | None
    full_name: str | None
    linkedin_url: str | None
    source_parent_slug: str | None
    _payload_json: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SyntheticCsvRow:
        social = payload.get("social") if isinstance(payload.get("social"), dict) else {}
        return cls(
            str(payload.get("public_identifier") or "").lower(),
            text(payload.get("approved")),
            text(payload.get("full_name")),
            text(payload.get("linkedin_url") or social.get("linkedin_url")),
            text(payload.get("source_parent_slug")),
            compact_json(payload),
        )

    @classmethod
    def from_json(
        cls,
        value: str,
        *,
        approved: str | None = None,
    ) -> SyntheticCsvRow | None:
        try:
            payload = json.loads(value or "{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        row = cls.from_payload(payload)
        return row.with_approved(approved) if approved is not None else row

    def with_approved(self, approved: str | None) -> SyntheticCsvRow:
        """Flip only `approved`, rebuilding from the full payload — how a
        prior human yes/no survives a re-assembled row (see
        AssembleSyntheticProfile.execute)."""
        payload = self.to_payload()
        payload["approved"] = approved or ""
        return self.from_payload(payload)

    def to_payload(self) -> dict[str, str]:
        return {
            str(key): str(value or "")
            for key, value in json.loads(self._payload_json).items()
        }
