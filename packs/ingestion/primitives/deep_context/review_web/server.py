"""HTTP routing, asset serving, and in-app workflow jobs for review.

SINGLE-WRITER SESSION CONTRACT (2026-07-29): while this server runs it is the
only writer of the review-session files — enforced by the advisory flock in
`common.acquire_review_session_lock` (mutating CLI mains refuse while it is
held). Memory is therefore authoritative for the whole session: the model is
built at boot and rebuilt only at this server's own refresh points (job
terminals, stage boundaries). Files are write-through flushes for crash safety
and the baton pass to the next process. Views subscribe to `/api/events` (SSE)
and re-snapshot `/api/status` on each nudge — the browser never polls.

Changelog:
  2026-08-05 (sqlite P1+P2): review.sqlite (next to review.csv) is the
    server's store. Reads: cmd_serve creates/imports it (strict — an
    unrepresentable row refuses serve by name); model builds and
    review_rows_now compose rows from the db, with needs_import (keyed on the
    CSV stat) as the other-writer check. Writes: every session mutation
    (apply_decision, apply_worth_decision, the guided-retarget worker) routes
    through commit_rows — the db transaction is the commit, the CSVs are
    exports of it; a crash between commit and export recovers at next boot
    (recover_pending_export). Without a db wired (tests), both doors fall
    back to the plain CSV loader/writer unchanged.
  2026-07-30: Bare review launches always land on the read-only directory;
    staged workflow launches opt in with an explicit --stage.
  2026-07-29 (single-writer rewrite): deleted the stat/signature invalidation
    apparatus (`input_signature`, `accept_local_write`, `accept_rows_write`,
    per-request stat checks) — sediment from defending an undesigned
    concurrency mode that once caused the stale-enrich-phase bug and later a
    9-minute GIL pile-up on an 8 GB machine (1 Hz polls x full-model rebuilds).
    `parents_now()` returns memory; `refresh_parents_from_disk()` runs at job
    terminals (via the `_job_events` channel) and stage boundaries. Added `/api/events`
    (SSE nudge stream) and the session flock in `cmd_serve` (canonical store
    only). The review JS replaced `setInterval` polling with one EventSource.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.parse
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.deep_context.enrichment_contract import (
    STATE_DONE,
    STATE_FREE_PENDING,
    STATE_NEEDS_APPROVAL,
    STATUS_COMPLETED,
    STATUS_RESEARCH_COMPLETE,
    derive_enrichment_state,
    read_enrichment_manifest,
)
from packs.ingestion.primitives.common.legacy import ensure_owner_phones, resolve_stored_identity_policy
from packs.ingestion.primitives.deep_context.common import (
    INDEX_JSON,
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    ENRICH_MANIFEST,
    FACTS_DIR,
    LINKEDIN_OVERRIDES_CSV,
    OWNER_JSON,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    RAW_DIR,
    REVIEW_MANIFEST,
    VERDICTS_JSONL,
    acquire_review_session_lock,
    load_env,
    now_iso,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    _write_override_rows,
    load_override_rows,
)

from packs.ingestion.primitives.deep_context.assemble_synthetic_profile import AssembleSyntheticProfile
from packs.ingestion.primitives.deep_context.review_db import ReviewDb
from packs.ingestion.primitives.deep_context.prefetch_profiles import PrefetchProfiles
from packs.ingestion.primitives.enrich.rapidapi_client import rapidapi_profile
from packs.ingestion.schemas.people_schema import extract_public_identifier
from packs.ingestion.primitives.deep_context.reconcile_deep_research import ReconcileDeepResearch
from .decisions import apply_decision, apply_synthetic_decision, apply_worth_decision, carry_forward_multi_option_contacts, sync_synthetic_gate
from .feedback import (
    FEEDBACK_ACTIONS,
    FEEDBACK_ALERT,
    build_feedback_request,
    post_feedback_quietly,
    submit_directory_feedback,
)
from .retarget_queue import ESTIMATED_COST_USD, GuidedRetarget, RetargetQueue, TERMINAL_STATES, failed_notes_from_items, linkedin_url_in_guidance, run_guided_retarget
from .model import SYNTHETIC_PEOPLE_CSV, USER_WORTH_VALUES, _all_review_parents, _primary_candidate, _worth_key, candidate_state, effective_no_for_key, load_avatar, load_connection_keys, summarize, synthetic_worth_key
from .rendering import DECISION_CHUNK_SIZE, REVIEW_CSS, REVIEW_JS, _phase_view, _primary_candidate, decision_rows_payload, directory_page_html, linkedin_card_body, linkedin_review_body, linkedin_review_queue, page_html, render_dossier_markdown, render_person_detail, render_worth_card, worth_review_body
from .workflow import approve_enrichment_manifest, browser_stage_for_next_action, current_worth_selection, enrichment_handoff_completed, needs_worth_review, phase_is_completed, read_review_manifest, review_progress, review_state_token, worth_selection_from_parents, write_enrichment_handoff, write_review_manifest

def _manifest_for_review_path(review_path: Path) -> Path:
    try:
        if review_path.resolve() == LINKEDIN_OVERRIDES_CSV.resolve():
            return REVIEW_MANIFEST
    except (OSError, RuntimeError):
        pass
    return review_path.parent / "review" / "manifest.json"


# The enrichment scope this app always runs with. Constructor kwargs, not CLI
# flags: the in-app jobs build the same `pipeline/contract.py` nodes every other
# caller does (construct-and-run) instead of fabricating an argv and re-parsing
# it. Same defaults — the node constructors and their parsers agree — so the
# work and the manifests are unchanged; only the invocation shape is.
ENRICH_SCOPE = {"include_candidates": True, "include_plausibly_absent": True}


_job_lock = threading.Lock()

# Browser re-login, offered by the UI when a feedback post returns needs_auth.
# auth.py runs the whole authorization-code flow itself (local callback server,
# auto-opens the browser, writes credentials.json); one flow at a time.
AUTH_SCRIPT = Path(__file__).resolve().parents[5] / "packs/powerset/primitives/auth/auth.py"
_auth_login_lock = threading.Lock()
_auth_login: dict[str, Any] = {"proc": None}


def start_auth_login() -> str:
    """Spawn `auth.py login` unless one is already mid-flight; returns status."""
    with _auth_login_lock:
        proc = _auth_login["proc"]
        if proc is not None and proc.poll() is None:
            return "already_running"
        _auth_login["proc"] = subprocess.Popen(
            [sys.executable, str(AUTH_SCRIPT), "login"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "login_started"


def _mark_enrichment_failed(error: str) -> None:
    """Best-effort: surface a job crash in the fixed enrichment manifest so
    workflow_status turns it into retry_enrichment instead of a silent stall."""
    try:
        existing = json.loads(ENRICH_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    if existing.get("status") == "failed" and existing.get("error") == error[:500]:
        return  # already surfaced; a repeat write would churn the UI poll per retry
    existing.update({"stage": "enrich", "status": "failed", "error": error[:500],
                     "updated_at": now_iso()})
    try:
        ENRICH_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        ENRICH_MANIFEST.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except OSError:
        pass


class _JobEvents:
    """The one in-process channel between jobs and views (single-writer:
    jobs are the only mid-session writer besides clicks). Primitives report
    progress here beside their manifest flushes — the server never reads its
    own flush back; job terminals (success OR failure) fire the terminal
    subscribers so the handler closure re-derives the model and nudges views."""

    def __init__(self) -> None:
        self.latest: dict[str, Any] = {}
        self._progress_subs: list[Callable[[], None]] = []
        self._terminal_subs: list[Callable[[], None]] = []

    def subscribe(self, *, progress: Callable[[], None] | None = None,
                  terminal: Callable[[], None] | None = None) -> None:
        if progress:
            self._progress_subs.append(progress)
        if terminal:
            self._terminal_subs.append(terminal)

    def report(self, event: dict[str, Any]) -> None:
        self.latest = dict(event)
        self._fire(self._progress_subs)

    def terminal(self) -> None:
        self.latest = {}
        self._fire(self._terminal_subs)

    @staticmethod
    def _fire(subs: list[Callable[[], None]]) -> None:
        for sub in list(subs):
            try:
                sub()
            except Exception:
                pass  # a view nudge must never break the job


_job_events = _JobEvents()


def _run_pipeline_job(name: str, steps: Callable[[], None]) -> None:
    if not _job_lock.acquire(blocking=False):
        return  # one job at a time; the durable manifests re-trigger any rerun

    def runner() -> None:
        try:
            steps()
        # BaseException on purpose: the primitives raise SystemExit on their
        # guard paths, which `except Exception` misses — the thread then died
        # silently and the manifest stranded mid-state with no failure marker.
        except BaseException as exc:  # the manifest is the UI's/agent's error surface
            _mark_enrichment_failed(f"{name}: {type(exc).__name__}: {exc}")
        finally:
            _job_lock.release()
            _job_events.terminal()

    threading.Thread(target=runner, name=f"pipeline-job-{name}", daemon=True).start()


def _post_enrichment_chain() -> None:
    """Free follow-ups once research is done: no-LinkedIn cards + profile cache."""
    AssembleSyntheticProfile().run()
    # `fetch=True` IS the spend: the profile cache misses are hydrated here, on
    # the same authorization that started this chain (research completed).
    PrefetchProfiles(fetch=True).run()


def _free_enrichment_steps() -> None:
    """The ONE free-work pass: run the enrichment continue with a $0 ceiling.
    Zero net-new does ALL the free work (reuse + fingerprint-cached retarget
    judging) and the follow-up chain; any real spend hits the primitive's budget
    gate, which stamps a current needs_approval receipt WITHOUT spending a cent
    (the Approve button owns money). No convergence loop: the chain may re-drift
    the selection, and the next enrich-page render re-derives and re-triggers."""
    ReconcileDeepResearch(**ENRICH_SCOPE, approve=True, budget=0.0,
                          on_progress=_job_events.report).run()
    enrichment = read_enrichment_manifest(selection=current_worth_selection())
    if enrichment.get("status") == STATUS_RESEARCH_COMPLETE:
        _post_enrichment_chain()


def start_free_enrichment_job() -> None:
    """Start-or-join THE single free-work job (one module-level mutex; idempotent).
    Rendering the enrich page is the only trigger — a stranded manifest state
    cannot survive a reload because every render re-derives and re-kicks this."""
    _run_pipeline_job("free-enrichment", _free_enrichment_steps)


def start_approved_enrichment_job(budget: float) -> None:
    """The Approve $X click IS the user's spend approval: run exactly that."""
    def steps() -> None:
        # The budget is rounded to cents exactly as the argv form did, so the
        # primitive's gate compares the same ceiling the UI approved.
        ReconcileDeepResearch(**ENRICH_SCOPE, approve=True, budget=round(budget, 2),
                              on_progress=_job_events.report).run()
        _post_enrichment_chain()

    _run_pipeline_job("approved-enrichment", steps)


