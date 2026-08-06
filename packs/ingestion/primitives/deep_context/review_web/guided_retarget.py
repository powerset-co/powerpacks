"""Durable SQLite queue over the canonical identity-research contract.

Guided research differs from ordinary enrichment by one optional input: the
user's words.  The provider still receives the same canonical dossier/identity
packet, writes to the same fixed per-handle research directory, and therefore
reuses the same paid result when dossier plus guidance are unchanged.  Guidance
state in SQLite is the only queue/progress record; this module does not create a
second job row or enrichment manifest.
"""

from __future__ import annotations

import csv
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
    FACTS_DIR,
    PROFILE_CACHE_DIR,
    RAW_DIR,
    RECONCILE_DIR,
    load_owner,
    owner_background_block,
)
from packs.ingestion.primitives.deep_context.db import views
from packs.ingestion.primitives.deep_context.db.models import (
    GuidanceRow,
    GuidanceState,
    ReviewSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.deep_research_contacts import (
    ResearchRunParams,
    run_research,
)
from packs.ingestion.primitives.deep_context.research_reconcile.selection import (
    DEFAULT_PROCESSOR,
    QUEUE_FIELDS,
)
from packs.ingestion.primitives.deep_context.db.models import RESEARCH_CONFIRM_THRESHOLD
from packs.ingestion.primitives.deep_context.identity_evidence import (
    ResearchEvaluation,
    evaluate_research_candidate,
    prefer_cached_profile,
)
from packs.ingestion.primitives.deep_context.dossier_evidence import DossierEvidence
from packs.ingestion.primitives.deep_context.research_result import ResearchResult
from packs.ingestion.primitives.deep_context.identity_reconcile.queue import linkedin_view
from packs.ingestion.primitives.deep_context.identity_reconcile.results import upsert_retargets
from packs.ingestion.primitives.enrich.rapidapi_client import hydrate_profiles
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url


GUIDED_DIR = RECONCILE_DIR / "retarget-guidance"
QUEUE_CSV = GUIDED_DIR / "research_queue.csv"
RESEARCH_RESULT = "01_research_parallel.json"
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
    candidate_pubs: tuple[str, ...] = ()
    synthetic_pubs: tuple[str, ...] = ()
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
        out_dir: Path = GUIDED_DIR,
        research_dir: Path = DEEP_RESEARCH_DIR,
        facts_dir: Path = FACTS_DIR,
        raw_dir: Path = RAW_DIR,
        profile_cache_dir: Path = PROFILE_CACHE_DIR,
        use_llm: bool = True,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "medium",
        confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
    ) -> None:
        self.db = db
        self.runner = runner or self._research
        self.on_change = on_change or (lambda: None)
        self.out_dir = Path(out_dir)
        self.research_dir = Path(research_dir)
        self.facts_dir = Path(facts_dir)
        self.raw_dir = Path(raw_dir)
        self.profile_cache_dir = Path(profile_cache_dir)
        self.use_llm = use_llm
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.confirm_threshold = confirm_threshold
        self._thread: threading.Thread | None = None
        self._pending: list[GuidanceRequest] = []

    def submit(self, request: GuidanceRequest) -> dict[str, Any]:
        parent = views.person_detail(self.db, request.queue_slug or request.slug)
        if not parent:
            raise StoreError(f"person not found: {request.queue_slug or request.slug}")
        parent_id = str(parent["parent_id"])
        active = any(
            row.get("handle") == parent_id and row.get("state") in ACTIVE_STATES
            for row in views.retarget_snapshot(self.db)["guidance"]
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
            item = self._item(request, "applied", "user-provided LinkedIn applied directly")
            item.update({"new_url": url, "resolved_pubs": resolved})
            self._save(parent_id, request, GuidanceState.APPLIED.value, item)
            self.on_change()
            return item
        item = self._item(request, "queued")
        self._save(parent_id, request, GuidanceState.PENDING.value, item)
        self._enqueue(request)
        return item

    def resume(self) -> int:
        resumed = 0
        for row in views.retarget_snapshot(self.db)["guidance"]:
            if row.get("state") not in ACTIVE_STATES:
                continue
            detail = row.get("detail") or {}
            request_data = detail.get("request") if isinstance(detail, dict) else None
            if not isinstance(request_data, dict):
                continue
            request = GuidanceRequest(
                **{
                    **request_data,
                    "person_ids": tuple(request_data.get("person_ids") or ()),
                    "candidate_pubs": tuple(request_data.get("candidate_pubs") or ()),
                    "synthetic_pubs": tuple(request_data.get("synthetic_pubs") or ()),
                    "match_emails": tuple(request_data.get("match_emails") or ()),
                    "match_phones": tuple(request_data.get("match_phones") or ()),
                }
            )
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
            parent = views.person_detail(self.db, request.queue_slug or request.slug)
            if not parent:
                continue
            parent_id = str(parent["parent_id"])
            running = self._item(request, "researching", "Parallel research running")
            self._save(parent_id, request, GuidanceState.RUNNING.value, running)
            self.on_change()
            try:
                result = self.runner(request)
                self._apply_provider_result(parent_id, parent, request, result)
            except BaseException as exc:
                item = self._item(request, "failed", f"{type(exc).__name__}: {exc}"[:500])
                self._save(parent_id, request, GuidanceState.FAILED.value, item)
            self.on_change()

    def _research(self, request: GuidanceRequest) -> dict[str, Any]:
        parent = views.person_detail(self.db, request.queue_slug or request.slug)
        if not parent:
            raise StoreError(f"person not found: {request.queue_slug or request.slug}")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        queue_csv = self.out_dir / QUEUE_CSV.name
        row = self._research_row(request, parent)
        with queue_csv.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=QUEUE_FIELDS)
            writer.writeheader()
            writer.writerow(row)
        run_params = ResearchRunParams(
            input_csv=queue_csv,
            output_dir=self.research_dir,
            processor=DEFAULT_PROCESSOR,
        )
        result = run_research(run_params)
        if str(result.get("status") or "") not in {"completed", "no_work"}:
            raise StoreError(str(result.get("error") or "guided research failed"))
        research = ResearchResult.load(self.research_dir / request.slug / RESEARCH_RESULT)
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
            item = self._item(request, "no_match", str(result.get("detail") or "no LinkedIn found"))
            self._save(parent_id, request, GuidanceState.FAILED.value, item)
            return item

        public_identifier = extract_public_identifier(url).lower()
        hydrate_profiles([(public_identifier, url)], self.profile_cache_dir)
        cached = linkedin_view(
            {"public_identifier": public_identifier, "linkedin_url": url},
            self.profile_cache_dir,
        )
        profile = prefer_cached_profile(research.identity_profile(), cached)
        dossier = DossierEvidence.load(
            request.person_ids, self.facts_dir, self.raw_dir
        ).as_judge_dict()
        owner = load_owner()
        evaluation: ResearchEvaluation = evaluate_research_candidate(
            dossier,
            profile,
            name=request.name or str(parent.get("name") or ""),
            match_emails=list(request.match_emails),
            match_phones=list(request.match_phones),
            confidence=research.confidence,
            unverified=research.unverified,
            use_llm=self.use_llm,
            owner_block=owner_background_block(owner) if owner else "",
            model=self.model,
            effort=self.reasoning_effort,
            confirm_threshold=self.confirm_threshold,
        )
        proposal = {
            "old_public_identifier": request.pub,
            "new_linkedin_url": url,
            "new_public_identifier": public_identifier,
            "confidence": research.confidence,
            "reason": research.reason or result.get("detail") or "guided research",
            "source": ReviewSource.USER_GUIDANCE.value,
            **evaluation.projection_fields,
        }
        projected = upsert_retargets(self.db, [proposal])
        if evaluation.accepted and projected.get("proposed"):
            detail = str(evaluation.verdict.get("reason") or result.get("detail") or "research result applied")
            item = self._item(request, "applied", detail)
            item.update({"new_url": url, "resolved_pubs": [request.pub]})
            self._save(parent_id, request, GuidanceState.APPLIED.value, item)
            return item

        reason = str(
            evaluation.verdict.get("reason")
            or evaluation.projection_fields.get("llm_reject_reason")
            or "research result did not clear the identity threshold"
        )
        item = self._item(request, "no_match", reason)
        item["candidate_url"] = url
        self._save(parent_id, request, GuidanceState.FAILED.value, item)
        return item

    @staticmethod
    def _research_row(request: GuidanceRequest, parent: dict[str, Any]) -> dict[str, str]:
        candidates = parent.get("candidates") or []
        emails = sorted({
            str(value).strip()
            for candidate in candidates
            for value in candidate.get("match_emails") or []
            if str(value).strip()
        } | {str(value).strip() for value in request.match_emails if str(value).strip()})
        phones = sorted({
            str(value).strip()
            for candidate in candidates
            for value in candidate.get("match_phones") or []
            if str(value).strip()
        } | {str(value).strip() for value in request.match_phones if str(value).strip()})
        identity = {
            "display_name": request.name or str(parent.get("name") or ""),
            "emails": emails,
            "phones": phones,
            "current_linkedin": normalize_linkedin_url(request.linkedin_url),
        }
        dossier_path = Path(str(parent.get("dossier_path") or ""))
        try:
            dossier = dossier_path.read_text(encoding="utf-8") if dossier_path.is_file() else ""
        except OSError:
            dossier = ""
        packet = "Identity\n" + json.dumps(identity, ensure_ascii=False, sort_keys=True)
        if dossier.strip():
            packet += "\n\nDossier\n" + dossier.strip()
        return {
            "handle": request.slug,
            "source_parent_slug": request.queue_slug or request.slug,
            "source_person_ids": json.dumps(request.person_ids, ensure_ascii=False),
            "source_candidate_public_identifier": request.pub,
            "display_name": request.name,
            "bio": packet,
            "known_info": "",
            "primary_email": emails[0] if emails else "",
            "phone_e164": phones[0] if phones else "",
            "area_code": "",
            "source_channel": "email" if emails else "phone" if phones else "unknown",
            "retarget_hint": request.guidance.strip(),
        }

    @staticmethod
    def _item(request: GuidanceRequest, state: str, detail: str = "") -> dict[str, Any]:
        return {
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

    def _save(
        self,
        parent_id: str,
        request: GuidanceRequest,
        guidance_state: str,
        item: dict[str, Any],
    ) -> None:
        detail = json.dumps({**item, "request": asdict(request)}, separators=(",", ":"))
        self.db.save_state(
            GuidanceRow(
                parent_id,
                parent_id,
                request.guidance,
                guidance_state,
                request.pub,
                request.submitted_at,
                str(item.get("new_url") or "") or None,
                detail,
            )
        )
