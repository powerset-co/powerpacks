"""Project completed Parallel outputs into the canonical SQLite store."""

from __future__ import annotations

import hashlib
import json

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
    ReviewSource,
    RowKind,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research import queue
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import (
    ResearchQueueRow,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
    ParallelProviderResult,
    ResearchRunParams,
)
from packs.ingestion.primitives.deep_context.enrich.research_result import ResearchResult
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    normalize_linkedin_url,
)

def research_artifact_projections(
    params: ResearchRunParams,
    rows: tuple[ResearchQueueRow, ...] | list[ResearchQueueRow] | None = None,
) -> tuple[ArtifactProjection, ...]:
    """Parse completed provider outputs once into typed SQLite projections."""
    projections: list[ArtifactProjection] = []
    seen: set[str] = set()
    for row in params.rows if rows is None else rows:
        handle = queue.candidate_handle(row)
        if handle in seen:
            continue
        seen.add(handle)
        result_path = params.output_dir / handle / "01_research_parallel.json"
        if not result_path.is_file():
            continue
        result_data = result_path.read_bytes()
        profile_payload = json.loads(result_data)
        if not isinstance(profile_payload, dict):
            raise ValueError(f"research artifact must be an object: {result_path}")
        profile: ResearchResult = ResearchResult.from_payload(profile_payload)
        person_ids = [
            value.strip().lower()
            for value in row.source_person_ids
            if value.strip()
        ]
        if not person_ids:
            raise ValueError(f"research queue row has no person ids: {handle}")
        row_key = row.row_key.strip().lower()
        public_identifier = row.source_candidate_public_identifier.strip().lower()
        parent_id = row.parent_id.strip().lower()
        if not parent_id or not row_key:
            raise ValueError(f"research queue ownership is unresolved: {handle}")
        linkedin_value = profile.linkedin_url
        linkedin_url: str | None = (
            normalize_linkedin_url(linkedin_value) if linkedin_value else None
        )
        found_public_identifier = (
            extract_public_identifier(linkedin_url).lower() if linkedin_url else ""
        )
        artifact_key = f"research:{handle}".lower()
        now = now_iso()
        artifact = ArtifactRow(
            artifact_key=artifact_key,
            kind=ArtifactKind.RESEARCH.value,
            parent_id=parent_id,
            path=str(result_path.resolve()),
            content_fingerprint=hashlib.sha256(result_data).hexdigest(),
            status=ProjectionStatus.PROJECTED.value,
            candidate_key=row_key,
            input_fingerprint=queue.input_fingerprint(row, handle),
            payload_json=json.dumps(profile.to_payload(), separators=(",", ":")),
            projected_at=now,
        )
        candidate: LinkRow | None = None
        if not row.candidate_exists:
            candidate = LinkRow(
                row_key,
                parent_id,
                public_identifier or found_public_identifier,
                RowKind.RESEARCH.value,
                None,
                row.display_name.strip() or None,
                candidate_origin=any(
                    value.startswith("candidate:") for value in person_ids
                ),
                paid_profile=True,
                source=ReviewSource.DEEP_RESEARCH.value,
                updated_at=now_iso(),
            )
        raw_artifact: ArtifactRow | None = None
        raw_path = params.output_dir / handle / "00_parallel_raw.json"
        if raw_path.is_file():
            raw_data = raw_path.read_bytes()
            raw_payload = json.loads(raw_data)
            if not isinstance(raw_payload, dict):
                raise ValueError(f"raw research artifact must be an object: {raw_path}")
            provider_result: ParallelProviderResult = (
                ParallelProviderResult.from_payload(raw_payload)
            )
            raw_artifact = ArtifactRow(
                artifact_key=f"raw-result:{row_key}".lower(),
                kind=ArtifactKind.RAW_RESULT.value,
                parent_id=parent_id,
                path=str(raw_path.resolve()),
                content_fingerprint=hashlib.sha256(raw_data).hexdigest(),
                status=ProjectionStatus.PROJECTED.value,
                candidate_key=row_key,
                payload_json=json.dumps(provider_result.to_payload(), separators=(",", ":")),
                projected_at=now_iso(),
            )
        projections.append(ArtifactProjection(
            artifact=artifact,
            raw_artifact=raw_artifact,
            candidate=candidate,
            candidate_people=(
                CandidatePeopleProjection(
                    row_key,
                    tuple(
                        CandidatePersonRow(row_key, person_id, parent_id)
                        for person_id in sorted(set(person_ids))
                    ),
                )
                if candidate is not None else None
            ),
            research=ResearchRow(
                handle,
                parent_id,
                (
                    ResearchStatus.COMPLETE.value
                    if linkedin_url else ResearchStatus.NO_MATCH.value
                ),
                row_key,
                artifact_key,
                params.selection_fingerprint or None,
                json.dumps(profile.to_payload(), separators=(",", ":")),
                now_iso(),
            ),
        ))
    return tuple(projections)
