"""Provider execution and canonical identity settlement for guided research."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    DEEP_RESEARCH_DIR,
    PROFILE_CACHE_DIR,
)
from packs.ingestion.primitives.deep_context.db.models import (
    GuidanceRow,
    GuidanceState,
    ParentSnapshotRow,
    RESEARCH_CONFIRM_THRESHOLD,
    ReviewSource,
    ResearchHandle,
)
from packs.ingestion.primitives.deep_context.db.view_models import (
    CandidateViewRow,
    EnrichmentQueueRow,
)
from packs.ingestion.primitives.deep_context.db.people_views import (
    ParentViewRow,
    person_detail,
)
from packs.ingestion.primitives.deep_context.db.identity_queries import research_rows
from packs.ingestion.primitives.deep_context.db.queries import parents
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.deep_research_contacts import (
    ResearchRunParams,
    run_research,
)
from packs.ingestion.primitives.deep_context.dossier_evidence import owner_background
from packs.ingestion.primitives.deep_context.identity_reconcile.guidance import GuidanceRequest
from packs.ingestion.primitives.deep_context.identity_reconcile.models import (
    GuidanceOutcome,
    GuidedProviderResult,
)
from packs.ingestion.primitives.deep_context.research_reconcile.judging import (
    propose_retargets,
)
from packs.ingestion.primitives.deep_context.research_reconcile.selection import (
    DEFAULT_PROCESSOR,
    build_queue_row,
)
from packs.ingestion.primitives.deep_context.parallel_research.queue import (
    ResearchQueueRow,
)
from packs.ingestion.primitives.deep_context.research_result import ResearchResult
from packs.ingestion.schemas.people_schema import normalize_linkedin_url


@dataclass(frozen=True)
class GuidedResearch:
    """Run one guided provider request through the shared research judge."""

    db: Db
    research_dir: Path = DEEP_RESEARCH_DIR
    profile_cache_dir: Path = PROFILE_CACHE_DIR
    use_llm: bool = True
    model: str = DEFAULT_MODEL
    reasoning_effort: str = "medium"
    confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD

    def research(self, request: GuidanceRequest) -> GuidedProviderResult:
        parent = person_detail(self.db, request.slug)
        if not parent:
            raise StoreError(f"person not found: {request.slug}")
        row = self.research_row(request, parent)
        result = run_research(
            ResearchRunParams(
                output_dir=self.research_dir,
                rows=(row,),
                processor=DEFAULT_PROCESSOR,
                db=self.db,
            )
        )
        if result.status not in {"completed", "no_work"}:
            raise StoreError(result.error or "guided research failed")
        research_row = next(
            (
                item
                for item in research_rows(self.db, handle=row.handle)
                if str(item.candidate_key or "").lower() == request.row_key.lower()
            ),
            None,
        )
        research = ResearchResult.from_json(research_row.result_json) if research_row else None
        if research is None:
            raise StoreError("guided research produced no result")
        return GuidedProviderResult(
            research.linkedin_url,
            research.reason,
            research,
        )

    def apply_provider_result(
        self,
        parent_id: str,
        parent: ParentViewRow,
        request: GuidanceRequest,
        result: GuidedProviderResult,
    ) -> GuidanceOutcome:
        """Judge and project a provider URL; human-pasted URLs bypass this path."""
        if not isinstance(result, GuidedProviderResult):
            raise TypeError("guided runner must return a GuidedProviderResult")
        research = result.research_result
        url = normalize_linkedin_url(research.linkedin_url)
        if not url:
            return self.record(
                parent_id,
                request,
                GuidanceState.FAILED,
                "no_match",
                result.detail or "no LinkedIn found",
            )
        person_ids = tuple(request.person_ids) or parent.person_ids
        canonical_parent: ParentSnapshotRow = next(iter(parents(self.db, parent_id=parent_id)))
        handle = ResearchHandle.for_parent(parent_id, canonical_parent.display_slug)
        propose_retargets(
            [
                EnrichmentQueueRow(
                    parent_id=parent_id,
                    parent_slug=handle,
                    name=request.name or parent.name,
                    person_ids=person_ids,
                    row_key=request.row_key,
                    candidate_exists=True,
                    linkedin_url=request.linkedin_url,
                    verdict="",
                    verdict_reason="",
                    match_emails=tuple(request.match_emails),
                    match_phones=tuple(request.match_phones),
                    candidate_origin=False,
                )
            ],
            db=self.db,
            use_llm=self.use_llm,
            owner_block=owner_background(self.db),
            model=self.model,
            effort=self.reasoning_effort,
            confirm_threshold=self.confirm_threshold,
            profile_cache_dir=self.profile_cache_dir,
            source=ReviewSource.USER_GUIDANCE.value,
            provided_results={handle: research},
        )
        updated_parent = person_detail(self.db, parent_id)
        decision: CandidateViewRow | None = (
            next(
                (candidate for candidate in updated_parent.candidates if candidate.row_key == request.row_key),
                None,
            )
            if updated_parent
            else None
        )
        if decision is None:
            return self.record(
                parent_id,
                request,
                GuidanceState.FAILED,
                "no_match",
                "research result could not be attached to this person",
                candidate_url=url,
            )
        rejected = decision.llm_reject.lower() in {
            "1",
            "true",
            "yes",
        }
        if decision.action == "retarget" and not rejected:
            return self.record(
                parent_id,
                request,
                GuidanceState.APPLIED,
                "applied",
                result.detail or "research result applied",
                new_url=url,
                resolved_pubs=[request.row_key],
            )
        return self.record(
            parent_id,
            request,
            GuidanceState.FAILED,
            "no_match",
            str(decision.llm_reject_reason or "research result did not clear the identity threshold"),
            candidate_url=url,
        )

    def research_row(
        self,
        request: GuidanceRequest,
        parent: ParentViewRow,
    ) -> ResearchQueueRow:
        parent_id = parent.parent_id
        canonical_parent: ParentSnapshotRow = next(iter(parents(self.db, parent_id=parent_id)))
        handle = ResearchHandle.for_parent(parent_id, canonical_parent.display_slug)
        candidate: CandidateViewRow | None = next(
            (item for item in parent.candidates if item.row_key == request.row_key),
            None,
        )
        row = EnrichmentQueueRow(
            parent_id=parent_id,
            candidate_exists=candidate is not None,
            row_key=request.row_key,
            parent_slug=handle,
            person_ids=tuple(request.person_ids) or parent.person_ids,
            name=request.name or parent.name,
            linkedin_url=(request.linkedin_url or (candidate.url if candidate else "")),
            verdict="",
            verdict_reason=candidate.reason if candidate else "",
            match_emails=tuple(request.match_emails),
            match_phones=tuple(request.match_phones),
            candidate_origin=False,
        )
        return build_queue_row(
            self.db,
            row,
            owner_context=owner_background(self.db),
            guidance=request.guidance,
        )

    def record(
        self,
        parent_id: str,
        request: GuidanceRequest,
        guidance_state: GuidanceState,
        state: str,
        detail: str = "",
        new_url: str = "",
        resolved_pubs: list[str] | tuple[str, ...] = (),
        candidate_url: str = "",
    ) -> GuidanceOutcome:
        item = GuidanceOutcome(
            slug=request.slug,
            row_key=request.row_key,
            name=request.name,
            guidance=request.guidance,
            state=state,
            detail=detail,
            submitted_at=request.submitted_at,
            updated_at=now_iso(),
            new_url=new_url,
            resolved_pubs=tuple(resolved_pubs),
            candidate_url=candidate_url,
        )
        detail_json = json.dumps({**item.as_dict(), "request": asdict(request)}, separators=(",", ":"))
        self.db.project_rows(
            (
                GuidanceRow(
                    parent_id,
                    parent_id,
                    request.guidance,
                    guidance_state.value,
                    request.row_key,
                    request.submitted_at,
                    item.new_url or None,
                    detail_json,
                ),
            )
        )
        return item
