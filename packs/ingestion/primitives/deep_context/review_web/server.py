"""Frozen Deep Context HTTP transport over canonical SQLite state."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.assemble_synthetic_profile import AssembleSyntheticProfile
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV, ENRICH_MANIFEST, FACTS_DIR, LINKEDIN_OVERRIDES_CSV,
    PROFILE_CACHE_DIR, REVIEW_MANIFEST, ROOT,
)
from packs.ingestion.primitives.deep_context.db import views
from packs.ingestion.primitives.deep_context.db.models import StageStateRow, StageStatus
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.prefetch_profiles import PrefetchProfiles
from packs.ingestion.primitives.deep_context.reconcile_deep_research import ReconcileDeepResearch
from packs.ingestion.primitives.deep_context.review_web import REVIEW_CSS, REVIEW_HTML, REVIEW_JS
from packs.ingestion.primitives.deep_context.review_web.feedback import (
    FEEDBACK_ACTIONS, FEEDBACK_ALERT, build_feedback_request,
    post_feedback_quietly, submit_directory_feedback,
)
from packs.ingestion.primitives.deep_context.review_web.guided_retarget import (
    GuidanceRequest,
    GuidedRetargetWorker,
)
from packs.ingestion.primitives.deep_context.review_web.rendering import (
    DECISION_CHUNK_SIZE, GO_BACK_HTML, _carousel_nav, _phase_view, _step,
    decision_rows_payload, directory_page_html, esc, linkedin_finished_body,
    markdown_to_html, render_decision_table, render_decision_tabs,
    render_enrichment, render_linkedin_card, render_person_detail,
    render_worth_card, worth_pending_entries, worth_review_body,
    worth_search_html,
)
from packs.ingestion.primitives.deep_context.review_web.sqlite_adapter import SqliteReviewAdapter


ENRICH_SCOPE = {"include_candidates": True, "include_plausibly_absent": True}
ESTIMATED_COST_USD = 0.06
TERMINAL_STATES = {"applied", "synthetic", "no_match", "failed"}
AUTH_SCRIPT = Path(__file__).resolve().parents[5] / "packs/powerset/primitives/auth/auth.py"
USER_WORTH_VALUES = {"yes", "no"}
AGENT_ACTIONS = {"retry_enrichment", "realize"}
_auth_proc: dict[str, Any] = {"proc": None}


def _failed_notes(items: list[dict[str, Any]]) -> dict[str, str]:
    latest: dict[str, dict[str, Any]] = {}
    for item in items:
        slug = str(item.get("queue_slug") or item.get("slug") or "").lower()
        if slug and slug not in latest:
            latest[slug] = item
    return {slug: str(item.get("detail") or "the job did not finish") for slug, item in latest.items() if item.get("state") == "failed"}


def _primary_candidate(parent: dict[str, Any]) -> dict[str, Any]:
    candidates = parent.get("candidates") or []
    return next((row for row in candidates if row.get("primary")), candidates[0] if candidates else {})


def _manifest_for_review_path(review_path: Path) -> Path:
    try:
        if review_path.resolve() == LINKEDIN_OVERRIDES_CSV.resolve():
            return REVIEW_MANIFEST
    except OSError:
        pass
    return review_path.parent / "review" / "manifest.json"


def start_auth_login() -> str:
    proc = _auth_proc["proc"]
    if proc is not None and proc.poll() is None:
        return "already_running"
    _auth_proc["proc"] = subprocess.Popen(
        [sys.executable, str(AUTH_SCRIPT), "login"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return "login_started"


def make_handler(
    review_path: Path,
    verdicts_path: Path,
    parents_dir: Path,
    dossier_dir: Path,
    confirm_threshold: float,
    detach_threshold: float,
    synthetic_path: Path = ROOT / "synthetic-people.csv",
    facts_dir: Path = FACTS_DIR,
    people_csv: Path = DEFAULT_PEOPLE_CSV,
    manifest_path: Path | None = None,
    enrichment_manifest_path: Path = ENRICH_MANIFEST,
    profile_cache_dir: Path = PROFILE_CACHE_DIR,
    avatar_dir: Path | None = None,
    initial_parents: list[dict[str, Any]] | None = None,
    agent_notifier: Callable[[], object] | None = None,
    run_jobs: bool | None = None,
    guided_retargets: Any | None = None,
    *,
    db: Db | None = None,
):
    """Build the frozen handler; callers must explicitly provide a bootstrapped v6 DB."""
    del verdicts_path, confirm_threshold, detach_threshold, avatar_dir, initial_parents
    del parents_dir, dossier_dir, facts_dir, people_csv, profile_cache_dir, enrichment_manifest_path
    if db is None:
        raise StoreError("make_handler requires an explicit bootstrapped Deep Context v6 Db")
    manifest_path = manifest_path or _manifest_for_review_path(review_path)
    api = SqliteReviewAdapter(db, Path(review_path), Path(synthetic_path), manifest_path)
    if not api.parents() and api.progress()["total"] == 0:
        raise StoreError(
            "Deep Context database is empty; run bin/deep-context migrate-sqlite"
        )
    if run_jobs is None:
        try:
            run_jobs = Path(review_path).resolve() == LINKEDIN_OVERRIDES_CSV.resolve()
        except OSError:
            run_jobs = False
    retargets_enabled = bool(run_jobs or guided_retargets)
    sequence = {"n": 0}
    job = {"running": False}

    def notify() -> None:
        sequence["n"] += 1

    def wake_agent() -> None:
        if agent_notifier:
            try:
                agent_notifier()
            except Exception:
                pass

    if guided_retargets is None and run_jobs:
        guided_retargets = GuidedRetargetWorker(db, on_change=notify)
        guided_retargets.resume()

    def spawn_job(name: str, steps: Callable[[], None]) -> None:
        if job["running"]:
            return
        job["running"] = True

        def runner() -> None:
            try:
                steps()
            except BaseException as exc:
                db.save_state(
                    StageStateRow(
                        "enrich",
                        StageStatus.FAILED.value,
                        api.selection()["sha256"],
                        error=f"{name}: {type(exc).__name__}: {exc}"[:500],
                        updated_at=now_iso(),
                    )
                )
            finally:
                job["running"] = False
                notify()
                wake_agent()

        threading.Thread(target=runner, name=f"pipeline-job-{name}", daemon=True).start()

    def enrichment_job(budget: float) -> None:
        ReconcileDeepResearch(
            **ENRICH_SCOPE,
            approve=True,
            budget=round(budget, 2),
            on_progress=lambda _: notify(),
            db=db,
        ).run()
        AssembleSyntheticProfile(db=db).run()
        PrefetchProfiles(db=db, fetch=True).run()

    def parent_hit(pub: str, slug: str = "") -> tuple[dict[str, Any], dict[str, Any]] | None:
        parent = api.parent_for_candidate(pub, slug)
        candidate = api.candidate(parent, pub)
        return (parent, candidate) if parent and candidate else None

    def dirs(parent: dict[str, Any], *, hide_raw: bool = False):
        parent_dir, child_dir, copy = api.render_dirs(parent)
        if hide_raw:
            copy["person_ids"] = []
        return parent_dir, child_dir, copy

    def worth_body(params: dict[str, list[str]]) -> str | None:
        queue = views.worth_review(db, "queue")
        pick = str((params.get("pick") or [""])[0]).strip().lower()
        excluded = {v.strip().lower() for v in str((params.get("exclude") or [""])[0]).split(",") if v.strip()}
        if pick:
            queue = [p for p in queue if str(p.get("key") or "").lower() == pick]
            if not queue:
                return None
        queue = [p for p in queue if str(p.get("key") or "").lower() not in excluded]
        queue.sort(key=lambda p: str(p.get("name") or "").lower())
        if not queue:
            return worth_review_body(
                [], api.progress(), Path("/__none__"), Path("/__none__"), auto_continue=not api.phase_completed("worth")
            )
        try:
            index = max(0, int(str((params.get("index") or ["0"])[0]))) % len(queue)
        except ValueError:
            index = 0
        selected = queue[index]
        parent = views.person_detail(db, str(selected.get("parent_id") or ""))
        if parent is None:
            return None
        parent_dir, child_dir, parent = dirs(parent, hide_raw=True)
        card = render_worth_card(parent, parent_dir, child_dir, Path("/__none__"))
        if str((params.get("debug") or [""])[0]) == "1":
            return (
                f"<div class='carousel-shell' data-queue-index='{index}' "
                f"data-queue-total='{len(queue)}'>{_carousel_nav()}{card}</div>"
            )
        return card

    def linkedin_body(params: dict[str, list[str]]) -> str:
        queue = views.linkedin_review(db, "queue")
        excluded = {v.strip().lower() for v in str((params.get("exclude") or [""])[0]).split(",") if v.strip()}
        inflight = {
            str(item.get("queue_slug") or item.get("slug") or "").lower()
            for item in api.retargets()
            if item.get("state") not in TERMINAL_STATES
        }
        queue = [p for p in queue if str(p.get("slug") or "").lower() not in excluded | inflight]
        if not queue:
            return linkedin_finished_body(
                api.progress(),
                linkedin_complete=api.phase_completed("linkedin"),
                retargets_in_flight=len(inflight),
                auto_continue=not api.phase_completed("linkedin"),
            )
        try:
            index = max(0, int(str((params.get("index") or ["0"])[0]))) % len(queue)
        except ValueError:
            index = 0
        parent_dir, child_dir, parent = dirs(queue[index])
        card = render_linkedin_card(
            parent,
            parent.get("candidates") or [],
            parent_dir,
            child_dir,
            Path("/__none__"),
            failure_note=_failed_notes(api.retargets()).get(str(parent.get("slug") or ""), ""),
        )
        return (
            f"<div class='linkedin-stage' data-queue-index='{index}' "
            f"data-queue-total='{len(queue)}'>{_carousel_nav()}{card}</div>"
            if str((params.get("debug") or [""])[0]) == "1"
            else card
        )

    def full_page(params: dict[str, list[str]]) -> bytes:
        parents, progress = api.parents(), api.progress()
        view = _phase_view(params, progress, manifest_path)
        preview = str((params.get("preview") or [""])[0]) == "1"
        enrichment, complete = api.enrichment(), set(api.manifest().get("completed_stages") or [])
        if view == "worth":
            tab = str((params.get("view") or ["review"])[0]).lower()
            tab = tab if tab in {"review", "yes", "no"} else "review"
            tabs = render_decision_tabs(progress, tab, preview=preview)
            if tab == "review":
                body = worth_body(params) or ""
                pending = worth_pending_entries(parents)
                search = worth_search_html("review", pending) if pending else ""
            else:
                body = render_decision_table(parents, tab)
                search = ""
            content = f"<div class='worth-stage'>{tabs}{search}<div class='worth-panel'>{body}</div></div>"
        elif view == "enrich":
            if run_jobs and enrichment.get("state") == "free_pending" and not job["running"]:
                spawn_job("free-enrichment", lambda: enrichment_job(0))
            content = render_enrichment(enrichment, progress, worth_complete="worth" in complete)
        elif view == "linkedin":
            content = f"<div class='linkedin-stage'><div class='linkedin-panel'>{linkedin_body(params)}</div></div>"
        else:
            content = (
                "<div class='empty-state done'><div class='empty-mark'>✓</div><h2>All set</h2>"
                f"<p>{progress['linkedin_done']} identities checked · {progress['rejected']} rejected</p>"
                f"{GO_BACK_HTML}</div>"
            )
        active = {"worth": 0, "enrich": 1, "linkedin": 2, "done": 2}[view]
        specs = (
            (1, "Review Decisions", active == 0, "worth" in complete, progress["worth_pending"], "/?stage=worth&preview=1"),
            (2, "Enrich Contacts", active == 1, enrichment.get("status") == "completed", int((enrichment.get("counts") or {}).get("pending") or 0), "/?stage=enrich&preview=1"),
            (3, "Check LinkedIn", active == 2, "linkedin" in complete, progress["linkedin_pending"], "/?stage=linkedin&preview=1"),
        )
        steps = "<i class='step-line'></i>".join(_step(*spec) for spec in specs)
        workflow = api.workflow_status(job_running=job["running"])
        replacements = {
            "{{TITLE}}": esc(
                {"worth": "Add People", "enrich": "Enrich Contacts", "linkedin": "Check LinkedIn", "done": "All Set"}[
                    view
                ]
            ),
            "{{STAGE}}": view,
            "{{PREVIEW}}": "true" if preview else "false",
            "{{EXTERNAL_UPDATES}}": "true" if view in {"enrich", "done"} else "false",
            "{{STATE_TOKEN}}": workflow["state_token"],
            "{{ENRICHMENT_STATUS}}": str(enrichment.get("status") or ""),
            "{{STEPPER}}": steps,
            "{{CONTENT}}": content,
        }
        document = REVIEW_HTML.read_text(encoding="utf-8")
        for key, value in replacements.items():
            document = document.replace(key, value)
        return document.encode()

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
                        if sequence["n"] == seen:
                            time.sleep(1)
                        current = sequence["n"]
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
                return self.send_json(api.status(job_running=job["running"]))
            if parsed.path == "/api/enrichment":
                return self.send_json(api.enrichment())
            if parsed.path == "/api/retargets":
                return self.send_json(
                    {
                        "items": api.retargets(),
                        "enabled": retargets_enabled,
                        "estimated_cost_usd": ESTIMATED_COST_USD,
                        "feedback_alert": dict(FEEDBACK_ALERT),
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
                body = markdown_to_html(api.dossier_markdown((params.get("slug") or [""])[0]))
                return self.send_bytes(body.encode())
            if parsed.path == "/api/decision-rows":
                view = str((params.get("view") or [""])[0]).lower()
                if view not in {"yes", "no"}:
                    return self.send_json({"error": f"unknown view: {view}"}, 400)
                try:
                    offset = int((params.get("offset") or ["0"])[0])
                    limit = int((params.get("limit") or [str(DECISION_CHUNK_SIZE)])[0])
                except ValueError:
                    offset, limit = 0, DECISION_CHUNK_SIZE
                return self.send_json(decision_rows_payload(api.parents(), view, offset=offset, limit=min(max(1, limit), 200)))
            if parsed.path == "/api/worth-card":
                body = worth_body(params)
                if body is None:
                    return self.send_bytes(b"gone", "text/plain; charset=utf-8", 404)
                return self.send_bytes(body.encode())
            if parsed.path == "/api/linkedin-card":
                return self.send_bytes(linkedin_body(params).encode())
            if parsed.path == "/api/person":
                parent = api.parent(str((params.get("slug") or [""])[0]).lower())
                if not parent:
                    return self.send_bytes(b"not found", "text/plain", 404)
                parent_dir, child_dir, parent = dirs(parent)
                return self.send_bytes(render_person_detail(parent, parent_dir, child_dir, Path("/__none__")).encode())
            if parsed.path == "/directory":
                parent = api.parent(str((params.get("person") or [""])[0]).lower())
                parent_dir, child_dir, _ = dirs(parent) if parent else (Path("/__none__"), Path("/__none__"), {})
                return self.send_bytes(
                    directory_page_html(
                        api.parents(),
                        params,
                        parents_dir=parent_dir,
                        dossier_dir=child_dir,
                        profile_cache_dir=Path("/__none__"),
                        handoff=api.workflow_status()["next_action"] == "realize",
                    )
                )
            if parsed.path == "/api/avatar":
                avatar = api.avatar((params.get("pub") or [""])[0])
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
                except StoreError as exc:
                    return self.send_bytes(str(exc).encode(), "text/plain; charset=utf-8", 409)
                if run_jobs:
                    spawn_job(
                        "approved-enrichment",
                        lambda: enrichment_job(float((enrichment.get("approval") or {}).get("approved_budget_usd") or 0)),
                    )
                wake_agent()
                return self.send_json({"ok": True, "enrichment": enrichment})
            if parsed.path == "/complete":
                stage = (form.get("stage") or [""])[0].strip().lower()
                try:
                    manifest = api.save_stage(stage, True)
                except StoreError as exc:
                    return self.send_bytes(str(exc).encode(), "text/plain; charset=utf-8", 409)
                notify()
                wake_agent()
                return self.send_json({"ok": True, "manifest": manifest, "progress": api.progress()})
            if parsed.path == "/feedback":
                comment = (form.get("comment") or [""])[0].strip()
                action = (form.get("action") or [""])[0].strip()
                if not comment or len(comment) > 4000:
                    return self.send_bytes(b"comment must be 1-4000 characters", "text/plain", 400)
                if action not in FEEDBACK_ACTIONS:
                    return self.send_bytes(b"unknown feedback action", "text/plain", 400)
                slug = (form.get("parent_slug") or [""])[0].strip()
                hit = parent_hit(pub, slug) if pub else None
                parent = hit[0] if hit else api.parent(slug)
                candidate = hit[1] if hit else _primary_candidate(parent or {})
                if not parent:
                    return self.send_bytes(b"person not found", "text/plain", 404)
                payload = submit_directory_feedback(
                    build_feedback_request(
                        parent, candidate, action=action, comment=comment, retarget_items=api.retargets()
                    )
                )
                status = 200 if payload.get("status") == "submitted" else 502
                return self.send_json({"ok": status == 200, **payload}, status)
            if parsed.path == "/retarget":
                guidance = (form.get("guidance") or [""])[0].strip()
                slug = (form.get("parent_slug") or [""])[0].strip()
                if not guidance or len(guidance) > 2000:
                    return self.send_bytes(b"guidance must be 1-2000 characters", "text/plain", 400)
                if not retargets_enabled:
                    return self.send_bytes(b"in-app jobs are disabled on this server", "text/plain", 503)
                hit = parent_hit(pub, slug) if pub else None
                parent = hit[0] if hit else api.parent(slug)
                candidate = hit[1] if hit else {}
                if not parent:
                    return self.send_bytes(b"person not found", "text/plain", 404)
                key = str(candidate.get("row_key") or pub or (parent.get("person_ids") or [""])[0]).strip()
                if not key:
                    return self.send_bytes(b"person has no review key", "text/plain", 400)
                request = GuidanceRequest(
                    slug=str(parent.get("dossier_slug") or parent.get("slug") or slug),
                    pub=key,
                    name=str(parent.get("name") or ""),
                    guidance=guidance,
                    person_ids=tuple(str(v) for v in parent.get("person_ids") or []),
                    linkedin_url=str(candidate.get("url") or ""),
                    candidate_pubs=tuple(
                        sorted(
                            str(c.get("row_key") or "")
                            for c in parent.get("candidates") or []
                            if not c.get("synthetic")
                        )
                    ),
                    synthetic_pubs=tuple(
                        sorted(
                            str(c.get("row_key") or "") for c in parent.get("candidates") or [] if c.get("synthetic")
                        )
                    ),
                    queue_slug=str(parent.get("slug") or slug),
                    submitted_at=now_iso(),
                    match_emails=tuple(str(v) for v in candidate.get("match_emails") or []),
                    match_phones=tuple(str(v) for v in candidate.get("match_phones") or []),
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
                return self.send_json({"ok": True, "item": item, "estimated_cost_usd": ESTIMATED_COST_USD})
            if parsed.path == "/worth":
                value = (form.get("worth") or [""])[0].strip().lower()
                if value not in {*USER_WORTH_VALUES, "restore"}:
                    return self.send_bytes(b"worth must be yes, no, or restore", "text/plain", 400)
                slug = (form.get("parent_slug") or [""])[0].strip()
                parent = api.parent(slug) if slug else None
                key = str((parent.get("worth_row") or {}).get("key") or pub) if parent else pub
                try:
                    api.set_worth(key, value, (form.get("note") or [""])[0].strip()[:2000])
                except StoreError as exc:
                    return self.send_bytes(str(exc).encode(), "text/plain; charset=utf-8", 400)
                row = next((r for r in views.worth_review(db, "rows") if r["key"] == key), None) or {}
                progress = api.progress()
                manifest = api.save_stage("worth", progress["worth_pending"] == 0)
                notify()
                wake_agent()
                return self.send_json(
                    {
                        "ok": True,
                        "pub": pub,
                        "network_worth": "" if value == "restore" else value,
                        "action": "",
                        "approved": "",
                        "new_url": "",
                        "effective": row.get("effective") or "maybe",
                        "source": row.get("source") or "llm",
                        "reason": (row.get("machine") or {}).get("reason") or "",
                        "rejected": row.get("effective") == "no",
                        "counts": api.counts(),
                        "progress": progress,
                        "review_manifest": manifest,
                        "next_stage": "enrich" if progress["worth_pending"] == 0 else "worth",
                        "state_token": api.workflow_status()["state_token"],
                    }
                )
            decision = (form.get("decision") or [""])[0]
            new_url = (form.get("new_url") or [""])[0]
            slug = (form.get("parent_slug") or [""])[0]
            if not pub or decision not in {"keep", "detach", "fix", "reset", "exclude"}:
                return self.send_bytes(b"bad request", "text/plain", 400)
            hit = parent_hit(pub, slug)
            if not hit:
                return self.send_bytes(f"review row not found: {pub}".encode(), "text/plain; charset=utf-8", 400)
            if slug and str(hit[0].get("slug") or "") != slug:
                return self.send_bytes(b"stale or mismatched person card", "text/plain; charset=utf-8", 400)
            try:
                result, resolved = api.decide(str(hit[1].get("row_key") or pub), decision, new_url)
            except StoreError as exc:
                return self.send_bytes(str(exc).encode(), "text/plain; charset=utf-8", 400)
            progress = api.progress()
            notify()
            wake_agent()
            return self.send_json(
                {
                    "ok": True,
                    "pub": pub,
                    **result,
                    "counts": api.counts(),
                    "progress": progress,
                    "resolved_pubs": resolved,
                    "state_token": api.workflow_status()["state_token"],
                }
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler


def cmd_serve(args: argparse.Namespace) -> None:
    review_path, manifest_path = Path(args.review), Path(args.manifest)
    db_path = ROOT / "deep-context.sqlite"
    if not db_path.exists():
        raise SystemExit(
            f"Deep Context database is missing: {db_path}; "
            "run bin/deep-context migrate-sqlite"
        )
    db = Db(db_path)
    try:
        with urllib.request.urlopen(f"http://{args.host}:{args.port}/api/status", timeout=1) as response:
            live = json.loads(response.read())
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        live = {}
    stage = args.stage or "directory"
    if live.get("primitive") == "reconcile_review_web":
        url = f"http://{args.host}:{args.port}/" + ("directory" if stage == "directory" else f"?stage={stage}")
        print(json.dumps({"primitive": "reconcile_review_web", "status": "reused", "url": url, "manifest": str(manifest_path), "stage": stage}, indent=2))
        if args.open:
            webbrowser.open(url)
        return
    if args.fresh and stage == "worth":
        db.save_state(StageStateRow("worth", StageStatus.PENDING.value, updated_at=now_iso()))
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(
            review_path,
            Path(args.verdicts),
            Path(args.parents_dir),
            Path(args.dossier_dir),
            args.confirm_threshold,
            args.detach_threshold,
            manifest_path=manifest_path,
            db=db,
        ),
    )
    host, port = server.server_address
    url = f"http://{host}:{port}/" + ("directory" if stage == "directory" else f"?stage={stage}")
    state = views.workflow_state(db)
    print(json.dumps({"primitive": "reconcile_review_web", "status": "serving", "url": url, "manifest": str(manifest_path), "parents": len(views.linkedin_review(db, "parents")), "progress": state["progress"]}, indent=2))
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)


def workflow_status(*, manifest_path: Path = REVIEW_MANIFEST, **_: Any) -> dict[str, Any]:
    db_path = Path(manifest_path).parent.parent / "deep-context.sqlite"
    if not db_path.exists():
        raise StoreError(
            f"Deep Context database is missing: {db_path}; "
            "run bin/deep-context migrate-sqlite"
        )
    api = SqliteReviewAdapter(Db(db_path), LINKEDIN_OVERRIDES_CSV, ROOT / "synthetic-people.csv", Path(manifest_path))
    payload = api.workflow_status()
    commands = dict((
        ("review_people", "bin/deep-context review"), ("preview_enrichment", "bin/deep-context reconcile-deep-research --dry-run --include-candidates --include-plausibly-absent"), ("await_enrichment_approval", "wait for the user to click Approve in Enrich Contacts"), ("run_approved_enrichment", "bin/deep-context reconcile-deep-research --include-candidates --include-plausibly-absent --approve"), ("run_enrichment_from_cache", "bin/deep-context reconcile-deep-research --include-candidates --include-plausibly-absent"), ("wait_for_enrichment", "bin/deep-context review-status"), ("retry_enrichment", "inspect the enrichment projection error"), ("assemble_synthetic", "bin/deep-context assemble-synthetic"), ("continue_enrichment", "wait for the user to click Continue in Enrich Contacts"), ("review_linkedin", "wait for LinkedIn Yes/No decisions in the review UI"), ("finish_linkedin", "wait for the user to click Finish in Check LinkedIn"), ("realize", "bin/deep-context stop && bin/deep-context realize"),
    ))
    payload.update({"command": commands[payload["next_action"]], "poll_after_seconds": 60})
    return payload
