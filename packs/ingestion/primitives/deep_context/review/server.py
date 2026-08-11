"""Frozen Deep Context HTTP transport over canonical SQLite state."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Protocol

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db.identity_views import (
    linkedin_parents,
    linkedin_queue,
)
from packs.ingestion.primitives.deep_context.db.models import (
    PARENT_WORTH_PREFIX,
    RESEARCH_CONFIRM_THRESHOLD,
)
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.db.people_views import (
    CandidateViewRow,
    ParentViewRow,
    person_detail,
)
from packs.ingestion.primitives.deep_context.db.worth_views import worth_queue, worth_rows
from packs.ingestion.primitives.deep_context.db.view_models import WorthRow
from packs.ingestion.primitives.deep_context.enrich.enrichment_pipeline import (
    EnrichmentPipeline,
)
from packs.ingestion.primitives.deep_context.review.feedback import (
    FEEDBACK_ACTIONS,
    build_feedback_request,
    feedback_alert,
    post_feedback_quietly,
    submit_directory_feedback,
)
from packs.ingestion.primitives.deep_context.review.guided_retarget import GuidedRetargetWorker
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guidance import GuidanceRequest
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.guided import GuidanceOutcome
from packs.ingestion.primitives.deep_context.review.rendering import (
    GO_BACK_HTML,
    REVIEW_CSS,
    REVIEW_JS,
    _carousel_nav,
    _phase_view,
    _primary_candidate,
    _step,
    directory_page_html,
    linkedin_finished_body,
    markdown_to_html,
    page_html,
    render_decision_table,
    render_decision_tabs,
    render_enrichment,
    render_linkedin_card,
    render_person_detail,
    render_worth_card,
    worth_finished_body,
    worth_pending_entries,
    worth_search_html,
)
from packs.ingestion.primitives.deep_context.review.sqlite_adapter import (
    STAGES,
    GuidanceViewRow,
    SqliteReviewAdapter,
)


ESTIMATED_COST_USD = 0.06
# Non-terminal wire-level progress codes (GuidanceOutcome.state / GuidanceViewRow.state) —
# not the coarse persisted GuidanceState set in identity_reconcile/guidance.py.
IN_FLIGHT_RETARGET_STATES = {"queued", "researching", "judging", "hydrating"}
AUTH_SCRIPT = Path(__file__).resolve().parents[5] / "packs/powerset/primitives/auth/auth.py"
_auth_proc: subprocess.Popen[bytes] | None = None


class GuidedRetargets(Protocol):
    def resume(self) -> int: ...

    def submit(self, request: GuidanceRequest) -> GuidanceOutcome: ...


def _failed_notes(items: list[GuidanceViewRow]) -> dict[str, str]:
    latest: dict[str, GuidanceViewRow] = {}
    for item in items:
        slug = item.slug.lower()
        if slug and slug not in latest:
            latest[slug] = item
    return {slug: item.detail or "the job did not finish" for slug, item in latest.items() if item.state == "failed"}


def _value(params: dict[str, list[str]], key: str, default: str = "") -> str:
    return str((params.get(key) or [default])[0])


def _index(params: dict[str, list[str]], size: int) -> int:
    try:
        return max(0, int(_value(params, "index", "0"))) % size
    except ValueError:
        return 0


def _excluded(params: dict[str, list[str]]) -> set[str]:
    values = _value(params, "exclude").split(",")
    return {value.strip().lower() for value in values if value.strip()}


def start_auth_login() -> str:
    global _auth_proc
    if _auth_proc is not None and _auth_proc.poll() is None:
        return "already_running"
    _auth_proc = subprocess.Popen(
        [sys.executable, str(AUTH_SCRIPT), "login"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return "login_started"


def make_handler(
    *,
    db: Db,
    confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
    agent_notifier: Callable[[], object] | None = None,
    run_jobs: bool = False,
    guided_retargets: GuidedRetargets | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build the frozen handler over an explicit supported Deep Context database."""
    api = SqliteReviewAdapter(db, confirm_threshold)
    if api.snapshot().progress.total == 0:
        raise StoreError("Deep Context database is empty; run bin/deep-context migrate-sqlite")
    retargets_enabled = bool(run_jobs or guided_retargets)
    sequence = 0

    def notify() -> None:
        nonlocal sequence
        sequence += 1

    def wake_agent() -> None:
        if agent_notifier:
            try:
                agent_notifier()
            except Exception:
                pass

    if guided_retargets is None and run_jobs:
        guided_retargets = GuidedRetargetWorker(db, on_change=notify)
        guided_retargets.resume()
    enrichment_jobs = EnrichmentPipeline(
        db,
        api.confirm_threshold,
        on_change=notify,
        on_finish=wake_agent,
    )
    job_running, spawn_job = enrichment_jobs.running, enrichment_jobs.start

    def parent_hit(
        submitted_key: str,
        slug: str = "",
    ) -> tuple[str, ParentViewRow, CandidateViewRow] | None:
        resolved = api.resolve_candidate(submitted_key)
        if not resolved:
            return None
        row_key, parent = resolved
        candidate = api.candidate(parent, row_key)
        if not parent or not candidate:
            return None
        if slug and parent.slug != slug:
            raise StoreError("stale or mismatched person card")
        return row_key, parent, candidate

    def worth_body(params: dict[str, list[str]]) -> str | None:
        queue = worth_queue(db)
        pick = _value(params, "pick").strip().lower()
        excluded = _excluded(params)
        if pick:
            queue = [p for p in queue if p.key.lower() == pick]
            if not queue:
                return None
        queue = [p for p in queue if p.key.lower() not in excluded]
        queue.sort(key=lambda p: p.name.lower())
        if not queue:
            state = api.snapshot(job_running=job_running())
            progress = state.progress
            return worth_finished_body(progress, auto_continue=bool(progress.worth_pending))
        index = _index(params, len(queue))
        selected = queue[index]
        parent = person_detail(db, selected.parent_id)
        if parent is None:
            return None
        card = render_worth_card(parent)
        if _value(params, "debug") == "1":
            return (
                f"<div class='carousel-shell' data-queue-index='{index}' "
                f"data-queue-total='{len(queue)}'>{_carousel_nav()}{card}</div>"
            )
        return card

    def linkedin_body(params: dict[str, list[str]]) -> str:
        queue = linkedin_queue(db)
        excluded = _excluded(params)
        inflight = {item.slug.lower() for item in api.retargets() if item.state in IN_FLIGHT_RETARGET_STATES}
        queue = [p for p in queue if p.slug.lower() not in excluded | inflight]
        if not queue:
            state = api.snapshot(job_running=job_running())
            progress = state.progress
            completed = not progress.linkedin_pending
            return linkedin_finished_body(
                progress,
                linkedin_complete=completed,
                retargets_in_flight=len(inflight),
                auto_continue=not completed,
            )
        index = _index(params, len(queue))
        parent = queue[index]
        card = render_linkedin_card(
            parent,
            parent.candidates,
            failure_note=_failed_notes(api.retargets()).get(parent.slug, ""),
        )
        return (
            f"<div class='linkedin-stage' data-queue-index='{index}' "
            f"data-queue-total='{len(queue)}'>{_carousel_nav()}{card}</div>"
            if _value(params, "debug") == "1"
            else card
        )

    def full_page(params: dict[str, list[str]]) -> bytes:
        state = api.snapshot(job_running=job_running())
        progress = state.progress
        worth_rows = linkedin_parents(db)
        view = _phase_view(params)
        preview = _value(params, "preview") == "1"
        enrichment = api.enrichment(state)
        if view == "worth":
            tab = _value(params, "view", "review").lower()
            tab = tab if tab in {"review", "yes", "no"} else "review"
            tabs = render_decision_tabs(progress, tab, preview=preview)
            if tab == "review":
                body = worth_body(params) or ""
                pending = worth_pending_entries(worth_queue(db))
                search = worth_search_html("review", pending) if pending else ""
            else:
                body = render_decision_table(worth_rows, tab)
                search = ""
            content = f"<div class='worth-stage'>{tabs}{search}<div class='worth-panel'>{body}</div></div>"
        elif view == "enrich":
            content = render_enrichment(enrichment)
        elif view == "linkedin":
            content = (
                "<div class='linkedin-stage'><div class='linkedin-panel' "
                f"data-linkedin-panel>{linkedin_body(params)}</div></div>"
            )
        else:
            content = (
                "<div class='empty-state done'><div class='empty-mark'>✓</div><h2>All set</h2>"
                f"<p>{progress.linkedin_done} identities checked · {progress.rejected} rejected</p>"
                f"{GO_BACK_HTML}</div>"
            )
        active = {"worth": 0, "enrich": 1, "linkedin": 2, "done": 2}[view]
        specs = (
            (
                1,
                "Review Decisions",
                active == 0,
                not progress.worth_pending,
                progress.worth_pending,
                "/?stage=worth&preview=1",
            ),
            (
                2,
                "Enrich Contacts",
                active == 1,
                enrichment.status == "completed",
                enrichment.counts.pending,
                "/?stage=enrich&preview=1",
            ),
            (
                3,
                "Check LinkedIn",
                active == 2,
                not progress.linkedin_pending,
                progress.linkedin_pending,
                "/?stage=linkedin&preview=1",
            ),
        )
        steps = "<i class='step-line'></i>".join(_step(*spec) for spec in specs)
        return page_html(
            {"worth": "Add People", "enrich": "Enrich Contacts", "linkedin": "Check LinkedIn", "done": "All Set"}[view],
            view,
            content,
            preview=preview,
            external_updates=view in {"enrich", "done"},
            state_token=state.state_token,
            enrichment_status=enrichment.status,
            stepper=steps,
        )

    class Handler(BaseHTTPRequestHandler):
        def send_bytes(
            self,
            body: bytes,
            content_type: str = "text/html; charset=utf-8",
            status: int = 200,
            *,
            cache: str = "no-store",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            self.send_bytes(json.dumps(payload).encode(), "application/json; charset=utf-8", status)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/healthz":
                return self.send_bytes(b"ok", "text/plain")
            if parsed.path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                seen = -1
                try:
                    self.wfile.write(b"retry: 2000\n\n")
                    while True:
                        if sequence == seen:
                            time.sleep(1)
                        current = sequence
                        self.wfile.write(
                            (
                                f"data: {json.dumps({'seq': current, 'job': None})}\n\n"
                                if current != seen
                                else ": ping\n\n"
                            ).encode()
                        )
                        seen = current
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            if parsed.path == "/api/status":
                return self.send_json(api.status(job_running=job_running()))
            if parsed.path == "/api/enrichment":
                return self.send_json(api.enrichment().as_dict())
            if parsed.path == "/api/retargets":
                return self.send_json(
                    {
                        "items": [item.as_dict() for item in api.retargets()],
                        "enabled": retargets_enabled,
                        "estimated_cost_usd": ESTIMATED_COST_USD,
                        "feedback_alert": feedback_alert().as_dict(),
                    }
                )
            assets = {
                "/assets/reconcile-review.css": (REVIEW_CSS, "text/css; charset=utf-8"),
                "/assets/reconcile-review.js": (REVIEW_JS, "text/javascript; charset=utf-8"),
            }
            if parsed.path in assets:
                path, kind = assets[parsed.path]
                return self.send_bytes(path.read_bytes(), kind, cache="no-cache")
            if parsed.path == "/api/dossier":
                parent = person_detail(db, _value(params, "slug"))
                body = markdown_to_html(parent.dossier_body if parent else "")
                return self.send_bytes(body.encode())
            if parsed.path == "/api/worth-card":
                body = worth_body(params)
                if body is None:
                    return self.send_bytes(b"gone", "text/plain; charset=utf-8", 404)
                return self.send_bytes(body.encode())
            if parsed.path == "/api/linkedin-card":
                return self.send_bytes(linkedin_body(params).encode())
            if parsed.path == "/api/person":
                parent = person_detail(db, _value(params, "slug").lower())
                if not parent:
                    return self.send_bytes(b"not found", "text/plain", 404)
                return self.send_bytes(render_person_detail(parent).encode())
            if parsed.path == "/directory":
                handoff = api.snapshot().next_action == "realize"
                return self.send_bytes(directory_page_html(linkedin_parents(db), params, handoff=handoff))
            if parsed.path == "/api/avatar":
                try:
                    row_key = api.resolve_row_key(_value(params, "pub"))
                except StoreError as exc:
                    return self.send_bytes(str(exc).encode(), "text/plain; charset=utf-8", 400)
                avatar: tuple[bytes, str] | None = api.avatar(row_key) if row_key else None
                if not avatar:
                    return self.send_bytes(b"not found", "text/plain", 404)
                return self.send_bytes(avatar[0], avatar[1], cache="private, max-age=86400")
            if parsed.path != "/":
                return self.send_bytes(b"not found", "text/plain", 404)
            return self.send_bytes(full_page(params))

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            routes = {"/decide", "/worth", "/complete", "/approve-enrichment", "/retarget", "/feedback", "/auth/login"}
            if parsed.path not in routes:
                return self.send_bytes(b"not found", "text/plain", 404)
            origin = (self.headers.get("Origin") or "").strip()
            if origin and (urllib.parse.urlparse(origin).hostname or "").lower() not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                return self.send_bytes(b"cross-origin request rejected", "text/plain", 403)
            length = min(int(self.headers.get("Content-Length", "0")), 32_768)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            pub = (form.get("pub") or [""])[0]
            if parsed.path == "/auth/login":
                return self.send_json({"ok": True, "status": start_auth_login()})
            if parsed.path == "/approve-enrichment":
                try:
                    enrichment = api.approve_enrichment()
                except (KeyError, StoreError, ValueError) as exc:
                    return self.send_bytes(str(exc).encode(), "text/plain; charset=utf-8", 409)
                approval = enrichment.approval
                if not approval:
                    return self.send_json({"ok": True, "enrichment": enrichment.as_dict()})
                if not run_jobs:
                    return self.send_bytes(
                        b"enrichment job execution is disabled",
                        "text/plain; charset=utf-8",
                        409,
                    )
                try:
                    budget = float(approval.approved_budget_usd)
                except (TypeError, ValueError) as exc:
                    return self.send_bytes(str(exc).encode(), "text/plain; charset=utf-8", 409)
                total_count = enrichment.counts.total
                launched = spawn_job(
                    total_count,
                    budget,
                    enrichment.selection.fingerprint,
                )
                if not launched:
                    return self.send_json(
                        {
                            "ok": True,
                            "enrichment": api.enrichment().as_dict(),
                        }
                    )
                wake_agent()
                return self.send_json({"ok": True, "enrichment": enrichment.as_dict()})
            if parsed.path == "/complete":
                stage = (form.get("stage") or [""])[0].strip().lower()
                if stage not in STAGES:
                    error = StoreError(f"unknown review stage: {stage}")
                    return self.send_bytes(str(error).encode(), "text/plain; charset=utf-8", 409)
                state = api.snapshot(job_running=job_running())
                manifest = {**api.manifest(stage, state=state).as_dict(), "status": "completed"}
                notify()
                wake_agent()
                return self.send_json(
                    {
                        "ok": True,
                        "manifest": manifest,
                        "progress": asdict(state.progress),
                    }
                )
            if parsed.path == "/feedback":
                comment = (form.get("comment") or [""])[0].strip()
                action = (form.get("action") or [""])[0].strip()
                if not comment or len(comment) > 4000:
                    return self.send_bytes(b"comment must be 1-4000 characters", "text/plain", 400)
                if action not in FEEDBACK_ACTIONS:
                    return self.send_bytes(b"unknown feedback action", "text/plain", 400)
                slug = (form.get("parent_slug") or [""])[0].strip()
                if pub.startswith(PARENT_WORTH_PREFIX):
                    parent = person_detail(db, pub.removeprefix(PARENT_WORTH_PREFIX))
                    worth_key = parent.worth_row.key if parent else ""
                    if worth_key != pub:
                        return self.send_bytes(b"review row not found", "text/plain", 404)
                    candidate = None
                else:
                    try:
                        hit: tuple[str, ParentViewRow, CandidateViewRow] | None = parent_hit(pub, slug) if pub else None
                    except StoreError as exc:
                        return self.send_bytes(str(exc).encode(), "text/plain", 400)
                    if pub and not hit:
                        return self.send_bytes(b"review row not found", "text/plain", 404)
                    parent = hit[1] if hit else person_detail(db, slug)
                    candidate: CandidateViewRow | None = (
                        hit[2] if hit else _primary_candidate(parent) if parent else None
                    )
                if not parent:
                    return self.send_bytes(b"person not found", "text/plain", 404)
                feedback = submit_directory_feedback(
                    build_feedback_request(
                        parent, candidate, action=action, comment=comment, retarget_items=api.retargets()
                    )
                )
                status = 200 if feedback.status == "submitted" else 502
                return self.send_json(
                    {"ok": status == 200, **feedback.as_dict()},
                    status,
                )
            if parsed.path == "/retarget":
                guidance = (form.get("guidance") or [""])[0].strip()
                slug = (form.get("parent_slug") or [""])[0].strip()
                if not guidance or len(guidance) > 2000:
                    return self.send_bytes(b"guidance must be 1-2000 characters", "text/plain", 400)
                if not retargets_enabled:
                    return self.send_bytes(b"in-app jobs are disabled on this server", "text/plain", 503)
                if pub:
                    try:
                        hit = parent_hit(pub, slug)
                    except StoreError as exc:
                        return self.send_bytes(str(exc).encode(), "text/plain", 400)
                    if not hit:
                        return self.send_bytes(b"review row not found", "text/plain", 404)
                    row_key, parent, candidate = hit
                else:
                    parent = person_detail(db, slug)
                    if not parent:
                        return self.send_bytes(b"person not found", "text/plain", 404)
                    if not parent.person_ids:
                        return self.send_bytes(b"person has no research key", "text/plain", 400)
                    row_key = parent.person_ids[0]
                    candidate = None
                request = GuidanceRequest(
                    slug=parent.slug or slug,
                    row_key=row_key,
                    name=parent.name,
                    guidance=guidance,
                    person_ids=parent.person_ids,
                    linkedin_url=candidate.url if candidate else "",
                    submitted_at=now_iso(),
                    match_emails=candidate.match_emails if candidate else (),
                    match_phones=candidate.match_phones if candidate else (),
                )
                try:
                    item = guided_retargets.submit(request)
                except (ValueError, StoreError) as exc:
                    return self.send_bytes(str(exc).encode(), "text/plain", 409)
                try:
                    feedback = build_feedback_request(
                        parent, candidate, action="retarget", comment=guidance, retarget_items=[item]
                    )
                    threading.Thread(target=post_feedback_quietly, args=(feedback,), daemon=True).start()
                except SystemExit:
                    pass
                notify()
                wake_agent()
                return self.send_json(
                    {
                        "ok": True,
                        "item": item.as_dict(),
                        "estimated_cost_usd": ESTIMATED_COST_USD,
                    }
                )
            if parsed.path == "/worth":
                value = (form.get("worth") or [""])[0].strip().lower()
                if value not in {"yes", "no", "restore"}:
                    return self.send_bytes(b"worth must be yes, no, or restore", "text/plain", 400)
                slug = (form.get("parent_slug") or [""])[0].strip()
                parent: ParentViewRow | None = person_detail(db, slug) if slug else None
                if not parent:
                    return self.send_bytes(b"person not found", "text/plain", 404)
                key = parent.worth_row.key
                if not key or (pub and pub != key):
                    return self.send_bytes(b"worth row not found", "text/plain", 404)
                try:
                    api.set_worth(key, value, (form.get("note") or [""])[0].strip()[:2000])
                except StoreError as exc:
                    return self.send_bytes(str(exc).encode(), "text/plain; charset=utf-8", 400)
                row: WorthRow | None = next((item for item in worth_rows(db) if item.key == key), None)
                if row is None:
                    return self.send_bytes(b"written worth row is missing", "text/plain", 409)
                state = api.snapshot(job_running=job_running())
                progress = state.progress
                enrichment = api.enrichment(state)
                manifest = api.manifest(
                    "worth",
                    state=state,
                    enrichment=enrichment,
                )
                notify()
                wake_agent()
                return self.send_json(
                    {
                        "ok": True,
                        "pub": pub,
                        "network_worth": "" if value == "restore" else value,
                        "effective": row.effective,
                        "source": row.source,
                        "reason": row.machine.reason,
                        "rejected": row.effective == "no",
                        "counts": asdict(api.counts()),
                        "progress": asdict(progress),
                        "review_manifest": manifest.as_dict(),
                        "next_stage": "enrich" if progress.worth_pending == 0 else "worth",
                        "state_token": state.state_token,
                    }
                )
            decision = (form.get("decision") or [""])[0]
            new_url = (form.get("new_url") or [""])[0]
            slug = (form.get("parent_slug") or [""])[0]
            note = (form.get("note") or [""])[0].strip()[:2000]
            if not pub or decision not in {"keep", "detach", "fix", "reset", "exclude"}:
                return self.send_bytes(b"bad request", "text/plain", 400)
            try:
                hit = parent_hit(pub, slug)
            except StoreError as exc:
                return self.send_bytes(str(exc).encode(), "text/plain; charset=utf-8", 400)
            if not hit:
                return self.send_bytes(f"review row not found: {pub}".encode(), "text/plain; charset=utf-8", 404)
            row_key, _parent, _candidate = hit
            try:
                result = api.decide(row_key, decision, new_url, note)
            except StoreError as exc:
                return self.send_bytes(str(exc).encode(), "text/plain; charset=utf-8", 400)
            state = api.snapshot(job_running=job_running())
            progress = state.progress
            notify()
            wake_agent()
            return self.send_json(
                {
                    "ok": True,
                    "pub": row_key,
                    "action": result.action,
                    "approved": result.approved,
                    "new_url": result.new_url,
                    "counts": asdict(api.counts()),
                    "progress": asdict(progress),
                    "resolved_pubs": list(result.resolved_pubs),
                    "state_token": state.state_token,
                }
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler
