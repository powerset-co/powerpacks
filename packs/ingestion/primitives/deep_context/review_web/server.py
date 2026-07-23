"""HTTP routing, asset serving, and in-app workflow jobs for review."""

from __future__ import annotations

import argparse
import json
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
    STATE_FREE_PENDING,
    STATE_NEEDS_APPROVAL,
    STATUS_COMPLETED,
    STATUS_RESEARCH_COMPLETE,
    derive_enrichment_state,
    read_enrichment_manifest,
)
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    ENRICH_MANIFEST,
    FACTS_DIR,
    LINKEDIN_OVERRIDES_CSV,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    REVIEW_MANIFEST,
    VERDICTS_JSONL,
    now_iso,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    _write_override_rows,
    load_override_rows,
)

from packs.ingestion.primitives.deep_context import assemble_synthetic_profile, prefetch_profiles, reconcile_deep_research
from .decisions import apply_decision, apply_synthetic_decision, apply_worth_decision, carry_forward_multi_option_contacts, sync_synthetic_gate
from .model import SYNTHETIC_PEOPLE_CSV, USER_WORTH_VALUES, _all_review_parents, _worth_key, candidate_state, effective_no_for_key, load_avatar, load_connection_keys, summarize, synthetic_worth_key
from .rendering import DECISION_CHUNK_SIZE, REVIEW_CSS, REVIEW_JS, _phase_view, _primary_candidate, decision_rows_payload, linkedin_card_body, linkedin_review_body, page_html, render_dossier_markdown, render_worth_card, worth_review_body
from .workflow import approve_enrichment_manifest, browser_stage_for_next_action, current_worth_selection, enrichment_handoff_completed, needs_worth_review, phase_is_completed, read_review_manifest, review_progress, review_state_token, worth_selection_from_parents, write_enrichment_handoff, write_review_manifest

def _manifest_for_review_path(review_path: Path) -> Path:
    try:
        if review_path.resolve() == LINKEDIN_OVERRIDES_CSV.resolve():
            return REVIEW_MANIFEST
    except (OSError, RuntimeError):
        pass
    return review_path.parent / "review" / "manifest.json"


ENRICH_FLAGS = ["--include-candidates", "--include-plausibly-absent"]


_job_lock = threading.Lock()


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

    threading.Thread(target=runner, name=f"pipeline-job-{name}", daemon=True).start()


def _post_enrichment_chain() -> None:
    """Free follow-ups once research is done: no-LinkedIn cards + profile cache."""
    assemble_synthetic_profile.main([])
    prefetch_profiles.main(["--fetch"])


