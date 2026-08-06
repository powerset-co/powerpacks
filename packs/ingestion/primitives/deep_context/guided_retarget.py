"""Durable SQLite queue over the canonical identity-research contract.

Guided research differs from ordinary enrichment by one optional input: the
user's words.  The provider still receives the same canonical dossier/identity
packet, writes to the same fixed per-handle research directory, and therefore
reuses the same paid result when dossier plus guidance are unchanged.  Guidance
state in SQLite is the only queue/progress record; this module does not create a
second job row or enrichment manifest.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    DEEP_RESEARCH_DIR,
    PROFILE_CACHE_DIR,
)
from packs.ingestion.primitives.deep_context.db.people_views import person_detail
from packs.ingestion.primitives.deep_context.db.models import (
    CanonicalSnapshot,
    GuidanceRow,
    GuidanceState,
    RESEARCH_CONFIRM_THRESHOLD,
    ReviewSource,
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
from packs.ingestion.primitives.deep_context.research_reconcile.selection import (
    DEFAULT_PROCESSOR,
    build_queue,
)
from packs.ingestion.primitives.deep_context.research_result import ResearchResult
from packs.ingestion.primitives.deep_context.research_reconcile.judging import (
    propose_retargets,
)
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url


ACTIVE_STATES = {GuidanceState.PENDING.value, GuidanceState.RUNNING.value}
_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_%.\-]+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GuidanceRequest:
    slug: str
    pub: str
    name: str
    guidance: str
    person_ids: tuple[str, ...] = ()
    linkedin_url: str = ""
    queue_slug: str = ""
    submitted_at: str = ""
    match_emails: tuple[str, ...] = ()
    match_phones: tuple[str, ...] = ()


def linkedin_url_in_guidance(guidance: str) -> tuple[str, str]:
    match = _LINKEDIN_RE.search(guidance)
    if not match:
        return "", ""
    raw = match.group(0)
    url = normalize_linkedin_url(raw if raw.lower().startswith("http") else f"https://{raw}")
    pub = extract_public_identifier(url).lower()
    return (url, pub) if pub else ("", "")


class GuidedRetargetWorker:
    """Serial worker whose queue, progress, and result survive server restarts."""

    def __init__(
        self,
        db: Db,
        *,
        runner: Callable[[GuidanceRequest], dict[str, Any]] | None = None,
        on_change: Callable[[], None] | None = None,
        research_dir: Path = DEEP_RESEARCH_DIR,
        profile_cache_dir: Path = PROFILE_CACHE_DIR,
        use_llm: bool = True,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "medium",
        confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
    ) -> None:
        self.db = db
        self.runner = runner or self._research
        self.on_change = on_change or (lambda: None)
        self.research_dir = Path(research_dir)
        self.profile_cache_dir = Path(profile_cache_dir)
        self.use_llm = use_llm
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.confirm_threshold = confirm_threshold
        self._thread: threading.Thread | None = None
        self._pending: list[GuidanceRequest] = []

    def submit(self, request: GuidanceRequest) -> dict[str, Any]:
        parent = person_detail(self.db, request.queue_slug or request.slug)
        if not parent:
            raise StoreError(f"person not found: {request.queue_slug or request.slug}")
        parent_id = str(parent["parent_id"])
        active = any(
            row.get("handle") == parent_id and row.get("state") in ACTIVE_STATES
            for row in identity_snapshot(self.db).guidance
        )
        if active:
            raise StoreError(f"{request.name or request.slug} is already being retargeted")
        url, public_identifier = linkedin_url_in_guidance(request.guidance)
        if url:
            resolved = self.db.decide_identity(
                request.pub,
                "retarget",
                replacement_url=url,
                replacement_public_identifier=public_identifier,
            )
            item = self._record(
                parent_id, request, GuidanceState.APPLIED, "applied",
                "user-provided LinkedIn applied directly",
                new_url=url, resolved_pubs=resolved,
            )
            self.on_change()
            return item
        item = self._record(parent_id, request, GuidanceState.PENDING, "queued")
        self._enqueue(request)
        return item

    def resume(self) -> int:
        resumed = 0
        for row in identity_snapshot(self.db).guidance:
            if row.get("state") not in ACTIVE_STATES:
                continue
            detail = row.get("detail") or {}
            request_data = detail.get("request") if isinstance(detail, dict) else None
            if not isinstance(request_data, dict):
                continue
            values = dict(request_data)
            for key in ("person_ids", "match_emails", "match_phones"):
                values[key] = tuple(values.get(key) or ())
            request = GuidanceRequest(**values)
            self._enqueue(request)
            resumed += 1
        return resumed

    def _enqueue(self, request: GuidanceRequest) -> None:
        self._pending.append(request)
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._drain,
            name="guided-retarget",
            daemon=True,
        )
        self._thread.start()
        self.on_change()

    def _drain(self) -> None:
        while True:
            if not self._pending:
                self._thread = None
                return
            request = self._pending.pop(0)
            parent = person_detail(self.db, request.queue_slug or request.slug)
            if not parent:
                continue
            parent_id = str(parent["parent_id"])
            self._record(
                parent_id, request, GuidanceState.RUNNING,
                "researching", "Parallel research running",
            )
            self.on_change()
            try:
                result = self.runner(request)
                self._apply_provider_result(parent_id, parent, request, result)
            except BaseException as exc:
                self._record(
                    parent_id, request, GuidanceState.FAILED, "failed",
                    f"{type(exc).__name__}: {exc}"[:500],
                )
            self.on_change()

    def _research(self, request: GuidanceRequest) -> dict[str, Any]:
        parent = person_detail(self.db, request.queue_slug or request.slug)
        if not parent:
            raise StoreError(f"person not found: {request.queue_slug or request.slug}")
        row = self._research_row(request, parent, canonical_snapshot(self.db))
        run_params = ResearchRunParams(
            output_dir=self.research_dir,
            rows=(row,),
            processor=DEFAULT_PROCESSOR,
            db=self.db,
        )
        result = run_research(run_params)
        if str(result.get("status") or "") not in {"completed", "no_work"}:
            raise StoreError(str(result.get("error") or "guided research failed"))
        research = ResearchResult.from_snapshot(
            identity_snapshot(self.db),
            handle=row["handle"],
            candidate_key=request.pub,
        )
        if research is None:
            research = ResearchResult.from_payload({})
        profile = research.to_payload()
        social = profile.get("social") if isinstance(profile.get("social"), dict) else {}
        metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
        return {
            "new_url": social.get("linkedin_url") or profile.get("linkedin_url") or "",
            "detail": metadata.get("research_notes") or "Parallel research result applied",
            "research_result": research,
        }

    def _apply_provider_result(
        self,
        parent_id: str,
        parent: dict[str, Any],
        request: GuidanceRequest,
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
            return self._record(
                parent_id, request, GuidanceState.FAILED, "no_match",
                str(result.get("detail") or "no LinkedIn found"),
            )

        snapshot = canonical_snapshot(self.db)
        person_ids = tuple(request.person_ids) or tuple(parent.get("person_ids") or ())
        handle = request.queue_slug or request.slug

        propose_retargets(
            [{
                "parent_slug": handle, "parent_id": parent_id,
                "candidate_key": request.pub.lower(), "person_ids": list(person_ids),
                "name": request.name or str(parent.get("name") or ""),
                "linkedin": {"linkedin_url": request.linkedin_url},
                "match_emails": list(request.match_emails),
                "match_phones": list(request.match_phones),
            }],
            db=self.db, use_llm=self.use_llm,
            owner_block=owner_background(snapshot),
            model=self.model, effort=self.reasoning_effort,
            confirm_threshold=self.confirm_threshold,
            profile_cache_dir=self.profile_cache_dir,
            source=ReviewSource.USER_GUIDANCE.value,
            provided_results={handle: research},
        )
        decision = identity_snapshot(self.db).link_decisions.get(request.pub.lower()) or {}
        rejected = str(decision.get("llm_reject") or "").lower() in {"1", "true", "yes"}
        accepted = decision.get("action") == "retarget" and not rejected

        if accepted:
            detail = str(result.get("detail") or "research result applied")
            return self._record(
                parent_id, request, GuidanceState.APPLIED, "applied", detail,
                new_url=url, resolved_pubs=[request.pub],
            )

        reason = str(decision.get("llm_reject_reason") or
                     "research result did not clear the identity threshold")
        return self._record(
            parent_id, request, GuidanceState.FAILED, "no_match", reason,
            candidate_url=url,
        )

    def _research_row(
        self, request: GuidanceRequest, parent: dict[str, Any], snapshot: CanonicalSnapshot,
    ) -> dict[str, str]:
        candidates = parent.get("candidates") or []
        candidate = next(
            (
                item
                for item in candidates
                if str(item.get("row_key") or item.get("pub") or "").lower()
                == request.pub.lower()
            ),
            {},
        )
        subject = {
            "parent_slug": request.slug,
            "person_ids": list(request.person_ids or parent.get("person_ids") or ()),
            "candidate_key": request.pub,
            "name": request.name or str(parent.get("name") or ""),
            "linkedin": {
                "linkedin_url": request.linkedin_url or candidate.get("url") or ""
            },
            "verdict": {"reason": candidate.get("reason") or ""},
            "match_emails": list(request.match_emails),
            "match_phones": list(request.match_phones),
        }
        return build_queue(
            [subject],
            snapshot,
            guidance=request.guidance,
        )[0]

    def _record(
        self,
        parent_id: str,
        request: GuidanceRequest,
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
        }
        item.update(extra)
        detail = json.dumps({**item, "request": asdict(request)}, separators=(",", ":"))
        self.db.project_rows((
            GuidanceRow(
                parent_id,
                parent_id,
                request.guidance,
                guidance_state.value,
                request.pub,
                request.submitted_at,
                str(item.get("new_url") or "") or None,
                detail,
            ),
        ))
        return item
