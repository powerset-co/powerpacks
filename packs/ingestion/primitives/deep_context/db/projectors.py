"""Typed file-writer boundaries for parent-owned Deep Context artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from packs.ingestion.primitives.common.jsonio import now_iso
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


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _number(value: object) -> float | None:
    """Parse a scalar to a float, or None when there was no number at all.

    `str(value or "")` used to decide "was there a number?", which reads a
    real 0 / 0.0 / False as absent — the falsy-numeric family that has shipped
    bugs here twice. A stored 0.0 confidence is a MEASUREMENT ("the judge was
    certain of nothing"), not a missing one; only None and blank text are
    absent.
    """
    if value is None:
        return None
    try:
        text = str(value).strip()
        return float(text) if text else None
    except ValueError:
        return None


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


class ProjectionValue:
    """Legacy-import access to projector-normalized scalar policies."""

    text = staticmethod(_text)
    number = staticmethod(_number)
    sha256 = staticmethod(_sha256)
    content_type = staticmethod(_content_type)


def project_parent_fact(db: Db, path: Path, parent_id: str) -> ParentFactProjection:
    """Project one synthesis output owned directly by its canonical parent."""
    path = Path(path)
    if not path.is_file():
        changed = db.project_rows((
            ArtifactReplacement(ArtifactKind.FACTS.value, (), parent_id=parent_id),
        ))
        return ParentFactProjection(parent_id, changed, 0)
    data = path.read_bytes()
    records = [
        json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()
    ]
    record = records[-1] if records else {}
    parsed: SynthesisRecord | None = SynthesisRecord.from_payload(record)
    facts = parsed.facts if parsed and parsed.facts else SynthesizedFacts()
    worth = facts.network_worth
    raw_decision = worth.decision.strip().lower() if worth else ""
    decision: str | None = (
        raw_decision if raw_decision in NETWORK_WORTH_VALUES else None
    )
    artifact_key = f"facts:{parent_id}"
    projected = db.project_rows((
        ArtifactReplacement(
            ArtifactKind.FACTS.value,
            (ArtifactRow(
                artifact_key=artifact_key,
                kind=ArtifactKind.FACTS.value,
                parent_id=parent_id,
                path=str(path.resolve()),
                input_fingerprint=_text(
                    parsed.input_evidence_fingerprint if parsed else None
                ),
                content_fingerprint=_sha256(data),
                status=ProjectionStatus.PROJECTED.value,
                payload_json=json.dumps(record, separators=(",", ":")),
                projected_at=now_iso(),
            ),),
            parent_id=parent_id,
        ),
        FactRow(
            subject_key=parent_id,
            parent_id=parent_id,
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
    return ParentFactProjection(parent_id, projected, int(decision is None))


def project_parent_source_bundle(db: Db, path: Path, parent_id: str) -> ProjectionResult:
    """Project one parent bundle, or remove its projection when absent."""
    path = Path(path)
    if not path.is_file():
        changed = db.project_rows((
            ArtifactReplacement(
                ArtifactKind.SOURCE_BUNDLE.value, (), parent_id=parent_id,
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
    if str(payload.get("person_id") or "").strip().lower() != parent_id:
        raise ProjectionError(f"source bundle owner mismatch: source-bundle:{parent_id}")
    changed = db.project_rows((ArtifactRow(
        artifact_key=f"source-bundle:{parent_id}",
        kind=ArtifactKind.SOURCE_BUNDLE.value,
        parent_id=parent_id,
        path=str(path.resolve()),
        content_fingerprint=_sha256(data),
        status=ProjectionStatus.PROJECTED.value,
        payload_json=json.dumps(payload, separators=(",", ":")),
        projected_at=now_iso(),
    ),))
    return ProjectionResult("collect_person_context", "projected", 1, changed)
