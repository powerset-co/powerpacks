"""Typed upload manifest and the status vocabulary shared with the People page."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.db.models import ShareDecisionRow


class Stage(StrEnum):
    PLANNING = "planning"
    CHECKING_ACCESS = "checking_access"
    CHECKING_PEOPLE = "checking_people"
    CHECKING_COMPANIES = "checking_companies"
    CHECKING_SCHOOLS = "checking_schools"
    CHECKING_CHANGES = "checking_changes"
    WRITING_PEOPLE = "writing_people"
    PEOPLE = "people"
    SUMMARIES = "summaries"
    EDUCATION = "education"
    COMPANIES = "companies"
    SCHOOLS = "schools"
    COMMITTING = "committing"
    COMPLETED = "completed"


CHANGED_CHECK = "Your network changed since the check. Check again."
CHECK_FIRST = "Check your network before sharing."
RUN_ACTIVE = "A check or upload is already running."
INTERRUPTED = "This upload was interrupted. Check again to resume."
UPLOAD_FAILED = "Upload failed. Check again to resume."
CHECK_FAILED = "Could not check your network. Check again."


@dataclass(frozen=True)
class UploadManifest:
    status: str = "idle"
    stage: str | None = None
    dry_run: bool = True
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict[str, Any] = field(default_factory=lambda: {
        "total": 0, "uploaded": 0, "skipped": 0, "namespaces": {},
    })
    plan: dict[str, Any] | None = None
    checked_target: dict[str, Any] | None = None
    share_digest: str | None = None
    last_upload: dict[str, Any] | None = None
    target: dict[str, Any] | None = None
    person_hashes: dict[str, str] = field(default_factory=dict)
    owned_people: tuple[str, ...] = ()
    pending_upserts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    operator_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    error_type: str | None = None
    http_status: int | None = None

    @classmethod
    def read(cls, path: Path) -> UploadManifest:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        values = {key: value for key, value in raw.items() if key in cls.__dataclass_fields__}
        values["progress"] = {**cls().progress, **(values.get("progress") or {})}
        return cls(**values)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_suffix(".tmp")
        pending.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pending.replace(path)

    def at(self, stage: Stage, **changes: Any) -> UploadManifest:
        return replace(self, stage=stage.value, **changes)


def share_digest(rows: tuple[ShareDecisionRow, ...]) -> str:
    decisions = sorted((row.person_id, row.public_identifier, row.share, row.reason, row.labels)
                       for row in rows)
    return hashlib.sha256(json.dumps(decisions, separators=(",", ":")).encode()).hexdigest()
