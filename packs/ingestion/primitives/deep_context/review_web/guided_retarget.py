"""Durable SQLite queue for user-guided, file-first identity research."""

from __future__ import annotations

import csv
import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.imports.common import write_manifest
from packs.ingestion.primitives.deep_context.common import RECONCILE_DIR
from packs.ingestion.primitives.deep_context.db import views
from packs.ingestion.primitives.deep_context.db.models import (
    GuidanceRow,
    GuidanceState,
    JobKind,
    JobRow,
    JobStatus,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.projectors import project_manifest
from packs.ingestion.primitives.deep_context.deep_research_contacts import (
    ResearchRunParams,
    research_artifact_inventory,
    run_research,
)
from packs.ingestion.primitives.deep_context.reconcile_deep_research import (
    DEFAULT_PROCESSOR,
    QUEUE_FIELDS,
)
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url


GUIDED_DIR = RECONCILE_DIR / "retarget-guidance"
QUEUE_CSV = GUIDED_DIR / "research_queue.csv"
MANIFEST = GUIDED_DIR / "manifest.json"
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
    ) -> None:
        self.db = db
        self.runner = runner or self._research
        self.on_change = on_change or (lambda: None)
        self.out_dir = Path(out_dir)
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
            self._save(parent_id, request, GuidanceState.APPLIED.value, JobStatus.APPLIED.value, item)
            self.on_change()
            return item
        item = self._item(request, "queued")
        self._save(parent_id, request, GuidanceState.PENDING.value, JobStatus.QUEUED.value, item)
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
            self._save(parent_id, request, GuidanceState.RUNNING.value, JobStatus.RUNNING.value, running)
            self.on_change()
            try:
                result = self.runner(request)
                url = normalize_linkedin_url(str(result.get("new_url") or ""))
                if not url:
                    item = self._item(request, "no_match", str(result.get("detail") or "no LinkedIn found"))
                    self._save(parent_id, request, GuidanceState.FAILED.value, JobStatus.NO_MATCH.value, item)
                else:
                    public_identifier = extract_public_identifier(url).lower()
                    resolved = self.db.decide_identity(
                        request.pub,
                        "retarget",
                        replacement_url=url,
                        replacement_public_identifier=public_identifier,
                    )
                    item = self._item(request, "applied", str(result.get("detail") or "research result applied"))
                    item.update({"new_url": url, "resolved_pubs": resolved})
                    self._save(parent_id, request, GuidanceState.APPLIED.value, JobStatus.APPLIED.value, item)
            except BaseException as exc:
                item = self._item(request, "failed", f"{type(exc).__name__}: {exc}"[:500])
                self._save(parent_id, request, GuidanceState.FAILED.value, JobStatus.FAILED.value, item)
            self.on_change()

    def _research(self, request: GuidanceRequest) -> dict[str, Any]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        queue_csv = self.out_dir / QUEUE_CSV.name
        manifest = self.out_dir / MANIFEST.name
        email = request.match_emails[0] if request.match_emails else ""
        phone = request.match_phones[0] if request.match_phones else ""
        row = {
            "handle": request.slug,
            "source_parent_slug": request.slug,
            "source_person_ids": json.dumps(request.person_ids),
            "source_candidate_public_identifier": request.pub,
            "display_name": request.name,
            "bio": "",
            "known_info": request.guidance,
            "primary_email": email,
            "phone_e164": phone,
            "area_code": "",
            "source_channel": "user-guidance",
            "retarget_hint": request.guidance,
        }
        with queue_csv.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=QUEUE_FIELDS)
            writer.writeheader()
            writer.writerow(row)
        run_params = ResearchRunParams(
            input_csv=queue_csv,
            output_dir=self.out_dir,
            processor=DEFAULT_PROCESSOR,
        )
        result = run_research(run_params)
        if str(result.get("status") or "") not in {"completed", "no_work"}:
            raise StoreError(str(result.get("error") or "guided research failed"))
        projection_params = ResearchRunParams(
            input_csv=queue_csv,
            output_dir=self.out_dir,
            processor=DEFAULT_PROCESSOR,
            manifest=str(manifest),
            db=self.db,
        )
        inventory = research_artifact_inventory(projection_params)
        write_manifest(
            self.out_dir.name,
            {
                "stage": "guided-retarget",
                "status": "completed" if inventory else "no_match",
                "counts": {"total": 1, "completed": int(bool(inventory)), "failed": 0},
                "artifacts": inventory,
                "updated_at": now_iso(),
            },
            import_dir=self.out_dir.parent,
        )
        project_manifest(self.db, manifest)
        parent = views.person_detail(self.db, request.queue_slug or request.slug) or {}
        candidate = next(
            (
                row
                for row in parent.get("candidates") or []
                if str(row.get("row_key") or "").lower() == request.pub.lower()
            ),
            {},
        )
        return {
            "new_url": candidate.get("new_url") or "",
            "detail": candidate.get("reason") or "Parallel research result applied",
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
        job_status: str,
        item: dict[str, Any],
    ) -> None:
        detail = json.dumps({**item, "request": asdict(request)}, separators=(",", ":"))
        finished = now_iso() if job_status not in {JobStatus.QUEUED.value, JobStatus.RUNNING.value} else None
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
        self.db.save_state(
            JobRow(
                f"guided-retarget:{parent_id}",
                JobKind.GUIDED_RETARGET.value,
                job_status,
                parent_id,
                request.pub,
                completed_count=int(finished is not None),
                total_count=1,
                error=str(item.get("detail") or "") if job_status == JobStatus.FAILED.value else None,
                result_json=detail,
                started_at=request.submitted_at,
                finished_at=finished,
            )
        )
