"""Project one accepted Parallel output into the canonical SQLite store."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactProjection,
    ArtifactRow,
    CandidatePeopleProjection,
    CandidatePersonRow,
    LinkRow,
    ProjectionStatus,
    ResearchRow,
    ResearchStatus,
    RowKind,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import ResearchRunParams
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import ResearchQueueRow, input_fingerprint
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url


def research_artifact_projection(
    params: ResearchRunParams,
    row: ResearchQueueRow,
    result: ResearchResult,
    result_path: Path,
    result_data: bytes,
) -> ArtifactProjection:
    """Build the one research artifact/projection pair from an in-memory result."""
    handle = row.handle
    person_ids = sorted({value.strip().lower() for value in row.source_person_ids if value.strip()})
    if not person_ids:
        raise ValueError(f"research queue row has no person ids: {handle}")
    row_key = row.row_key.strip().lower()
    parent_id = row.parent_id.strip().lower()
    if not parent_id or not row_key:
        raise ValueError(f"research queue ownership is unresolved: {handle}")
    linkedin_url = normalize_linkedin_url(result.linkedin_url) if result.linkedin_url else None
    found_public_identifier = extract_public_identifier(linkedin_url).lower() if linkedin_url else ""
    artifact_key = f"research:{handle}".lower()
    now = now_iso()
    payload_json = json.dumps(result.to_payload(), separators=(",", ":"))
    candidate: LinkRow | None = None
    if not row.candidate_exists:
        candidate = LinkRow(
            row_key,
            parent_id,
            row.source_candidate_public_identifier.strip().lower() or found_public_identifier,
            RowKind.RESEARCH.value,
            None,
            row.display_name.strip() or None,
            candidate_origin=any(value.startswith("candidate:") for value in person_ids),
            paid_profile=True,
            source=WriterSource.DEEP_RESEARCH.value,
            updated_at=now,
        )
    return ArtifactProjection(
        artifact=ArtifactRow(
            artifact_key=artifact_key,
            kind=ArtifactKind.RESEARCH.value,
            parent_id=parent_id,
            path=str(result_path.resolve()),
            content_fingerprint=hashlib.sha256(result_data).hexdigest(),
            status=ProjectionStatus.PROJECTED.value,
            candidate_key=row_key,
            input_fingerprint=input_fingerprint(row, handle, processor=params.processor, beta_header=params.beta_header),
            payload_json=payload_json,
            projected_at=now,
        ),
        candidate=candidate,
        candidate_people=(
            CandidatePeopleProjection(
                row_key,
                tuple(CandidatePersonRow(row_key, person_id, parent_id) for person_id in person_ids),
            )
            if candidate is not None
            else None
        ),
        research=ResearchRow(
            handle,
            parent_id,
            ResearchStatus.COMPLETE.value if linkedin_url else ResearchStatus.NO_MATCH.value,
            row_key,
            artifact_key,
            params.selection_fingerprint or None,
            payload_json,
            now,
        ),
    )
