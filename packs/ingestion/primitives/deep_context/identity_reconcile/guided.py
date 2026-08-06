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
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
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
    build_queue,
)
from packs.ingestion.primitives.deep_context.research_result import ResearchResult
from packs.ingestion.schemas.people_schema import normalize_linkedin_url


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

    def research(self, request: Any) -> dict[str, Any]:
        parent = person_detail(self.db, request.queue_slug or request.slug)
        if not parent:
            raise StoreError(f"person not found: {request.queue_slug or request.slug}")
        row = self.research_row(request, parent, canonical_snapshot(self.db))
        result = run_research(ResearchRunParams(
            output_dir=self.research_dir,
            rows=(row,),
            processor=DEFAULT_PROCESSOR,
            db=self.db,
        ))
        if str(result.get("status") or "") not in {"completed", "no_work"}:
            raise StoreError(str(result.get("error") or "guided research failed"))
        research = ResearchResult.from_snapshot(
            identity_snapshot(self.db),
            handle=row["handle"],
            candidate_key=request.pub,
        ) or ResearchResult.from_payload({})
        profile = research.to_payload()
        social = profile.get("social") if isinstance(profile.get("social"), dict) else {}
        metadata = (
            profile.get("metadata")
            if isinstance(profile.get("metadata"), dict)
            else {}
        )
        return {
            "new_url": social.get("linkedin_url") or profile.get("linkedin_url") or "",
            "detail": metadata.get("research_notes") or "Parallel research result applied",
            "research_result": research,
        }

    def apply_provider_result(
        self,
        parent_id: str,
        parent: dict[str, Any],
        request: Any,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Judge and project a provider URL; human-pasted URLs bypass this path."""
        research = result.get("research_result")
        if not isinstance(research, ResearchResult):
            artifact = result.get("research_profile")
            payload = artifact if isinstance(artifact, dict) else {
                "person": {
                    "full_name": request.name,
                    "confidence": result.get("confidence") or 0,
                },
                "social": {"linkedin_url": result.get("new_url") or ""},
                "metadata": {"research_notes": result.get("detail") or ""},
            }
            research = ResearchResult.from_payload(payload)
        url = normalize_linkedin_url(research.linkedin_url)
        if not url:
            return self.record(
                parent_id,
                request,
                GuidanceState.FAILED,
                "no_match",
                str(result.get("detail") or "no LinkedIn found"),
            )
        person_ids = tuple(request.person_ids) or tuple(parent.get("person_ids") or ())
        snapshot = canonical_snapshot(self.db)
        canonical_parent = next(
            row for row in snapshot.parents if row.parent_id == parent_id
        )
        handle = ResearchHandle.for_parent(parent_id, canonical_parent.display_slug)
        propose_retargets(
            [{
                "parent_slug": handle,
                "parent_id": parent_id,
                "candidate_key": request.pub.lower(),
                "person_ids": list(person_ids),
                "name": request.name or str(parent.get("name") or ""),
                "linkedin": {"linkedin_url": request.linkedin_url},
                "match_emails": list(request.match_emails),
                "match_phones": list(request.match_phones),
            }],
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
        decision = identity_snapshot(self.db).link_decisions.get(
            request.pub.lower()
        ) or {}
        rejected = str(decision.get("llm_reject") or "").lower() in {
            "1", "true", "yes",
        }
        if decision.get("action") == "retarget" and not rejected:
            return self.record(
                parent_id,
                request,
                GuidanceState.APPLIED,
                "applied",
                str(result.get("detail") or "research result applied"),
                new_url=url,
                resolved_pubs=[request.pub],
            )
        return self.record(
            parent_id,
            request,
            GuidanceState.FAILED,
            "no_match",
            str(
                decision.get("llm_reject_reason")
                or "research result did not clear the identity threshold"
            ),
            candidate_url=url,
        )

    def research_row(
        self,
        request: Any,
        parent: dict[str, Any],
        snapshot: CanonicalSnapshot,
    ) -> dict[str, str]:
        parent_id = str(parent.get("parent_id") or "")
        canonical_parent = next(
            row for row in snapshot.parents if row.parent_id == parent_id
        )
        handle = ResearchHandle.for_parent(parent_id, canonical_parent.display_slug)
        candidate = next((
            item for item in parent.get("candidates") or []
            if str(item.get("row_key") or item.get("pub") or "").lower()
            == request.pub.lower()
        ), {})
        return build_queue([{
            "parent_id": parent_id,
            "parent_slug": handle,
            "person_ids": list(request.person_ids or parent.get("person_ids") or ()),
            "candidate_key": request.pub,
            "candidate_exists": bool(candidate),
            "name": request.name or str(parent.get("name") or ""),
            "linkedin": {
                "linkedin_url": request.linkedin_url or candidate.get("url") or ""
            },
            "verdict": {"reason": candidate.get("reason") or ""},
            "match_emails": list(request.match_emails),
            "match_phones": list(request.match_phones),
        }], snapshot, guidance=request.guidance)[0]

    def record(
        self,
        parent_id: str,
        request: Any,
        guidance_state: GuidanceState,
        state: str,
        detail: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        item = {
            "slug": request.slug,
            "pub": request.pub.lower(),
            "queue_slug": request.queue_slug or request.slug,
            "name": request.name,
            "guidance": request.guidance,
            "state": state,
            "detail": detail,
            "submitted_at": request.submitted_at,
            "updated_at": now_iso(),
            **extra,
        }
        detail_json = json.dumps(
            {**item, "request": asdict(request)}, separators=(",", ":")
        )
        self.db.project_rows((GuidanceRow(
            parent_id,
            parent_id,
            request.guidance,
            guidance_state.value,
            request.pub,
            request.submitted_at,
            str(item.get("new_url") or "") or None,
            detail_json,
        ),))
        return item