def make_handler(review_path: Path, verdicts_path: Path, parents_dir: Path, dossier_dir: Path,
                 confirm_threshold: float, detach_threshold: float,
                 synthetic_path: Path = SYNTHETIC_PEOPLE_CSV,
                 facts_dir: Path = FACTS_DIR, people_csv: Path = DEFAULT_PEOPLE_CSV,
                 manifest_path: Path | None = None,
                 enrichment_manifest_path: Path = ENRICH_MANIFEST,
                 profile_cache_dir: Path = PROFILE_CACHE_DIR,
                 avatar_dir: Path | None = None,
                 initial_parents: list[dict[str, Any]] | None = None,
                 agent_notifier: Callable[[], object] | None = None,
                 run_jobs: bool | None = None,
                 guided_retargets: RetargetQueue | None = None,
                 review_db: ReviewDb | None = None):
    manifest_path = manifest_path or _manifest_for_review_path(review_path)
    # In-app jobs call the primitives on their CANONICAL default paths, so they
    # only auto-enable for the canonical server (tests use temp paths -> off).
    if run_jobs is None:
        try:
            run_jobs = review_path.resolve() == LINKEDIN_OVERRIDES_CSV.resolve()
        except OSError:
            run_jobs = False
    avatar_dir = avatar_dir or manifest_path.parent / "avatars"
    mutation_lock = threading.Lock()

    def db_review_rows() -> dict[str, dict[str, str]] | None:
        """Phase-1 sqlite read door: keep review.sqlite current with the CSV
        and compose a fresh owned row snapshot from it. None when no db is
        wired (tests keep the plain CSV path)."""
        if review_db is None:
            return None
        if review_db.needs_import(review_path):
            review_db.import_stores(review_path, synthetic_path)
        return review_db.export_review_rows()

    def commit_rows(path: Path, rows: dict[str, dict[str, str]]) -> None:
        """Phase-2 write door: the db transaction is the commit, the CSV an
        export of it (crash between the two recovers at next boot). Without a
        db this IS the plain CSV writer, so decision flows stay one shape."""
        if review_db is None:
            _write_override_rows(path, rows)
            return
        review_db.apply_rows(rows, path, synthetic_csv=synthetic_path)

    def notify_agent() -> None:
        """Best-effort wake after durable UI mutations; file state stays authoritative."""
        if agent_notifier is None:
            return
        try:
            agent_notifier()
        except Exception:
            # Review decisions must never fail because an optional observer
            # hook (tests use it to count mutations) raised.
            pass

    # SINGLE-WRITER SESSION CONTRACT: while this server runs it is the ONLY
    # writer of the review-session files (the advisory session lock refuses
    # concurrent mutating CLI runs). Memory is therefore authoritative for the
    # whole session: no stat checks, no invalidation signatures — the model is
    # built at boot and rebuilt only after this server's OWN jobs write, at
    # explicit refresh points. Files are write-through flushes for crash
    # safety and the baton pass to the next process, never re-read mid-session.
    cached_parents = (
        initial_parents if initial_parents is not None else
        _all_review_parents(
            verdicts_path, review_path, synthetic_path, facts_dir, people_csv,
            parents_dir, dossier_dir, profile_cache_dir,
            rows=db_review_rows())
    )
    connection_keys = load_connection_keys(people_csv)

    # View change stream: mutation choke points and job completions bump the
    # sequence; /api/events holds a connection per tab and nudges it to
    # re-snapshot /api/status. The browser never polls.
    events_cond = threading.Condition()
    events_seq = {"n": 0}

    def notify_views() -> None:
        with events_cond:
            events_seq["n"] += 1
            events_cond.notify_all()

    def _after_job() -> None:
        refresh_parents_from_disk()
        notify_views()

    _job_events.subscribe(progress=notify_views, terminal=_after_job)

    def parents_now() -> list[dict[str, Any]]:
        """The in-memory SPA model — authoritative for the session."""
        return cached_parents

    def refresh_parents_from_disk() -> list[dict[str, Any]]:
        """Rebuild the model from files — the explicit refresh points: after
        this server's own in-process jobs write (their primitives author
        complex slices it is safer to re-derive than to patch), and at
        stage-completion boundaries so "completed" is only written when a
        fresh derivation agrees with the agent's own read."""
        nonlocal cached_parents, connection_keys, cached_rows
        cached_parents = _all_review_parents(
            verdicts_path, review_path, synthetic_path, facts_dir, people_csv,
            parents_dir, dossier_dir, profile_cache_dir,
            rows=db_review_rows())
        connection_keys = load_connection_keys(people_csv)
        cached_rows = None
        return cached_parents

    def _guided_runner(request: GuidedRetarget,
                       report: Callable[[str, str], None]) -> dict[str, Any]:
        """One guided retarget, serialized with the pipeline jobs (both write
        review.csv slices), then the same cache-first profile hydration the
        enrichment chain runs — the submit click covered this spend."""
        with _job_lock:
            result = run_guided_retarget(
                request, review_path=review_path, people_csv=people_csv,
                facts_dir=facts_dir, raw_dir=RAW_DIR,
                synthetic_path=synthetic_path,
                use_llm=True, on_progress=report, write=commit_rows)
        if result.get("state") == "applied" and result.get("new_url"):
            # An APPLIED retarget is no longer a pending candidate, so the
            # prefetch stage would skip it — fetch the new profile directly
            # (cache-first; same call apply_retargets makes at realize).
            report("hydrating", "fetching the confirmed profile")
            try:
                new_url = str(result["new_url"])
                new_pub = extract_public_identifier(new_url).lower()
                if new_pub:
                    rapidapi_profile(new_pub, new_url,
                                     cache_dir=profile_cache_dir)
            except BaseException as exc:
                result = {**result,
                          "detail": f"applied; profile fetch failed: {exc}"}
        refresh_parents_from_disk()
        notify_agent()
        return result

    guided_queue = guided_retargets
    if guided_queue is None and run_jobs:
        guided_queue = RetargetQueue(runner=_guided_runner, on_change=notify_views)

    def guided_inflight_slugs() -> frozenset[str]:
        """Parents with an ACTIVE guided re-research. Linear review: a person
        leaves the queue the moment their re-research is queued and the result
        applies in the background; only a FAILED job returns them to review."""
        if guided_queue is None:
            return frozenset()
        return frozenset(
            str(item.get("queue_slug") or item.get("slug") or "").strip().lower()
            for item in guided_queue.snapshot()
            if item.get("state") not in TERMINAL_STATES
            and (item.get("queue_slug") or item.get("slug")))

    def guided_failed_notes() -> dict[str, str]:
        """slug -> failure detail for parents whose LATEST guided re-research
        FAILED. A failed job returns the person to the queue; their card must
        say so, or the return reads as an unexplained loop."""
        if guided_queue is None:
            return {}
        return failed_notes_from_items(guided_queue.snapshot())

    # One free-enrichment attempt per worth-selection per process: a job that
    # FAILS must not restart on every page load (each restart rotates the state
    # token, which reloads the page, which restarts the job — an infinite
    # bounce). A new decision (new selection sha) or a server restart retries.
    _free_attempted: set[str] = set()

    # Parsed review.csv rows; our own decision writes mutate the dict in
    # place, and refresh_parents_from_disk drops it after a job write. The
    # mtime check catches every OTHER writer (the guided-retarget worker, a
    # CLI run against the same store): handing a handler a stale snapshot
    # here would make its whole-file rewrite erase those writers' rows.
    cached_rows: dict[str, dict[str, str]] | None = None
    cached_rows_mtime: int = -1

    def review_rows_now() -> dict[str, dict[str, str]]:
        nonlocal cached_rows, cached_rows_mtime
        if review_db is not None:
            # sqlite path: needs_import IS the other-writer check (keyed on the
            # CSV's stat, same contract as the mtime cache below). Handlers
            # keep mutating the returned dict in place and writing through.
            if cached_rows is None or review_db.needs_import(review_path):
                cached_rows = db_review_rows()
            return cached_rows
        try:
            mtime = review_path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        if cached_rows is None or mtime != cached_rows_mtime:
            cached_rows = load_override_rows(review_path)
            cached_rows_mtime = mtime
        return cached_rows

    def candidate_in_snapshot(pub: str, prefer_slug: str = "",
                              ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Resolve a candidate pub to (parent, candidate). The same pub can be
        owned by SEVERAL parents (one confirmed LinkedIn attached to two split
        parents), so when the client says which card it decided (prefer_slug),
        honor that parent — resolving globally would hit the other owner first
        and 409 every click as 'stale or mismatched person card'."""
        pub_lower = pub.strip().lower()
        hits: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for parent in cached_parents:
            for candidate in parent.get("candidates") or []:
                if str(candidate.get("pub") or "").strip().lower() == pub_lower:
                    hits.append((parent, candidate))
        if prefer_slug:
            for parent, candidate in hits:
                if str(parent.get("slug") or "") == prefer_slug:
                    return parent, candidate
        return hits[0] if hits else None

    def worth_parent_in_snapshot(key: str, parent_slug: str = "") -> dict[str, Any] | None:
        """The cached canonical parent this decision was rendered from."""
        slug_lower = parent_slug.strip().lower()
        if slug_lower:
            for parent in cached_parents:
                candidate_slug = str(parent.get("dossier_slug")
                                     or parent.get("slug") or "").strip().lower()
                if candidate_slug == slug_lower:
                    return parent
        key_lower = key.strip().lower()
        return next(
            (parent for parent in cached_parents
             if str(_worth_key(parent) or "").strip().lower() == key_lower),
            None,
        )

    def state_token_for(parents: list[dict[str, Any]], progress: dict[str, int]) -> str:
        selection = worth_selection_from_parents(parents, manifest_path=manifest_path)
        enrichment = read_enrichment_manifest(
            enrichment_manifest_path, selection=selection)
        return review_state_token(
            progress, selection, enrichment, read_review_manifest(manifest_path),
            job_running=_job_lock.locked())

    def invalidate_manifest(stage: str, progress: dict[str, int], *, launched: bool = False) -> None:
        write_review_manifest(stage, "awaiting_user", progress, path=manifest_path,
                              review_path=review_path, synthetic_path=synthetic_path,
                              launched=launched)

    class Handler(BaseHTTPRequestHandler):
        def send_bytes(self, body: bytes, content_type: str = "text/html; charset=utf-8",
                       status: int = 200, *, cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            self.send_bytes(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8",
                            status=status)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/healthz":
                self.send_bytes(b"ok", "text/plain")
                return
            if parsed.path == "/api/events":
                # SSE nudge stream: each event tells the view "re-snapshot
                # /api/status". Data is the sequence number only; the snapshot
                # endpoint stays the one answer shape. Keepalive comment every
                # 15s; EventSource reconnects (and re-snapshots) on its own.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                last_seen = -1
                try:
                    self.wfile.write(b"retry: 2000\n\n")
                    while True:
                        # Progress events are pushed by the primitives' own
                        # on_progress callbacks — no timers, no file reads.
                        with events_cond:
                            if events_seq["n"] == last_seen:
                                events_cond.wait(timeout=15.0)
                            current = events_seq["n"]
                        if current != last_seen:
                            last_seen = current
                            payload = json.dumps({"seq": current,
                                                  "job": _job_events.latest or None})
                            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                        else:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            if parsed.path == "/api/status":
                with mutation_lock:
                    status = workflow_status_from_parents(
                        parents_now(), manifest_path=manifest_path,
                        enrichment_manifest_path=enrichment_manifest_path)
                self.send_json({
                    "primitive": "reconcile_review_web",
                    "ok": True,
                    "manifest": str(manifest_path),
                    "stage": browser_stage_for_next_action(status["next_action"]),
                    "next_action": status["next_action"],
                    "state_token": review_state_token(
                        status["progress"], status["selection"],
                        status["enrichment"], status["review_manifest"],
                        job_running=_job_lock.locked()),
                })
                return
            if parsed.path == "/api/enrichment":
                with mutation_lock:
                    parents = parents_now()
                selection = worth_selection_from_parents(
                    parents, manifest_path=manifest_path)
                self.send_json(read_enrichment_manifest(
                    enrichment_manifest_path, selection=selection))
                return
            if parsed.path == "/api/retargets":
                self.send_json({
                    "items": guided_queue.snapshot() if guided_queue else [],
                    "enabled": guided_queue is not None,
                    "estimated_cost_usd": ESTIMATED_COST_USD,
                    "feedback_alert": dict(FEEDBACK_ALERT),
                })
                return
            if parsed.path == "/assets/reconcile-review.css":
                if not REVIEW_CSS.exists():
                    self.send_bytes(b"not found", "text/plain", status=404)
                else:
                    self.send_bytes(REVIEW_CSS.read_bytes(), "text/css; charset=utf-8",
                                    cache="no-cache")
                return
            if parsed.path == "/assets/reconcile-review.js":
                if not REVIEW_JS.exists():
                    self.send_bytes(b"not found", "text/plain", status=404)
                else:
                    self.send_bytes(REVIEW_JS.read_bytes(), "text/javascript; charset=utf-8",
                                    cache="no-cache")
                return
            if parsed.path == "/api/dossier":
                slug = (params.get("slug") or [""])[0]
                body = render_dossier_markdown(parents_dir, dossier_dir, slug)
                self.send_bytes(body.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/decision-rows":
                view = str((params.get("view") or [""])[0]).strip().lower()
                if view not in {"yes", "no"}:
                    self.send_json({"error": f"unknown view: {view}"}, status=400)
                    return
                try:
                    offset = int(str((params.get("offset") or ["0"])[0]))
                    limit = int(str((params.get("limit") or [str(DECISION_CHUNK_SIZE)])[0]))
                except ValueError:
                    offset, limit = 0, DECISION_CHUNK_SIZE
                with mutation_lock:
                    parents = parents_now()
                self.send_json(decision_rows_payload(
                    parents, view, offset=offset, limit=min(max(1, limit), 200),
                    parents_dir=parents_dir, dossier_dir=dossier_dir))
                return
            if parsed.path in {"/api/worth-card", "/api/linkedin-card"}:
                # The next queue card (or its stage-complete state), so a decision
                # click swaps content in place instead of reloading. Optional
                # debug/index params drive the browse-only carousel; defaults are
                # exactly the pre-carousel behavior.
                debug = str((params.get("debug") or [""])[0]).strip() == "1"
                try:
                    index = max(0, int(str((params.get("index") or ["0"])[0])))
                except ValueError:
                    index = 0
                exclude = frozenset(
                    key.strip().lower()
                    for key in str((params.get("exclude") or [""])[0]).split(",")
                    if key.strip())
                pick = str((params.get("pick") or [""])[0]).strip().lower()
                if parsed.path == "/api/worth-card" and pick:
                    # Typeahead jump: ONE specific pending person's card, served
                    # from the same lock-free snapshot as the exclude prefetch
                    # (never takes the mutation lock, never rebuilds the model).
                    # A key that is no longer pending — decided elsewhere or
                    # stale — answers 404 so the client prunes it locally and
                    # keeps the current card.
                    picked = next(
                        (parent for parent in cached_parents
                         if needs_worth_review(parent)
                         and str(_worth_key(parent) or "").strip().lower() == pick),
                        None)
                    if picked is None:
                        self.send_bytes(b"gone", "text/plain; charset=utf-8", status=404)
                        return
                    card = render_worth_card(picked, parents_dir, dossier_dir,
                                             profile_cache_dir)
                    self.send_bytes(card.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if exclude:
                    # Prefetch of the FOLLOWING card while a decision POST holds
                    # the mutation lock: render from the current snapshot without
                    # blocking. The excluded keys make the pick race-free, and the
                    # POST's own response re-syncs counts when it lands.
                    parents = cached_parents
                else:
                    with mutation_lock:
                        parents = parents_now()
                progress = review_progress(parents)
                if parsed.path == "/api/worth-card":
                    body = worth_review_body(parents, progress, parents_dir, dossier_dir,
                                             debug=debug, index=index,
                                             profile_cache_dir=profile_cache_dir,
                                             exclude=exclude or None,
                                             auto_continue=not phase_is_completed(
                                                 "worth", progress, manifest_path))
                elif debug:
                    selection = worth_selection_from_parents(
                        parents, manifest_path=manifest_path)
                    enrichment = read_enrichment_manifest(
                        enrichment_manifest_path, selection=selection)
                    body = linkedin_review_body(
                        parents, progress,
                        enrichment_complete=bool(enrichment.get("status") == STATUS_COMPLETED
                                                 and enrichment.get("current")),
                        linkedin_complete=phase_is_completed("linkedin", progress, manifest_path),
                        parents_dir=parents_dir, dossier_dir=dossier_dir,
                        enrichment=enrichment, profile_cache_dir=profile_cache_dir,
                        debug=debug, index=index,
                        inflight_slugs=guided_inflight_slugs(),
                        failed_notes=guided_failed_notes())
                else:
                    inflight = guided_inflight_slugs()
                    linkedin_done = phase_is_completed("linkedin", progress, manifest_path)
                    body = linkedin_card_body(
                        parents, progress,
                        linkedin_complete=linkedin_done,
                        parents_dir=parents_dir, dossier_dir=dossier_dir,
                        profile_cache_dir=profile_cache_dir,
                        exclude=frozenset(exclude | inflight) or None,
                        retargets_in_flight=len(inflight),
                        failed_notes=guided_failed_notes(),
                        auto_continue=not linkedin_done)
                self.send_bytes(body.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/person":
                # Directory pane fragment: read-only, served from the same
                # lock-free snapshot as the card prefetch paths.
                slug = str((params.get("slug") or [""])[0]).strip().lower()
                parent = next(
                    (item for item in cached_parents
                     if str(item.get("dossier_slug") or item.get("slug")
                            or "").strip().lower() == slug),
                    None)
                if parent is None:
                    self.send_bytes(b"not found", "text/plain", status=404)
                    return
                body = render_person_detail(parent, parents_dir, dossier_dir,
                                            profile_cache_dir)
                self.send_bytes(body.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/directory":
                # Browse-only view over the same in-memory model; never writes
                # and never starts jobs. Once the human flow is complete
                # (next_action realize — the agent's move), the old done
                # screen's go-back-to-Codex copy banner rides on top.
                with mutation_lock:
                    parents = parents_now()
                    status = workflow_status_from_parents(
                        parents, manifest_path=manifest_path,
                        enrichment_manifest_path=enrichment_manifest_path)
                self.send_bytes(directory_page_html(
                    parents, params, parents_dir=parents_dir,
                    dossier_dir=dossier_dir, profile_cache_dir=profile_cache_dir,
                    handoff=status["next_action"] == "realize"))
                return
            if parsed.path == "/api/avatar":
                pub = (params.get("pub") or [""])[0]
                avatar = load_avatar(pub, profile_cache_dir=profile_cache_dir,
                                     avatar_dir=avatar_dir)
                if not avatar:
                    self.send_bytes(b"not found", "text/plain", status=404)
                else:
                    body, content_type = avatar
                    self.send_bytes(body, content_type, cache="private, max-age=86400")
                return
            if parsed.path != "/":
                self.send_bytes(b"not found", "text/plain", status=404)
                return

            # Serialize the snapshot with decision writes. GET stays read-only for
            # the durable decision files; rendering the ENRICH page derives its
            # state from disk and starts-or-joins the one free-work job — so a
            # stranded manifest (external CLI write, restart, crash) never
            # survives a reload. Money is the only stop: a needs_approval state
            # renders the Approve button and starts nothing.
            with mutation_lock:
                parents = parents_now()
            enrichment_state = None
            if _phase_view(params, {}, manifest_path) == "enrich":
                selection = worth_selection_from_parents(
                    parents, manifest_path=manifest_path)
                enrichment_state = derive_enrichment_state(
                    selection, verdicts_path=verdicts_path, review_path=review_path,
                    facts_dir=facts_dir, manifest_path=enrichment_manifest_path,
                    job_running=_job_lock.locked())
                free_work = (enrichment_state["state"] == STATE_FREE_PENDING
                             or (enrichment_state["state"] == STATE_NEEDS_APPROVAL
                                 and not enrichment_state.get("approvable")
                                 and not enrichment_state.get("approval_current")))
                progress_now = review_progress(parents)
                selection_sha = str(selection.get("sha256") or "")
                failed_already = (
                    str(enrichment_state.get("status") or "") == "failed"
                    and selection_sha in _free_attempted)
                if run_jobs and free_work and not failed_already and (
                        progress_now["worth_pending"] == 0
                        or phase_is_completed("worth", progress_now, manifest_path)):
                    # Render keeps the derived free_pending/needs_approval screen
                    # ("Preparing…"); the next poll derives running + heartbeat.
                    # Feed-forward: a completed worth stage keeps the free job
                    # eligible even when later machine maybes exist.
                    if selection_sha:
                        _free_attempted.add(selection_sha)
                    start_free_enrichment_job()
                preview_now = str((params.get("preview") or [""])[0]).strip() == "1"
                if (not preview_now
                        and enrichment_state["state"] == STATE_DONE
                        and not enrichment_handoff_completed(manifest_path)):
                    # A DONE enrich stage is not a page — hand off server-side
                    # and land the browser straight on the LinkedIn stage (the
                    # old flow rendered a ceremony screen that a script then
                    # auto-clicked, flashing the stale page for a frame).
                    enrichment_now = read_enrichment_manifest(
                        enrichment_manifest_path, selection=selection)
                    write_enrichment_handoff(
                        enrichment_now, path=manifest_path,
                        review_path=review_path, synthetic_path=synthetic_path)
                    notify_agent()
                    self.send_response(303)
                    self.send_header("Location", "/?stage=linkedin")
                    self.end_headers()
                    return
            elif _phase_view(params, {}, manifest_path) == "linkedin":
                preview_now = str((params.get("preview") or [""])[0]).strip() == "1"
                progress_now = review_progress(parents)
                inflight_now = guided_inflight_slugs()
                queue_empty = not linkedin_review_queue(parents, inflight_now or None)
                if (not preview_now and queue_empty
                        and not phase_is_completed("linkedin", progress_now, manifest_path)):
                    # Same rule: an empty queue self-completes server-side, so
                    # the render below paints the go-back handoff state
                    # directly — never a Finish screen that clicks itself.
                    write_review_manifest(
                        "linkedin", "completed", progress_now, path=manifest_path,
                        review_path=review_path, synthetic_path=synthetic_path)
                    notify_agent()
            self.send_bytes(page_html(parents, params, review_path, parents_dir=parents_dir,
                                      dossier_dir=dossier_dir, manifest_path=manifest_path,
                                      enrichment_manifest_path=enrichment_manifest_path,
                                      profile_cache_dir=profile_cache_dir,
                                      verdicts_path=verdicts_path, facts_dir=facts_dir,
                                      enrichment_state=enrichment_state,
                                      job_running=_job_lock.locked(),
                                      inflight_slugs=guided_inflight_slugs(),
                                      failed_notes=guided_failed_notes()))

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in {"/decide", "/worth", "/complete", "/approve-enrichment",
                                   "/retarget", "/feedback", "/auth/login"}:
                self.send_bytes(b"not found", "text/plain", status=404)
                return
            origin = (self.headers.get("Origin") or "").strip()
            if origin and (urllib.parse.urlparse(origin).hostname or "").lower() not in {
                    "127.0.0.1", "localhost", "::1"}:
                self.send_bytes(b"cross-origin request rejected", "text/plain", status=403)
                return
            length = min(int(self.headers.get("Content-Length", "0")), 32_768)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            pub = (form.get("pub") or [""])[0]

            if parsed.path == "/auth/login":
                self.send_json({"ok": True, "status": start_auth_login()})
                return

            if parsed.path == "/approve-enrichment":
                try:
                    with mutation_lock:
                        current_parents = parents_now()
                        selection = worth_selection_from_parents(
                            current_parents, manifest_path=manifest_path)
                        enrichment = approve_enrichment_manifest(
                            enrichment_manifest_path, selection=selection)
                except ValueError as exc:
                    self.send_bytes(str(exc).encode("utf-8"), "text/plain; charset=utf-8",
                                    status=409)
                    return
                if run_jobs:
                    # The click IS the approval: run exactly the approved budget.
                    approved = float(
                        (enrichment.get("approval") or {}).get("approved_budget_usd") or 0)
                    start_approved_enrichment_job(approved)
                notify_agent()
                self.send_json({"ok": True, "enrichment": enrichment})
                return

            if parsed.path == "/complete":
                stage = (form.get("stage") or [""])[0].strip().lower()
                try:
                    with mutation_lock:
                        # Stage completion is a durable handoff to the agent —
                        # decide it from a FRESH rebuild, never the patched
                        # cache, so `review-status` can never disagree.
                        current_parents = refresh_parents_from_disk()
                        progress = review_progress(current_parents)
                        # Finish means finish — every stage completes on the
                        # user's word, exactly like worth's unresolved Maybes.
                        # Undecided people stay visible (the stepper always
                        # shows pending counts) and the queue stays reachable;
                        # in-flight guided re-research applies in the
                        # background either way.
                        if stage == "enrich":
                            selection = worth_selection_from_parents(
                                current_parents, manifest_path=manifest_path)
                            enrichment = read_enrichment_manifest(
                                enrichment_manifest_path, selection=selection)
                            manifest = write_enrichment_handoff(
                                enrichment, path=manifest_path,
                                review_path=review_path, synthetic_path=synthetic_path)
                        else:
                            # No enrichment kick here: the next enrich-page render
                            # derives the state and triggers the free job itself.
                            manifest = write_review_manifest(
                                stage, "completed", progress, path=manifest_path,
                                review_path=review_path, synthetic_path=synthetic_path)
                except ValueError as exc:
                    self.send_bytes(str(exc).encode("utf-8"), "text/plain; charset=utf-8",
                                    status=409)
                    return
                notify_agent()
                self.send_json({"ok": True, "manifest": manifest, "progress": progress})
                return

            if parsed.path == "/feedback":
                comment = (form.get("comment") or [""])[0].strip()
                action = (form.get("action") or [""])[0].strip()
                parent_slug = (form.get("parent_slug") or [""])[0].strip()
                if not comment or len(comment) > 4000:
                    self.send_bytes(b"comment must be 1-4000 characters",
                                    "text/plain", status=400)
                    return
                if action not in FEEDBACK_ACTIONS:
                    self.send_bytes(b"unknown feedback action", "text/plain", status=400)
                    return
                with mutation_lock:
                    hit = candidate_in_snapshot(pub, prefer_slug=parent_slug) if pub else None
                    if hit is not None:
                        target_parent, target_candidate = hit
                    else:
                        target_parent = next(
                            (p for p in parents_now()
                             if str(p.get("dossier_slug") or p.get("slug") or "")
                             == parent_slug), None)
                        target_candidate = dict(
                            _primary_candidate(target_parent)) if target_parent else {}
                    if target_parent is None:
                        self.send_bytes(b"person not found", "text/plain", status=404)
                        return
                    slug_now = str(target_parent.get("dossier_slug")
                                   or target_parent.get("slug") or parent_slug)
                    items = [item for item in
                             (guided_queue.snapshot() if guided_queue else [])
                             if item.get("slug") == slug_now
                             or (pub and item.get("pub") == pub.strip().lower())]
                    request = build_feedback_request(
                        target_parent, target_candidate, action=action,
                        comment=comment, retarget_items=items)
                payload = submit_directory_feedback(request)
                status = 200 if payload.get("status") == "submitted" else 502
                self.send_json({"ok": status == 200, **payload}, status=status)
                return

            if parsed.path == "/retarget":
                guidance = (form.get("guidance") or [""])[0].strip()
                parent_slug = (form.get("parent_slug") or [""])[0].strip()
                if not guidance or len(guidance) > 2000:
                    self.send_bytes(b"guidance must be 1-2000 characters",
                                    "text/plain", status=400)
                    return
                if guided_queue is None:
                    self.send_bytes(b"in-app jobs are disabled on this server",
                                    "text/plain", status=503)
                    return
                # Fail the submit HERE when it can only end in a dead job: a
                # guidance without a pasted URL needs Parallel research, and a
                # missing key means every such job fails after the card has
                # already advanced — the person silently loops back instead.
                url_hint, _ = linkedin_url_in_guidance(guidance)
                if not url_hint:
                    load_env()
                    if not (os.environ.get("PARALLEL_API_KEY") or "").strip():
                        self.send_bytes(
                            b"Re-research is unavailable on this install (no "
                            b"PARALLEL_API_KEY). Paste the right LinkedIn URL "
                            b"instead \xe2\x80\x94 that applies directly.",
                            "text/plain", status=503)
                        return
                with mutation_lock:
                    hit = candidate_in_snapshot(pub, prefer_slug=parent_slug) if pub else None
                    if hit is not None:
                        target_parent, target_candidate = hit
                    else:
                        # A person with no LinkedIn candidate yet: find them by
                        # slug and key the retarget on their first person_id.
                        target_parent = next(
                            (p for p in parents_now()
                             if str(p.get("dossier_slug") or p.get("slug") or "")
                             == parent_slug), None)
                        target_candidate = {}
                    if target_parent is None:
                        self.send_bytes(b"person not found", "text/plain", status=404)
                        return
                    # A synth- pub must never key a review.csv row (nothing reads
                    # synthetic candidates from review.csv, and apply_retargets
                    # would mint a contact-less person from it) — route it to the
                    # candidate person id exactly like /decide does.
                    if pub.strip().lower().startswith("synth-"):
                        pub = (synthetic_worth_key(synthetic_path, pub)
                               or str((target_parent.get("person_ids") or [""])[0])).strip()
                    # A ghost candidate (no pub) still has a review row — key the
                    # retarget on its row_key so the apply settles the actual row.
                    key = (pub or str(target_candidate.get("row_key") or "")
                           or str((target_parent.get("person_ids") or [""])[0])).strip()
                    if not key:
                        self.send_bytes(b"person has no review key", "text/plain", status=400)
                        return
                    request = GuidedRetarget(
                        slug=str(target_parent.get("dossier_slug")
                                 or target_parent.get("slug") or parent_slug),
                        pub=key,
                        name=str(target_parent.get("name") or ""),
                        guidance=guidance,
                        person_ids=tuple(
                            str(value) for value in target_parent.get("person_ids") or []),
                        linkedin_url=str(target_candidate.get("url") or ""),
                        # Settlement iterates REVIEW ROW KEYS: row_key covers
                        # ghost candidates (no pub) whose row is person-id-keyed.
                        candidate_pubs=tuple(sorted({
                            str(c.get("row_key") or c.get("pub") or "").strip().lower()
                            for c in target_parent.get("candidates") or []
                            if (c.get("row_key") or c.get("pub"))
                            and not c.get("synthetic")})),
                        synthetic_pubs=tuple(sorted({
                            str(c.get("row_key") or c.get("pub") or "").strip().lower()
                            for c in target_parent.get("candidates") or []
                            if (c.get("row_key") or c.get("pub"))
                            and c.get("synthetic")})),
                        queue_slug=str(target_parent.get("slug") or parent_slug),
                        submitted_at=now_iso(),
                        match_emails=tuple(
                            str(value) for value in target_candidate.get("match_emails") or []),
                        match_phones=tuple(
                            str(value) for value in target_candidate.get("match_phones") or []))
                try:
                    item = guided_queue.submit(request)
                except ValueError as exc:
                    self.send_bytes(str(exc).encode("utf-8"), "text/plain", status=409)
                    return
                # The guidance IS the feedback: auto-file it with the person's
                # full context, fire-and-forget — no popover, no extra input,
                # and never a UI error if the POST can't go out.
                try:
                    feedback_request = build_feedback_request(
                        target_parent, target_candidate, action="retarget",
                        comment=guidance,
                        retarget_items=[
                            entry for entry in guided_queue.snapshot()
                            if entry.get("pub") == item["pub"]
                            or entry.get("slug") == item["slug"]])
                    threading.Thread(
                        target=post_feedback_quietly, args=(feedback_request,),
                        name="retarget-feedback", daemon=True).start()
                except SystemExit as exc:
                    print(f"[feedback] skipped: {exc}", file=sys.stderr, flush=True)
                notify_agent()
                self.send_json({"ok": True, "item": item,
                                "estimated_cost_usd": ESTIMATED_COST_USD})
                return

            if parsed.path == "/worth":
                worth_val = (form.get("worth") or [""])[0].strip().lower()
                if worth_val not in {*USER_WORTH_VALUES, "restore"}:
                    self.send_bytes(b"worth must be yes, no, or restore", "text/plain", status=400)
                    return
                stored_worth = "" if worth_val == "restore" else worth_val
                worth_note = (form.get("note") or [""])[0].strip()[:2000]
                try:
                    with mutation_lock:
                        parents_now()
                        target_parent = worth_parent_in_snapshot(
                            pub, (form.get("parent_slug") or [""])[0])
                        if target_parent is None:
                            raise ValueError("stale or unknown parent worth card")
                        rows_now = review_rows_now()
                        # The queue key is the canonical parent-worth row. Child
                        # ids are evidence/membership only and never own the
                        # human decision.
                        model_row = target_parent.get("worth_row") or {}
                        write_key = str(model_row.get("key") or pub).strip().lower()
                        machine = model_row.get("machine") or {}
                        target_ids = {
                            str(value or "").strip().lower()
                            for value in target_parent.get("person_ids") or []
                        } - {""}
                        if stored_worth == "yes":
                            # Parent Yes supersedes any legacy child Exclude.
                            for key, legacy_row in rows_now.items():
                                legacy_pid = str(
                                    legacy_row.get("person_id") or ""
                                ).strip().lower()
                                if (
                                    (key in target_ids or legacy_pid in target_ids)
                                    and str(legacy_row.get("action") or "").strip().lower()
                                    == "exclude"
                                    and str(legacy_row.get("approved") or "").strip().lower()
                                    == "yes"
                                ):
                                    legacy_row["action"] = ""
                                    legacy_row["approved"] = ""
                        result = apply_worth_decision(
                            review_path,
                            write_key,
                            stored_worth,
                            rows=rows_now,
                            person_ids=list(model_row.get("person_ids") or []),
                            llm_worth=str(machine.get("decision") or ""),
                            llm_worth_reason=str(machine.get("reason") or ""),
                            user_worth_note=worth_note,
                            write=commit_rows,
                        )
                        notify_views()
                        gate_key = str(
                            (target_parent.get("person_ids") or [""])[0]
                        ).strip().lower()
                        gate = sync_synthetic_gate(synthetic_path, gate_key, stored_worth)
                        machine_decision = str(machine.get("decision") or "maybe")
                        effective = stored_worth or machine_decision
                        source = "user" if stored_worth else str(
                            machine.get("source") or "llm"
                        )
                        worth_state = {
                            "decision": effective,
                            "reason": (
                                "user decision"
                                if stored_worth
                                else str(machine.get("reason") or "")
                            ),
                            "source": source,
                        }
                        keepish = bool(gate and gate.get("approved") == "yes") or any(
                            str(candidate.get("approved") or "").strip().lower() == "yes"
                            and str(candidate.get("action") or "").strip().lower()
                            not in {"detach", "exclude"}
                            for candidate in target_parent.get("candidates") or []
                        )
                        connected = bool(target_parent.get("connection"))
                        state = {
                            "worth": worth_state,
                            "machine": machine,
                            "connected": connected,
                            "rejected": (
                                effective == "no"
                                and (source == "user" or (not keepish and not connected))
                            ),
                        }
                        row_now = rows_now.get(write_key) or {}
                        decided = gate or {
                            "action": (row_now.get("action") or "").strip().lower(),
                            "approved": (row_now.get("approved") or "").strip().lower(),
                        }
                        # worth_row is the SOLE worth truth for queue, tabs,
                        # and counts — patch it too, or the click lands on
                        # disk while the live model keeps serving the old
                        # decision until the next full rebuild.
                        def patch_worth_state(model_parent: dict[str, Any]) -> None:
                            model_parent["worth"] = state["worth"]
                            model_parent["machine_worth"] = state["machine"]
                            model_primary = _primary_candidate(model_parent)
                            model_primary["worth"] = state["worth"]
                            model_primary["machine_worth"] = state["machine"]
                            parent_row = model_parent.get("worth_row")
                            if parent_row is None:
                                return
                            machine_dec = (parent_row.get("machine") or {}).get("decision") or ""
                            if stored_worth:
                                parent_row["human"] = {"decision": stored_worth,
                                                       "updated_at": now_iso()}
                                parent_row["effective"] = stored_worth
                                parent_row["source"] = "user"
                            else:  # restore: back to the machine's verdict
                                parent_row["human"] = None
                                parent_row["effective"] = machine_dec or "maybe"
                                parent_row["source"] = ("llm" if machine_dec
                                                        else "default")

                        if target_parent:
                            patch_worth_state(target_parent)
                            primary = _primary_candidate(target_parent)
                            durable_candidate = next(
                                (candidate for candidate in target_parent.get("candidates") or []
                                 if str(candidate.get("pub") or "").strip().lower()
                                 == pub.strip().lower()),
                                None,
                            )
                            if durable_candidate:
                                durable_candidate["action"] = decided["action"]
                                durable_candidate["approved"] = decided["approved"]
                                durable_candidate["new_url"] = row_now.get(
                                    "new_linkedin_url", "")
                            if gate and primary.get("synthetic"):
                                primary["action"] = gate["action"]
                                primary["approved"] = gate["approved"]
                            # Canonical parent keys are unique, so no sibling
                            # propagation or identity aliasing is necessary.
                            attach_keys = {write_key}
                            for sibling in cached_parents:
                                if sibling is target_parent:
                                    continue
                                sibling_key = str(
                                    ((sibling.get("worth_row") or {}).get("key") or "")
                                ).strip().lower()
                                if sibling_key in attach_keys:
                                    patch_worth_state(sibling)
                        notify_views()
                        current_parents = cached_parents
                        progress = review_progress(current_parents)
                        if progress["worth_pending"] == 0:
                            # The patched cache says done — confirm against a
                            # FRESH rebuild before declaring completion, so the
                            # agent's own fresh read can never disagree.
                            current_parents = refresh_parents_from_disk()
                            progress = review_progress(current_parents)
                        if progress["worth_pending"] == 0:
                            review_manifest = write_review_manifest(
                                "worth", "completed", progress, path=manifest_path,
                                review_path=review_path, synthetic_path=synthetic_path)
                            next_stage = "enrich"
                        else:
                            review_manifest = write_review_manifest(
                                "worth", "awaiting_user", progress, path=manifest_path,
                                review_path=review_path, synthetic_path=synthetic_path)
                            next_stage = "worth"
                        counts = summarize(current_parents)
                        state_token = state_token_for(current_parents, progress)
                except ValueError as exc:
                    self.send_bytes(str(exc).encode("utf-8"), "text/plain; charset=utf-8",
                                    status=400)
                    return
                if worth_note and stored_worth in {"yes", "no"}:
                    # The note IS the feedback (same contract as retarget
                    # guidance): auto-file it with the person's context,
                    # fire-and-forget — never a UI error if the POST can't go out.
                    try:
                        feedback_request = build_feedback_request(
                            target_parent, dict(_primary_candidate(target_parent)),
                            action=f"worth_{stored_worth}", comment=worth_note)
                        threading.Thread(
                            target=post_feedback_quietly, args=(feedback_request,),
                            name="worth-feedback", daemon=True).start()
                    except SystemExit as exc:
                        print(f"[feedback] skipped: {exc}", file=sys.stderr, flush=True)
                notify_agent()
                self.send_json({
                    "ok": True, "pub": pub, **result,
                    "action": decided["action"], "approved": decided["approved"],
                    "new_url": row_now.get("new_linkedin_url", ""),
                    "effective": state["worth"]["decision"],
                    "source": state["worth"]["source"],
                    "reason": state["worth"]["reason"],
                    "rejected": state["rejected"],
                    "counts": counts,
                    "progress": progress,
                    "review_manifest": review_manifest,
                    "next_stage": next_stage,
                    "state_token": state_token,
                })
                return

            decision = (form.get("decision") or [""])[0]
            new_url = (form.get("new_url") or [""])[0]
            parent_slug = (form.get("parent_slug") or [""])[0]
            if not pub or decision not in {"keep", "detach", "fix", "reset", "exclude"}:
                self.send_bytes(b"bad request", "text/plain", status=400)
                return
            try:
                with mutation_lock:
                    parents_now()
                    pub_lower = pub.strip().lower()
                    target = candidate_in_snapshot(pub, prefer_slug=parent_slug)
                    if not target:
                        raise ValueError(f"review row not found: {pub}")
                    target_parent, target_candidate = target
                    actual_slug = str(target_parent.get("slug") or "")
                    if parent_slug and parent_slug != actual_slug:
                        raise ValueError("stale or mismatched person card")
                    synthetic_target = pub_lower.startswith("synth-")
                    if synthetic_target:
                        worth_key = synthetic_worth_key(synthetic_path, pub)
                        if decision == "fix":
                            if not worth_key:
                                raise ValueError(f"synthetic worth key not found: {pub}")
                            result = apply_decision(
                                review_path, verdicts_path, worth_key, decision, new_url,
                                confirm_threshold, detach_threshold, write=commit_rows)
                            rows = load_override_rows(review_path)
                            rows[worth_key.lower()]["person_id"] = (
                                rows[worth_key.lower()].get("person_id") or worth_key)
                            commit_rows(review_path, rows)
                            apply_synthetic_decision(synthetic_path, pub, "detach")
                            keepish = True
                            target_candidate["action"] = "verify"
                            target_candidate["approved"] = "no"
                            target_candidate["new_url"] = ""
                        else:
                            result = apply_synthetic_decision(synthetic_path, pub, decision)
                            keepish = result["approved"] == "yes"
                            target_candidate["action"] = result["action"]
                            target_candidate["approved"] = result["approved"]
                            target_candidate["new_url"] = result.get("new_url", "")
                    else:
                        result = apply_decision(
                            review_path, verdicts_path, pub, decision, new_url,
                            confirm_threshold, detach_threshold, write=commit_rows)
                        worth_key, keepish = pub, None
                        target_candidate["action"] = result["action"]
                        target_candidate["approved"] = result["approved"]
                        target_candidate["new_url"] = result.get("new_url", "")

                    # ANY answer resolves a multi-match person: every OTHER unapplied
                    # option on this parent is withdrawn as a link-level No decision
                    # (never a person reject), so one click resolves the whole parent
                    # and it does not reappear. That includes Skip (detach) — a Skip
                    # that settled only the primary re-served the same person with the
                    # remaining options next card. A synthetic sibling's gate lives in
                    # synthetic-people.csv, so it is withdrawn through its approve gate
                    # (link-level on a mixed parent per is_effective_no); a real
                    # sibling is detached in review.csv. Display-detached rows (judge
                    # wrong_person >= bar, approved still '') are settled here too —
                    # leaving them unwritten kept them eligible for paid re-research.
                    resolved_pubs = [pub_lower]
                    target_row_key = str(target_candidate.get("row_key")
                                         or pub_lower).strip().lower()
                    if decision in {"keep", "fix", "detach"}:
                        for sibling in target_parent.get("candidates") or []:
                            # Settle by the sibling's REVIEW ROW KEY: a ghost
                            # candidate has no pub, but its person-id-keyed row
                            # must settle too or the parent cycles back pending.
                            sibling_pub = str(sibling.get("row_key")
                                              or sibling.get("pub") or "").strip().lower()
                            if not sibling_pub or sibling_pub in {pub_lower, target_row_key}:
                                continue
                            sibling_approved = str(sibling.get("approved") or "").strip().lower()
                            if sibling.get("synthetic"):
                                # A synthetic option is pending unless the user already gated it
                                # (auto == still pending, matching pending_linkedin_candidates).
                                if sibling_approved in {"yes", "no"}:
                                    continue
                                try:
                                    apply_synthetic_decision(synthetic_path, sibling_pub, "detach")
                                except ValueError as exc:
                                    # Best-effort withdrawal: a row pruned between render
                                    # and click must not 400 the user's applied decision.
                                    print(f"[decide] sibling skipped: {exc}",
                                          file=sys.stderr, flush=True)
                                    continue
                                sibling["action"] = "verify"
                                sibling["approved"] = "no"
                                sibling["new_url"] = ""
                            else:
                                if (sibling_approved in {"yes", "no"}
                                        or candidate_state(sibling) not in {"review", "detached"}):
                                    continue
                                apply_decision(
                                    review_path, verdicts_path, sibling_pub, "detach", "",
                                    confirm_threshold, detach_threshold, write=commit_rows)
                                sibling["action"] = "detach"
                                sibling["approved"] = "yes"
                                sibling["new_url"] = ""
                            resolved_pubs.append(sibling_pub)
                        # Thinner synthetic duplicates pruned from the display model
                        # still hold pending gates in synthetic-people.csv — settle
                        # them with the parent so no row stays undecided forever.
                        for pruned_pub in target_parent.get("pruned_synthetic_pubs") or []:
                            try:
                                apply_synthetic_decision(
                                    synthetic_path, str(pruned_pub).strip().lower(), "detach")
                            except ValueError:
                                pass
                        # Carry the UNION of every candidate's contacts (kept + withdrawn
                        # siblings) onto the KEPT identity, so a withdrawn sibling's real
                        # email/phone is never lost. No-op for a single-candidate parent.
                        carry_forward_multi_option_contacts(
                            target_parent, target_candidate,
                            synthetic_path=synthetic_path, people_csv=people_csv)

                    notify_views()
                    current_parents = cached_parents
                    progress = review_progress(current_parents)
                    invalidate_manifest("linkedin", progress)
                    payload: dict[str, Any] = {
                        "ok": True, "pub": pub, **result,
                        "counts": summarize(current_parents),
                        "progress": progress,
                        "resolved_pubs": resolved_pubs,
                        "state_token": state_token_for(current_parents, progress),
                    }
                    if worth_key:
                        state = effective_no_for_key(
                            worth_key, load_override_rows(review_path), facts_dir,
                            keepish=keepish, connections=connection_keys)
                        payload.update({
                            "rejected": state["rejected"],
                            "effective": state["worth"]["decision"],
                            "source": state["worth"]["source"],
                        })
            except ValueError as exc:
                self.send_bytes(str(exc).encode("utf-8"), "text/plain; charset=utf-8",
                                status=400)
                return
            decide_note = (form.get("note") or [""])[0].strip()[:2000]
            if decide_note and decision == "detach":
                # A Skip's optional why-note rides the decision POST (the same
                # contract as the worth note; only the Skip UI sends one, and
                # Skip decides `detach`): auto-file it with the person's
                # context, fire-and-forget — never a UI error if it can't go out.
                try:
                    feedback_request = build_feedback_request(
                        target_parent, dict(target_candidate),
                        action="skip", comment=decide_note)
                    threading.Thread(
                        target=post_feedback_quietly, args=(feedback_request,),
                        name="skip-feedback", daemon=True).start()
                except SystemExit as exc:
                    print(f"[feedback] skipped: {exc}", file=sys.stderr, flush=True)
            notify_agent()
            self.send_json(payload)

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler


def cmd_serve(args: argparse.Namespace) -> None:
    # Stage-entry legacy scrubs: an owner.json predating the phones field gets
    # the owner's own numbers stamped so the identifier policy can drop them,
    # review rows written under the pre-decisive judge-apply policy get the
    # 2026-08 promotions/demotions without a re-judge, and parents left
    # half-decided by the pre-v1.15.3 single-row /decide get their pending
    # sibling rows (and synthetic gates) settled once.
    ensure_owner_phones(OWNER_JSON)
    resolve_stored_identity_policy(Path(args.review), INDEX_JSON, DEFAULT_PEOPLE_CSV,
                                   Path(args.synthetic_people))
    review_path = Path(args.review)
    verdicts_path = Path(args.verdicts)
    parents_dir = Path(args.parents_dir)
    synthetic_path = Path(args.synthetic_people)
    manifest_path = Path(args.manifest)

    # The sqlite store: review.sqlite lives next to review.csv. Session writes
    # commit HERE (the transaction is durability, the CSV an export of it), so
    # first finish any export a crash interrupted, then absorb between-session
    # CLI writes (strict import — an unrepresentable row refuses serve by name).
    review_db = ReviewDb(review_path.with_suffix(".sqlite"))
    review_db.recover_pending_export(review_path, synthetic_path)
    if review_db.needs_import(review_path):
        review_db.import_stores(review_path, synthetic_path)

    # "directory" is the read-only browse PATH, not a review stage: bare
    # `review` always lands there and never begins a people-review revision.
    # Workflow callers opt into a staged view explicitly with --stage.
    def query_for(stage: str) -> str:
        return ("directory" if stage == "directory"
                else f"?stage={urllib.parse.quote(stage)}")

    def build_initial_parents() -> list[dict[str, Any]]:
        if review_db.needs_import(review_path):
            review_db.import_stores(review_path, synthetic_path)
        return _all_review_parents(
            verdicts_path, review_path, synthetic_path,
            Path(args.facts_dir), Path(args.people_csv),
            Path(args.parents_dir), Path(args.dossier_dir),
            Path(args.profile_cache_dir),
            rows=review_db.export_review_rows())

    def begin_people_review(progress: dict[str, int]) -> None:
        write_review_manifest("worth", "awaiting_user", progress, path=manifest_path,
                              review_path=review_path, synthetic_path=synthetic_path,
                              launched=True)
        if progress["worth_pending"] == 0:
            write_review_manifest("worth", "completed", progress, path=manifest_path,
                                  review_path=review_path, synthetic_path=synthetic_path)

    # Reopening a live UI is read-only. Starting a new server begins one fresh
    # People-review revision; later stages are merely direct views into files.
    # The reuse probe runs BEFORE the session flock below — the live server is
    # the one holding it, so locking first would refuse the very server we are
    # about to reuse (bin/deep-context's enrichment-running deferral and the
    # directory browse landing both reach this path with a server up).
    status_payload: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(
                f"http://{args.host}:{args.port}/api/status", timeout=1) as response:
            status_payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError,
            json.JSONDecodeError, TimeoutError):
        status_payload = {}
    if status_payload.get("primitive") == "reconcile_review_web":
        live_manifest = str(status_payload.get("manifest") or "").strip()
        try:
            wrong_server = bool(
                live_manifest
                and Path(live_manifest).resolve() != manifest_path.resolve())
        except (OSError, RuntimeError):
            wrong_server = live_manifest != str(manifest_path)
        if wrong_server:
            raise SystemExit(
                f"Port {args.port} belongs to a review server for {live_manifest}; "
                f"this review uses {manifest_path}"
            )
        requested_stage = args.stage or "directory"
        requested_url = f"http://{args.host}:{args.port}/{query_for(requested_stage)}"
        if args.fresh and requested_stage == "worth":
            begin_people_review(review_progress(build_initial_parents()))
        print(json.dumps({"primitive": "reconcile_review_web", "status": "reused",
                          "url": requested_url, "manifest": str(manifest_path),
                          "stage": requested_stage}, indent=2))
        if args.open:
            webbrowser.open(requested_url)
        return

    # SINGLE-WRITER: hold the advisory session lock for the server lifetime so
    # mutating CLI primitives refuse to run concurrently (canonical store only —
    # temp-path test servers do not own the real session). Taken only once we
    # are actually starting a server, never on the reuse path above.
    session_lock = None
    try:
        canonical = review_path.resolve() == LINKEDIN_OVERRIDES_CSV.resolve()
    except OSError:
        canonical = False
    if canonical:
        session_lock = acquire_review_session_lock()  # held until process exit  # noqa: F841
    parents = build_initial_parents()
    progress = review_progress(parents)
    requested_stage = args.stage or "directory"
    if requested_stage == "worth":
        begin_people_review(progress)
    # No launch self-heal kick: enrichment state is DERIVED at every enrich-page
    # render (derive_enrichment_state), and the render starts-or-joins the one
    # free-work job — so a stranded persisted state cannot survive a reload.
    # No push notifier: the agent watches state with `review-status --wait`,
    # which stats the same durable files this server writes. Simplicity wins.
    server = ThreadingHTTPServer((args.host, args.port),
                                 make_handler(review_path, verdicts_path, parents_dir, Path(args.dossier_dir),
                                              args.confirm_threshold, args.detach_threshold,
                                              synthetic_path=synthetic_path,
                                              facts_dir=Path(args.facts_dir),
                                              people_csv=Path(args.people_csv),
                                              manifest_path=manifest_path,
                                              enrichment_manifest_path=Path(args.enrichment_manifest),
                                              profile_cache_dir=Path(args.profile_cache_dir),
                                              avatar_dir=Path(args.avatar_dir),
                                              initial_parents=parents,
                                              review_db=review_db))
    host, port = server.server_address
    url = f"http://{host}:{port}/{query_for(requested_stage)}"
    print(json.dumps({"primitive": "reconcile_review_web", "status": "serving", "url": url,
                      "manifest": str(manifest_path), "parents": len(parents),
                      "progress": progress}, indent=2))
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)


def workflow_status_from_parents(
    parents: list[dict[str, Any]], *,
    manifest_path: Path = REVIEW_MANIFEST,
    enrichment_manifest_path: Path = ENRICH_MANIFEST,
) -> dict[str, Any]:
    """Read-only next-action contract from an already-loaded server snapshot."""
    progress = review_progress(parents)
    selection = worth_selection_from_parents(parents, manifest_path=manifest_path)
    enrichment = read_enrichment_manifest(
        enrichment_manifest_path, selection=selection)
    worth_complete = phase_is_completed("worth", progress, manifest_path)
    enrich_continued = enrichment_handoff_completed(manifest_path)
    linkedin_complete = phase_is_completed("linkedin", progress, manifest_path)
    enrich_status = str(enrichment.get("status") or "not_started")
    approval_current = bool(enrichment.get("approval_current"))
    approved_budget = (float((enrichment.get("approval") or {}).get("approved_budget_usd") or 0)
                       if approval_current else 0.0)

    # Feed-forward: only an UNCOMPLETED worth stage asks for people review.
    # Machine-created pending after completion surfaces in the Review tab and
    # never regresses the stage (the browser navigates off this value).
    if not worth_complete:
        next_action = "review_people"
    elif enrich_status in {"not_started", "stale"}:
        next_action = "preview_enrichment"
    elif enrich_status == "needs_approval" and int(enrichment.get("would_submit") or 0) == 0:
        next_action = "run_enrichment_from_cache"
    elif enrich_status == "needs_approval" and approval_current:
        next_action = "run_approved_enrichment"
    elif enrich_status == "needs_approval":
        next_action = "await_enrichment_approval"
    elif enrich_status in {"running", "submitted"}:
        next_action = "wait_for_enrichment"
    elif enrich_status in {"failed", "completed_with_errors"}:
        next_action = "retry_enrichment"
    elif enrich_status == "research_complete":
        next_action = "assemble_synthetic"
    elif enrich_status != "completed":
        next_action = "wait_for_enrichment"
    elif not enrich_continued:
        next_action = "continue_enrichment"
    elif progress["linkedin_pending"]:
        next_action = "review_linkedin"
    elif not linkedin_complete:
        next_action = "finish_linkedin"
    else:
        next_action = "realize"

    commands = {
        "review_people": "bin/deep-context review",
        "preview_enrichment": (
            "bin/deep-context reconcile-deep-research --dry-run "
            "--include-candidates --include-plausibly-absent"
        ),
        "await_enrichment_approval": "wait for the user to click Approve in Enrich Contacts",
        "run_approved_enrichment": (
            "bin/deep-context reconcile-deep-research "
            "--include-candidates --include-plausibly-absent --approve "
            f"--budget {approved_budget:.2f}"
        ),
        "run_enrichment_from_cache": (
            "bin/deep-context reconcile-deep-research "
            "--include-candidates --include-plausibly-absent"
        ),
        "wait_for_enrichment": "bin/deep-context review-status",
        "retry_enrichment": "inspect the fixed enrichment manifest error",
        "assemble_synthetic": "bin/deep-context assemble-synthetic",
        "continue_enrichment": "wait for the user to click Continue in Enrich Contacts",
        "review_linkedin": "wait for LinkedIn Yes/No decisions in the review UI",
        "finish_linkedin": "wait for the user to click Finish in Check LinkedIn",
        "realize": "bin/deep-context stop && bin/deep-context realize",
    }
    return {
        "primitive": "deep_context_review_status",
        "status": "ok",
        "next_action": next_action,
        "command": commands[next_action],
        "poll_after_seconds": 60,
        "progress": progress,
        "selection": selection,
        "review_manifest": read_review_manifest(manifest_path),
        "enrichment": enrichment,
    }


def workflow_status(
    *, review_path: Path = LINKEDIN_OVERRIDES_CSV,
    verdicts_path: Path = VERDICTS_JSONL,
    synthetic_path: Path = SYNTHETIC_PEOPLE_CSV,
    facts_dir: Path = FACTS_DIR,
    people_csv: Path = DEFAULT_PEOPLE_CSV,
    manifest_path: Path = REVIEW_MANIFEST,
    enrichment_manifest_path: Path = ENRICH_MANIFEST,
    parents_dir: Path = PARENTS_DIR,
    dossier_dir: Path = DOSSIER_DIR,
    profile_cache_dir: Path = PROFILE_CACHE_DIR,
) -> dict[str, Any]:
    """Read-only next-action contract for the agent's one-minute CLI poll."""
    parents = _all_review_parents(
        verdicts_path, review_path, synthetic_path, facts_dir, people_csv,
        parents_dir, dossier_dir, profile_cache_dir)
    return workflow_status_from_parents(
        parents, manifest_path=manifest_path,
        enrichment_manifest_path=enrichment_manifest_path)


AGENT_ACTIONS = {
    "retry_enrichment",
    "realize",
}
