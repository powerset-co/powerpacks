"""Provider execution and canonical identity settlement for guided research."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    DEEP_RESEARCH_DIR,
    PROFILE_CACHE_DIR,
)
from packs.ingestion.primitives.deep_context.db.models import (
    CanonicalSnapshot,
    GuidanceRow,
    GuidanceState,
    RESEARCH_CONFIRM_THRESHOLD,
    ReviewSource,
    ResearchHandle,
)
from packs.ingestion.primitives.deep_context.db.view_models import (
    EnrichmentQueueRow,
)
from packs.ingestion.primitives.deep_context.db.people_views import (
    ParentViewRow,
    person_detail,
)
from packs.ingestion.primitives.deep_context.db.snapshots import (
    canonical_snapshot,
    identity_snapshot,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.deep_research_contacts import (
    ResearchRunParams,
    run_research,
)
from packs.ingestion.primitives.deep_context.dossier_evidence import owner_background
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
class GuidedProviderResult:
    """One parsed research-provider result passed into identity settlement."""

    new_url: str
    detail: str
    research_result: ResearchResult


@dataclass(frozen=True)
class GuidanceOutcome:
    """One durable guidance result before its HTTP/JSON serialization edge."""

    slug: str
    row_key: str
    name: str
    guidance: str
    state: str
    detail: str
    submitted_at: str
    updated_at: str
    new_url: str = ""
    resolved_pubs: tuple[str, ...] = ()
    candidate_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "slug": self.slug,
            "row_key": self.row_key,
            "name": self.name,
            "guidance": self.guidance,
            "state": self.state,
            "detail": self.detail,
            "submitted_at": self.submitted_at,
            "updated_at": self.updated_at,
        }
        if self.new_url:
            values["new_url"] = self.new_url
        if self.resolved_pubs:
            values["resolved_pubs"] = list(self.resolved_pubs)
        if self.candidate_url:
            values["candidate_url"] = self.candidate_url
        return values


@dataclass
class GuidedResearch:
    """Run one guided provider request through the shared research judge."""

    db: Db
    research_dir: Path = DEEP_RESEARCH_DIR
    profile_cache_dir: Path = PROFILE_CACHE_DIR
    use_llm: bool = True
    model: str = DEFAULT_MODEL
    reasoning_effort: str = "medium"
    confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD

    def research(self, request: Any) -> GuidedProviderResult:
        parent = person_detail(self.db, request.slug)
        if not parent:
            raise StoreError(f"person not found: {request.slug}")
        row = self.research_row(request, parent, canonical_snapshot(self.db))
        result = run_research(ResearchRunParams(
            output_dir=self.research_dir,
            rows=(row,),
            processor=DEFAULT_PROCESSOR,
            db=self.db,
        ))
        if result.status not in {"completed", "no_work"}:
            raise StoreError(result.error or "guided research failed")
        research = ResearchResult.from_snapshot(
            identity_snapshot(self.db),
            handle=row.handle,
            candidate_key=request.row_key,
        )
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
        request: Any,
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
        snapshot = canonical_snapshot(self.db)
        canonical_parent = next(
            row for row in snapshot.parents if row.parent_id == parent_id
        )
        handle = ResearchHandle.for_parent(parent_id, canonical_parent.display_slug)
        propose_retargets(
            [EnrichmentQueueRow(
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
            )],
            db=self.db,
            use_llm=self.use_llm,
            owner_block=owner_background(snapshot),
            model=self.model,
            effort=self.reasoning_effort,
            confirm_threshold=self.confirm_threshold,
            profile_cache_dir=self.profile_cache_dir,
            source=ReviewSource.USER_GUIDANCE.value,
            provided_results={handle: research},
        )
        updated_parent = person_detail(self.db, parent_id)
        decision = next(
            (
                candidate
                for candidate in updated_parent.candidates
                if candidate.row_key == request.row_key
            ),
            None,
        ) if updated_parent else None
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
            "1", "true", "yes",
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
            str(
                decision.llm_reject_reason
                or "research result did not clear the identity threshold"
            ),
            candidate_url=url,
        )

    def research_row(
        self,
        request: Any,
        parent: ParentViewRow,
        snapshot: CanonicalSnapshot,
    ) -> ResearchQueueRow:
        parent_id = parent.parent_id
        canonical_parent = next(
            row for row in snapshot.parents if row.parent_id == parent_id
        )
        handle = ResearchHandle.for_parent(parent_id, canonical_parent.display_slug)
        candidate = next(
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
            linkedin_url=(
                request.linkedin_url or (candidate.url if candidate else "")
            ),
            verdict="",
            verdict_reason=candidate.reason if candidate else "",
            match_emails=tuple(request.match_emails),
            match_phones=tuple(request.match_phones),
            candidate_origin=False,
        )
        return build_queue_row(
            snapshot,
            row,
            owner_context=owner_background(snapshot),
            guidance=request.guidance,
        )

    def record(
        self,
        parent_id: str,
        request: Any,
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
        detail_json = json.dumps(
            {**item.as_dict(), "request": asdict(request)}, separators=(",", ":")
        )
        self.db.project_rows((GuidanceRow(
            parent_id,
            parent_id,
            request.guidance,
            guidance_state.value,
            request.row_key,
            request.submitted_at,
            item.new_url or None,
            detail_json,
        ),))
        return item
