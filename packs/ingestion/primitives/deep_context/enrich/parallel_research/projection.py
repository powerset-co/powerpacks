"""Project one accepted Parallel output into the canonical SQLite store.

Changelog:
- 2026-09-25: `native_research_payload` (the retired normalized result shape
  converted to the provider envelope) lives here, shared by the seed stage and
  the legacy import.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

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
from packs.ingestion.primitives.deep_context.shared.coerce import clean_text
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url


def native_research_payload(payload: object, handle: str) -> dict[str, Any]:
    """Convert the retired normalized result once, at the legacy boundary."""
    if not isinstance(payload, dict):
        raise ValueError(f"research {handle} must be a JSON object")
    if payload.get("type") == "json" and isinstance(payload.get("content"), dict):
        return payload
    person = payload.get("person") if isinstance(payload.get("person"), dict) else {}
    location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
    social = payload.get("social") if isinstance(payload.get("social"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    headline = payload.get("headline") if isinstance(payload.get("headline"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    notes = clean_text(metadata.get("research_notes")) or clean_text(person.get("notes"))
    return {
        "type": "json",
        "content": {
            "real_name": clean_text(person.get("full_name")),
            "work_experience": payload.get("positions") if isinstance(payload.get("positions"), list) else [],
            "education": payload.get("education") if isinstance(payload.get("education"), list) else [],
            "location_city": clean_text(location.get("city")),
            "location_country": clean_text(location.get("country")),
            "linkedin_url": clean_text(social.get("linkedin_url")),
            "github_url": clean_text(social.get("github_url")),
            "summary": clean_text(summary.get("text")) or clean_text(headline.get("text")) or "",
        },
        "basis": [{"field": "real_name", "reasoning": notes, "citations": []}] if notes else [],
    }


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
    # One rendering of the provider envelope: the caller's pretty bytes are
    # both the file content and the DB payload — no second serialization.
    payload_json = result_data.decode("utf-8")
    candidate: LinkRow | None = None
    if not row.candidate_exists:
        candidate = LinkRow(
            row_key,
            parent_id,
            found_public_identifier,
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
            payload_json,
            now,
        ),
    )