def _free_enrichment_steps() -> None:
    """The ONE free-work pass: run the enrichment continue with a $0 ceiling.
    Zero net-new does ALL the free work (reuse + fingerprint-cached retarget
    judging) and the follow-up chain; any real spend hits the primitive's budget
    gate, which stamps a current needs_approval receipt WITHOUT spending a cent
    (the Approve button owns money). No convergence loop: the chain may re-drift
    the selection, and the next enrich-page render re-derives and re-triggers."""
    reconcile_deep_research.main([*ENRICH_FLAGS, "--approve", "--budget", "0.00"])
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
        reconcile_deep_research.main(
            [*ENRICH_FLAGS, "--approve", "--budget", f"{budget:.2f}"])
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
                 run_jobs: bool | None = None):
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

    def input_signature() -> tuple[tuple[str, int, int], ...]:
        """Cheap invalidation key for files that can change the review queue.

        Facts/dossiers are fixed before review. Provider work changes the durable
        review/synthetic CSVs, so those files are sufficient to notice external
        agent progress without recursively scanning thousands of artifacts.
        """
        values = []
        # ENRICH_MANIFEST included so an enrichment state change (in-app or an
        # external CLI completion) invalidates the cached model — without it
        # the enrich page served a stale phase until a manual server restart.
        for path in (review_path, verdicts_path, synthetic_path, people_csv,
                     ENRICH_MANIFEST):
            try:
                stat = path.stat()
                values.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                values.append((str(path), 0, 0))
        return tuple(values)

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

    cached_parents = (
        initial_parents if initial_parents is not None else
        _all_review_parents(
            verdicts_path, review_path, synthetic_path, facts_dir, people_csv,
            parents_dir, dossier_dir, profile_cache_dir)
    )
    cached_signature = input_signature()
    connection_keys = load_connection_keys(people_csv)

    def parents_now() -> list[dict[str, Any]]:
        """Return the in-memory SPA model, reloading only after an external write."""
        nonlocal cached_parents, cached_signature, connection_keys
        signature = input_signature()
        if signature != cached_signature:
            cached_parents = _all_review_parents(
                verdicts_path, review_path, synthetic_path, facts_dir, people_csv,
                parents_dir, dossier_dir, profile_cache_dir)
            connection_keys = load_connection_keys(people_csv)
            cached_signature = signature
        return cached_parents

    def accept_local_write() -> None:
        """The caller already updated ``cached_parents`` to mirror its durable write."""
        nonlocal cached_signature
        cached_signature = input_signature()

    def refresh_parents_from_disk() -> list[dict[str, Any]]:
        """Rebuild the model FRESH from files, discarding optimistic patches.

        Used at stage-completion boundaries: the agent's `review-status` CLI
        always rebuilds fresh, so "completed" must only ever be written when a
        fresh derivation agrees — otherwise the UI shows "waiting on the agent"
        while the agent's own read says N people are still pending (the
        off-by-N handoff split)."""
        nonlocal cached_parents, cached_signature, connection_keys
        cached_parents = _all_review_parents(
            verdicts_path, review_path, synthetic_path, facts_dir, people_csv,
            parents_dir, dossier_dir, profile_cache_dir)
        connection_keys = load_connection_keys(people_csv)
        cached_signature = input_signature()
        return cached_parents

    # Parsed review.csv rows, cached so a decision click does not re-read a
    # potentially large CSV per POST. Invalidation mirrors input_signature:
    # any external write changes the file stat and forces a reload; our own
    # writes refresh the stat via accept_rows_write (the dict itself was
    # mutated in place by apply_worth_decision, so it is already current).
    cached_rows: dict[str, dict[str, str]] | None = None
    cached_rows_sig: tuple[int, int] | None = None

    def _review_rows_sig() -> tuple[int, int]:
        try:
            stat = review_path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return (0, 0)

    def review_rows_now() -> dict[str, dict[str, str]]:
        nonlocal cached_rows, cached_rows_sig
        sig = _review_rows_sig()
        if cached_rows is None or sig != cached_rows_sig:
            cached_rows = load_override_rows(review_path)
            cached_rows_sig = sig
        return cached_rows

    def accept_rows_write() -> None:
        nonlocal cached_rows_sig
        cached_rows_sig = _review_rows_sig()

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
        """The cached parent this decision was rendered from. The card/row
        sends its parent slug (unique), because a worth KEY is not unique:
        two split parents can share one pub with DISTINCT worth_row dicts,
        and first-hit-by-key patched the wrong twin — leaving an unkillable
        pending zombie in the live model (the disk write was always right;
        only a restart cleared it). Slug match first, key fallback."""
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
                                             exclude=exclude or None)
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
                        debug=debug, index=index)
                else:
                    body = linkedin_card_body(
                        parents, progress,
                        linkedin_complete=phase_is_completed("linkedin", progress, manifest_path),
                        parents_dir=parents_dir, dossier_dir=dossier_dir,
                        profile_cache_dir=profile_cache_dir,
                        exclude=exclude or None)
                self.send_bytes(body.encode("utf-8"), "text/html; charset=utf-8")
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
                if run_jobs and free_work and (
                        progress_now["worth_pending"] == 0
                        or phase_is_completed("worth", progress_now, manifest_path)):
                    # Render keeps the derived free_pending/needs_approval screen
                    # ("Preparing…"); the next poll derives running + heartbeat.
                    # Feed-forward: a completed worth stage keeps the free job
                    # eligible even when later machine maybes exist.
                    start_free_enrichment_job()
            self.send_bytes(page_html(parents, params, review_path, parents_dir=parents_dir,
                                      dossier_dir=dossier_dir, manifest_path=manifest_path,
                                      enrichment_manifest_path=enrichment_manifest_path,
                                      profile_cache_dir=profile_cache_dir,
                                      verdicts_path=verdicts_path, facts_dir=facts_dir,
                                      enrichment_state=enrichment_state,
                                      job_running=_job_lock.locked()))

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in {"/decide", "/worth", "/complete", "/approve-enrichment"}:
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
                        pending_key = {"worth": "worth_pending",
                                       "linkedin": "linkedin_pending"}.get(stage)
                        if pending_key and progress[pending_key]:
                            self.send_bytes(
                                (f"{progress[pending_key]} people still need review — "
                                 "the page refreshed with the current queue").encode("utf-8"),
                                "text/plain; charset=utf-8", status=409)
                            return
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

            if parsed.path == "/worth":
                worth_val = (form.get("worth") or [""])[0].strip().lower()
                if worth_val not in {*USER_WORTH_VALUES, "restore"}:
                    self.send_bytes(b"worth must be yes, no, or restore", "text/plain", status=400)
                    return
                stored_worth = "" if worth_val == "restore" else worth_val
                try:
                    with mutation_lock:
                        parents_now()
                        target_parent = worth_parent_in_snapshot(
                            pub, (form.get("parent_slug") or [""])[0])
                        rows_now = review_rows_now()
                        # The posted pub is the QUEUE key; the durable mark must
                        # land on a row worth_view ATTACHES to the parent the
                        # user decided (row key or row.person_id inside that
                        # parent's identities). Split twins can share a queue
                        # key naming a row whose person_id belongs to the OTHER
                        # twin — writing there decides the wrong person, and the
                        # served twin re-derives pending on every rebuild: an
                        # undecidable zombie no click could ever clear.
                        write_key = pub.strip().lower()
                        target_ids = {str(value or "").strip().lower()
                                      for value in (target_parent or {}).get("person_ids") or []}
                        target_ids.discard("")
                        if target_parent and target_ids:
                            posted_row = rows_now.get(write_key) or {}
                            posted_pid = str(posted_row.get("person_id") or "").strip().lower()
                            if write_key not in target_ids and posted_pid not in target_ids:
                                write_key = sorted(target_ids)[0]
                        result = apply_worth_decision(review_path, write_key, stored_worth,
                                                      rows=rows_now)
                        accept_rows_write()
                        gate = sync_synthetic_gate(synthetic_path, write_key, stored_worth)
                        state = effective_no_for_key(
                            write_key, rows_now, facts_dir,
                            keepish=(gate["approved"] == "yes") if gate else None,
                            connections=connection_keys)
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
                            model_row = model_parent.get("worth_row")
                            if model_row is None:
                                return
                            machine_dec = (model_row.get("machine") or {}).get("decision") or ""
                            if stored_worth:
                                model_row["human"] = {"decision": stored_worth,
                                                      "updated_at": now_iso()}
                                model_row["effective"] = stored_worth
                                model_row["source"] = "user"
                            else:  # restore: back to the machine's verdict
                                model_row["human"] = None
                                model_row["effective"] = machine_dec or "maybe"
                                model_row["source"] = ("llm" if machine_dec
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
                            # A FRESH rebuild derives every parent the written
                            # row ATTACHES to (worth_view rule 3: row key or
                            # row.person_id inside the person's identities) —
                            # so the cache must patch exactly that same set, no
                            # more (an unrelated twin sharing only the queue
                            # key stays independently decidable) and no less
                            # (a merged parent sharing the identity must flip,
                            # or it keeps the queue serving a decided person).
                            written_pid = str((rows_now.get(write_key) or {})
                                              .get("person_id") or "").strip().lower()
                            attach_keys = {write_key, written_pid} - {""}
                            for sibling in cached_parents:
                                if sibling is target_parent:
                                    continue
                                sibling_ids = {str(value or "").strip().lower()
                                               for value in sibling.get("person_ids") or []}
                                if sibling_ids & attach_keys:
                                    patch_worth_state(sibling)
                        accept_local_write()
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
                                confirm_threshold, detach_threshold)
                            rows = load_override_rows(review_path)
                            rows[worth_key.lower()]["person_id"] = (
                                rows[worth_key.lower()].get("person_id") or worth_key)
                            _write_override_rows(review_path, rows)
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
                            confirm_threshold, detach_threshold)
                        worth_key, keepish = pub, None
                        target_candidate["action"] = result["action"]
                        target_candidate["approved"] = result["approved"]
                        target_candidate["new_url"] = result.get("new_url", "")

                    # One affirmative answer resolves a multi-match person: every OTHER
                    # still-pending option on this parent is withdrawn as a link-level No
                    # decision (never a person reject), so picking one option resolves the
                    # whole parent and it does not reappear. A synthetic sibling's gate lives
                    # in synthetic-people.csv, so it is withdrawn through its approve gate; a
                    # real-LinkedIn sibling is detached in review.csv exactly as before.
                    resolved_pubs = [pub_lower]
                    if decision in {"keep", "fix"}:
                        for sibling in target_parent.get("candidates") or []:
                            sibling_pub = str(sibling.get("pub") or "").strip().lower()
                            if not sibling_pub or sibling_pub == pub_lower:
                                continue
                            sibling_approved = str(sibling.get("approved") or "").strip().lower()
                            if sibling.get("synthetic"):
                                # A synthetic option is pending unless the user already gated it
                                # (auto == still pending, matching pending_linkedin_candidates).
                                if sibling_approved in {"yes", "no"}:
                                    continue
                                apply_synthetic_decision(synthetic_path, sibling_pub, "detach")
                                sibling["action"] = "verify"
                                sibling["approved"] = "no"
                                sibling["new_url"] = ""
                            else:
                                if candidate_state(sibling) != "review":
                                    continue
                                apply_decision(
                                    review_path, verdicts_path, sibling_pub, "detach", "",
                                    confirm_threshold, detach_threshold)
                                sibling["action"] = "detach"
                                sibling["approved"] = "yes"
                                sibling["new_url"] = ""
                            resolved_pubs.append(sibling_pub)
                        # Carry the UNION of every candidate's contacts (kept + withdrawn
                        # siblings) onto the KEPT identity, so a withdrawn sibling's real
                        # email/phone is never lost. No-op for a single-candidate parent.
                        carry_forward_multi_option_contacts(
                            target_parent, target_candidate,
                            synthetic_path=synthetic_path, people_csv=people_csv)

                    accept_local_write()
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
            notify_agent()
            self.send_json(payload)

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler


