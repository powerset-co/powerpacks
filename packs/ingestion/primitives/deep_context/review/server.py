"""The local review server's handler: the mounts, and the reads that are not the page's JSON.

Flow: `make_handler` builds one handler class over the canonical SQLite store. Each
request goes to the first mount that claims it:

GET   AppRoutes (the React shell at `/`, `/people`, `/searches`, ... and its assets),
      AccountsApi, TasksApi, ReviewApi (`/api/review/...`), then this module's own:
        /healthz         liveness, for the launcher
        /api/status      the store's stage, next action and state token
        /api/events      SSE: one message per change, with the running enrichment's receipt
        /api/enrichment  the enrichment plan and its state (bin/deep-context reads it)
        /api/retargets   re-research jobs and whether they can run (bin/deep-context reads it)
        /api/dossier     ?slug=: the person's dossier as an HTML fragment; &skip=1 is the
                         body a review card shows (no name, Contact or Network worth)
      then the Searches JSON routes, People's data routes and the legacy search routes.
POST  People, Searches, Accounts, Tasks, then ReviewApi (every write the review page makes).

Opening a page never resumes provider work. Enabled jobs start only from explicit
review requests; queued guided research is available to explicit agent recovery.
Anything unclaimed is a 404.

Changelog:
- 2026-10-02: guided research no longer resumes on handler construction.
- 2026-10-01: the LinkedIn queue is held in memory (linkedin_queue.py): loaded when Enrich
  finishes and when a re-research changes, not derived on every click.
- 2026-10-01: the Jinja review page is gone. `/` is the React shell (AppRoutes); the HTML
  routes (/api/worth-card, /api/linkedin-card, /api/worth-table, /api/worth-details,
  /assets/reconcile-review.*), /api/avatar, /decide and /approve-enrichment are deleted;
  /worth, /complete, /retarget, /feedback and /auth/login moved to review/api.py.
- 2026-09-30: /directory and /api/person are gone (People is the browse surface).
- 2026-09-26: the React app's shell page and assets (AppRoutes) answer first; the
  Searches JSON routes (search_api) answer before the legacy search routes.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable

from packs.ingestion.primitives.accounts.api import AccountsApi
from packs.ingestion.primitives.deep_context.db.models import RESEARCH_CONFIRM_THRESHOLD
from packs.ingestion.primitives.deep_context.db.people_views import dossier_body
from packs.ingestion.primitives.deep_context.db.queries import parents
from packs.ingestion.primitives.deep_context.db.store import Db, StoreError
from packs.ingestion.primitives.deep_context.enrich.enrichment_pipeline import (
    EnrichmentPipeline,
)
from packs.ingestion.primitives.deep_context.review.api import (
    ESTIMATED_COST_USD,
    GuidedRetargets,
    ReviewApi,
)
from packs.ingestion.primitives.deep_context.review.dossier_html import markdown_to_html
from packs.ingestion.primitives.deep_context.review.feedback import feedback_alert
from packs.ingestion.primitives.deep_context.review.guided_retarget import GuidedRetargetWorker
from packs.ingestion.primitives.deep_context.review.linkedin_queue import LinkedinQueue
from packs.ingestion.primitives.deep_context.review.sqlite_adapter import SqliteReviewAdapter
from packs.ingestion.primitives.refresh.api import TasksApi
from packs.ingestion.primitives.share.web.server import share_routes
from packs.search.primitives.deep_search.results_web.api import search_api
from packs.search.primitives.deep_search.results_web.server import DEFAULT_DEEP_SEARCH_ROOT, search_routes
from packs.shared.web.app import AppRoutes


def make_handler(
    *,
    db: Db,
    confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
    agent_notifier: Callable[[], object] | None = None,
    run_jobs: bool = False,
    guided_retargets: GuidedRetargets | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build the handler over an explicit supported Deep Context database."""
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

    # Who is pending a LinkedIn check, kept in memory: known once Enrich finishes, changed
    # after that only by a decision (which takes its parent out) or a re-research.
    linkedin = LinkedinQueue(db)

    def enrichment_finished() -> None:
        linkedin.load()
        wake_agent()

    def retarget_changed() -> None:
        linkedin.load()
        notify()

    enrichment_jobs = EnrichmentPipeline(
        db,
        confirm_threshold,
        on_change=notify,
        on_finish=enrichment_finished,
    )
    adapter = SqliteReviewAdapter(db, confirm_threshold, pipeline=enrichment_jobs)
    if not parents(db, limit=1):
        raise StoreError("Deep Context database is empty; run bin/deep-context ensure-parents")
    # The React app (Review, People, Searches, Accounts, Tasks), its JSON routes and the
    # legacy search routes (assets, tags, feedback) ride this server: one origin, one launcher.
    app = AppRoutes()
    share = share_routes(db)
    searches = search_routes(DEFAULT_DEEP_SEARCH_ROOT, base="/searches")
    searches_json = search_api(searches)
    accounts = AccountsApi()
    tasks = TasksApi()

    if guided_retargets is None and run_jobs:
        guided_retargets = GuidedRetargetWorker(db, on_change=retarget_changed)
    review = ReviewApi(
        db=db,
        adapter=adapter,
        start_enrichment=enrichment_jobs.start,
        notify=notify,
        wake_agent=wake_agent,
        run_jobs=run_jobs,
        guided_retargets=guided_retargets,
        linkedin=linkedin,
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
            if app.get(self, parsed) or accounts.get(self, parsed) or tasks.get(self, parsed):
                return None
            if review.get(self, parsed):
                return None
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
                    # Replay: the first event on connect carries the current
                    # enrichment state, so a refreshed page resumes the bar
                    # exactly where it left off — no waiting for the next
                    # write, no rubberband to zero. Then the stream pushes on
                    # every change; pings keep it alive.
                    job = enrichment_jobs.last_job if enrichment_jobs.running() else None
                    self.wfile.write(
                        f"data: {json.dumps({'seq': 0, 'job': job, 'replay': True})}\n\n".encode()
                    )
                    self.wfile.flush()
                    seen = sequence
                    while True:
                        if sequence == seen:
                            time.sleep(1)
                        current = sequence
                        # The enrichment pipeline's last receipt payload rides
                        # along so the page updates the bar in place; None
                        # between jobs (mutate-only events).
                        job = enrichment_jobs.last_job if sequence != seen else None
                        self.wfile.write(
                            (
                                f"data: {json.dumps({'seq': current, 'job': job})}\n\n"
                                if current != seen
                                else ": ping\n\n"
                            ).encode()
                        )
                        seen = current
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            if parsed.path == "/api/status":
                return self.send_json(adapter.status())
            if parsed.path == "/api/enrichment":
                return self.send_json(adapter.enrichment().as_dict())
            if parsed.path == "/api/retargets":
                return self.send_json(
                    {
                        "items": [item.as_dict() for item in adapter.retargets()],
                        "enabled": guided_retargets is not None,
                        "estimated_cost_usd": ESTIMATED_COST_USD,
                        "feedback_alert": feedback_alert().as_dict(),
                    }
                )
            if parsed.path == "/api/dossier":
                query = dict(urllib.parse.parse_qsl(parsed.query))
                # The review cards pass skip=1: the body starts at Summary.
                body = markdown_to_html(
                    dossier_body(db, query.get("slug", "")),
                    for_review_card=query.get("skip") == "1",
                )
                return self.send_bytes(body.encode())
            if searches_json.get(self, parsed) or share.get(self, parsed) or searches.get(self, parsed):
                return None
            return self.send_bytes(b"not found", "text/plain", 404)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if share.post(self, parsed) or searches.post(self, parsed) or accounts.post(self, parsed) or tasks.post(self, parsed):
                return None
            if review.post(self, parsed):
                return None
            return self.send_bytes(b"not found", "text/plain", 404)

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler
