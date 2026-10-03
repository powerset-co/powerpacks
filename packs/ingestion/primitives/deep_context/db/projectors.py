"""Project contact evidence and derived parent artifacts into SQLite.

Changelog:
- 2026-09-25: ProjectionValue, the legacy import's door to these policies, went with legacy.py.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.shared.coerce import clean_text
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactReplacement,
    ArtifactRow,
    FactRow,
    ProjectionStatus,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.synthesis.facts import NETWORK_WORTH_VALUES
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesizedFacts
from packs.ingestion.primitives.deep_context.synthesis.models import SynthesisRecord
from packs.ingestion.primitives.deep_context.synthesis.history import FactHistory


class ProjectionError(StoreError):
    pass


@dataclass(frozen=True)
class ProjectionResult:
    stage: str
    status: str
    artifacts: int
    projected: int


@dataclass(frozen=True)
class ParentFactProjection:
    parent_id: str
    synced_rows: int
    without_worth: int


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content_type(data: bytes) -> str:
    """Detect the small image set profile providers return, without extensions."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def project_parent_fact(db: Db, path: Path, parent_id: str, *, artifact_key: str | None = None,
                        excluded_person_ids: tuple[str, ...] = ()) -> ParentFactProjection:
    """Project parent facts without removing preserved extraction artifacts."""
    projection = _project_fact(db, path, parent_id, None, artifact_key or f"facts:{parent_id}")
    if excluded_person_ids:
        with db.transaction() as conn:
            conn.executemany("DELETE FROM facts WHERE parent_id=? AND person_id=?",
                             ((parent_id, person_id) for person_id in excluded_person_ids))
    return projection


def project_person_fact(db: Db, path: Path, person_id: str) -> ParentFactProjection:
    """Keep a contact's extraction owned by that contact across parent merges."""
    parent_id = db.query("SELECT parent_id FROM people WHERE person_id=?", (person_id,))[0]["parent_id"]
    return _project_fact(db, path, parent_id, person_id, f"facts:{person_id}")


def _project_fact(db: Db, path: Path, parent_id: str, person_id: str | None, artifact_key: str) -> ParentFactProjection:
    path = Path(path)
    if not path.is_file():
        raise ProjectionError(f"facts file is missing: {path}")
    data = path.read_bytes()
    records = [
        json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()
    ]
    record = FactHistory.from_records(records).payload()
    parsed: SynthesisRecord | None = SynthesisRecord.from_payload(record)
    facts = parsed.facts if parsed and parsed.facts else SynthesizedFacts()
    worth = facts.network_worth
    raw_decision = worth.decision.strip().lower() if worth else ""
    decision: str | None = (
        raw_decision if raw_decision in NETWORK_WORTH_VALUES else None
    )
    projected = db.project_rows((
        ArtifactRow(
            artifact_key=artifact_key,
            kind=ArtifactKind.FACTS.value,
            parent_id=parent_id,
            person_id=person_id,
            path=str(path.resolve()),
            input_fingerprint=clean_text(
                parsed.input_evidence_fingerprint if parsed else None
            ),
            content_fingerprint=_sha256(data),
            status=ProjectionStatus.PROJECTED.value,
            payload_json=json.dumps(record, separators=(",", ":")),
            projected_at=now_iso(),
        ),
        FactRow(
            subject_key=person_id or parent_id,
            parent_id=parent_id,
            person_id=person_id,
            artifact_key=artifact_key,
            machine_worth=decision,
            machine_worth_reason=worth.reason or None if worth else None,
            confidence=(
                parsed.final_confidence
                if parsed and parsed.final_confidence
                else facts.confidence
            ),
            is_owner=bool(facts.is_owner),
            facts_json=json.dumps(facts.to_payload(), separators=(",", ":")),
            projected_at=now_iso(),
        ),
    ))
    if artifact_key == f"parent-facts:{parent_id}":
        with db.transaction() as conn:
            conn.execute("DELETE FROM facts WHERE parent_id=? AND person_id IS NULL AND subject_key!=?",
                         (parent_id, parent_id))
    return ParentFactProjection(parent_id, projected, int(decision is None))


def project_parent_source_bundle(db: Db, path: Path, parent_id: str) -> ProjectionResult:
    """Project a derived parent bundle."""
    return _project_source_bundle(db, path, parent_id, None)


def project_person_source_bundle(db: Db, path: Path, person_id: str) -> ProjectionResult:
    """Project one contact bundle under its stable contact owner."""
    parent_id = db.query("SELECT parent_id FROM people WHERE person_id=?", (person_id,))[0]["parent_id"]
    return _project_source_bundle(db, path, parent_id, person_id)


def _project_source_bundle(db: Db, path: Path, parent_id: str, person_id: str | None) -> ProjectionResult:
    path = Path(path)
    if not path.is_file():
        changed = db.project_rows((
            ArtifactReplacement(
                ArtifactKind.SOURCE_BUNDLE.value, (), **({"person_id": person_id} if person_id else {"parent_id": parent_id}),
            ),
        ))
        return ProjectionResult("collect_person_context", "projected", 0, changed)
    try:
        data = path.read_bytes()
        payload = json.loads(data)
    except OSError as exc:
        raise ProjectionError(f"cannot read source bundle {path}: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"invalid JSON artifact {path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProjectionError(f"JSON artifact must be an object: {path.name}")
    subject = person_id or parent_id
    if str(payload.get("person_id") or "").strip().lower() != subject:
        raise ProjectionError(f"source bundle owner mismatch: source-bundle:{subject}")
    changed = db.project_rows((ArtifactRow(
        artifact_key=f"source-bundle:{subject}",
        kind=ArtifactKind.SOURCE_BUNDLE.value,
        parent_id=parent_id,
        person_id=person_id,
        path=str(path.resolve()),
        content_fingerprint=_sha256(data),
        status=ProjectionStatus.PROJECTED.value,
        payload_json=json.dumps(payload, separators=(",", ":")),
        projected_at=now_iso(),
    ),))
    return ProjectionResult("collect_person_context", "projected", 1, changed)