def cmd_serve(args: argparse.Namespace) -> None:
    review_path = Path(args.review)
    verdicts_path = Path(args.verdicts)
    parents_dir = Path(args.parents_dir)
    synthetic_path = Path(args.synthetic_people)
    manifest_path = Path(args.manifest)
    parents = _all_review_parents(
        verdicts_path, review_path, synthetic_path,
        Path(args.facts_dir), Path(args.people_csv),
        Path(args.parents_dir), Path(args.dossier_dir), Path(args.profile_cache_dir))
    progress = review_progress(parents)
    requested_stage = args.stage or "worth"
    query = f"?stage={urllib.parse.quote(requested_stage)}"
    requested_url = f"http://{args.host}:{args.port}/{query}"

    def begin_people_review() -> None:
        write_review_manifest("worth", "awaiting_user", progress, path=manifest_path,
                              review_path=review_path, synthetic_path=synthetic_path,
                              launched=True)
        if progress["worth_pending"] == 0:
            write_review_manifest("worth", "completed", progress, path=manifest_path,
                                  review_path=review_path, synthetic_path=synthetic_path)

    # Reopening a live UI is read-only. Starting a new server begins one fresh
    # People-review revision; later stages are merely direct views into files.
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
        if args.fresh and requested_stage == "worth":
            begin_people_review()
        print(json.dumps({"primitive": "reconcile_review_web", "status": "reused",
                          "url": requested_url, "manifest": str(manifest_path),
                          "stage": requested_stage}, indent=2))
        if args.open:
            webbrowser.open(requested_url)
        return

    if requested_stage == "worth":
        begin_people_review()
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
                                              initial_parents=parents))
    host, port = server.server_address
    url = f"http://{host}:{port}/{query}"
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
        "realize": "bin/deep-context realize",
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
