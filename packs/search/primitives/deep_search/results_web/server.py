"""Serve saved deep-search results and persist human feedback.

Flow: `SearchRoutes` answers the list (`/`, manifests only), one run
(`/run?run_id=`, loaded on first open and cached until its results or labels
change), `/api/search`, `/tags` and `/feedback`. The deep-context review
server mounts them under `/searches` (`bin/deep-context review searches`),
with or without a deep-context store; `make_handler` serves them alone for
tests.

Changelog:
  2026-09-26: routes became mountable under a base path; the list page reads
      the catalog; runs load one at a time; the standalone `main()` went, the
      review server is the one local server.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from packs.indexing.lib.io import write_json
from packs.powerset.primitives.send_feedback.send_feedback import FeedbackRequest

from . import RESULTS_CSS, RESULTS_JS
from .feedback import ENV_FILE, build_feedback_request, record_fit_label, submit_results_feedback
from ..search_harness import ROOT, backfill_manifests
from .model import FIT_LABELS_FILE, SearchCard, SearchResult, load_catalog, load_search
from .rendering import render_catalog, render_page, render_search_body

FeedbackSender = Callable[[FeedbackRequest], dict[str, object]]
Catalog = Callable[[], tuple[SearchCard, ...]]
LoadSearch = Callable[[str], SearchResult | None]
MAX_TAGS_REQUEST_BYTES = 1024 * 1024
DEFAULT_DEEP_SEARCH_ROOT = ROOT / ".powerpacks" / "deep-search"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _validate_tagged(tagged: Any) -> dict[str, Any]:
    if not isinstance(tagged, dict) or set(tagged) != {"tags", "assignments"}:
        raise ValueError("tags and assignments are required")
    tags, assignments = tagged["tags"], tagged["assignments"]
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("tags must be a list of strings")
    if not isinstance(assignments, dict):
        raise ValueError("assignments must be an object")
    known_tags = set(tags)
    if any(not isinstance(values, list)
           or not all(isinstance(tag, str) and tag in known_tags for tag in values)
           for values in assignments.values()):
        raise ValueError("assignments must contain lists of declared tags")
    return tagged


def _read_tagged(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return _validate_tagged(json.loads(text))


def _login() -> int:
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE)
    from packs.powerset.primitives.auth.auth import main

    return main(["login"])


def _send(handler: BaseHTTPRequestHandler, body: bytes, content_type: str = "text/html; charset=utf-8",
          status: int = 200, *, cache: str = "no-store") -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", cache)
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def _send_json(handler: BaseHTTPRequestHandler, payload: dict[str, object], status: int = 200) -> None:
    _send(handler, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status=status)


class SearchRoutes:
    """The viewer's routes, answered relative to `base` ("" alone, "/searches" mounted).

    `load` returns every search in scope; `catalog` and `load_one`, when given,
    let the list page skip the bodies and a run page open just its own run."""

    def __init__(self, results_root: Path, load: Callable[[], tuple[SearchResult, ...]],
                 feedback_sender: FeedbackSender = submit_results_feedback, *,
                 catalog: Catalog | None = None, load_one: LoadSearch | None = None,
                 base: str = "") -> None:
        self.results_root = results_root
        self.load = load
        self.feedback_sender = feedback_sender
        self.catalog = catalog
        self.load_one = load_one
        self.base = base.rstrip("/")

    def one(self, run_id: str) -> SearchResult | None:
        if self.load_one is not None:
            return self.load_one(run_id)
        return next((search for search in self.load() if search.run_id == run_id), None)

    def count(self) -> int:
        return len(self.catalog()) if self.catalog is not None else len(self.load())

    def _relative(self, path: str) -> str | None:
        """The route path under `base`, or None when the request is not ours."""
        if not self.base:
            return path
        if path == self.base:
            return "/"
        return path[len(self.base):] if path.startswith(self.base + "/") else None

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        path = self._relative(parsed.path)
        if path is None:
            return False
        query = urllib.parse.parse_qs(parsed.query)
        run_id = (query.get("run_id") or [""])[0]
        if path == "/assets/results.css":
            _send(handler, RESULTS_CSS.read_bytes(), "text/css; charset=utf-8", cache="no-cache")
        elif path == "/assets/results.js":
            _send(handler, RESULTS_JS.read_bytes(), "text/javascript; charset=utf-8", cache="no-cache")
        elif path == "/tags":
            if self.one(run_id) is None:
                _send(handler, b"search not found", "text/plain", status=404)
                return True
            try:
                tagged = _read_tagged(self.results_root / run_id / "tags.json")
            except (OSError, ValueError) as exc:
                _send_json(handler, {"ok": False, "error": str(exc)}, status=500)
                return True
            _send_json(handler, {"tagged": tagged})
        elif path == "/api/search":
            search = self.one(run_id)
            if search is None:
                _send(handler, b"search not found", "text/plain", status=404)
                return True
            _send(handler, render_search_body(search).encode("utf-8"))
        elif path in {"/", "/run"}:
            run_id = run_id or Path((query.get("run_dir") or [""])[0]).name
            if run_id:
                search = self.one(run_id)
                if search is None:
                    _send(handler, b"search not found", "text/plain", status=404)
                    return True
                _send(handler, render_page((search,), base=self.base).encode("utf-8"))
            elif self.catalog is not None:
                _send(handler, render_catalog(self.catalog(), base=self.base).encode("utf-8"))
            else:
                _send(handler, render_page(self.load(), base=self.base).encode("utf-8"))
        else:
            return False
        return True

    def post(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        path = self._relative(parsed.path)
        if path not in {"/feedback", "/auth/login", "/tags"}:
            return False
        origin = (handler.headers.get("Origin") or "").strip()
        if origin and (urllib.parse.urlparse(origin).hostname or "").lower() not in LOCAL_HOSTS:
            _send(handler, b"cross-origin request rejected", "text/plain", status=403)
            return True
        if path == "/auth/login":
            try:
                code = _login()
            except (OSError, SystemExit, ValueError):
                code = 1
            if code == 0:
                _send_json(handler, {"ok": True, "status": "authenticated"})
            else:
                _send_json(handler, {"status": "needs_auth", "error": "Sign-in did not complete. Try again."},
                           status=HTTPStatus.UNAUTHORIZED)
            return True
        if path == "/tags":
            self._save_tags(handler)
            return True
        self._save_feedback(handler)
        return True

    def _save_tags(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            length = int(handler.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("A non-empty request body is required")
        except ValueError as exc:
            _send_json(handler, {"ok": False, "error": str(exc)}, status=400)
            return
        if length > MAX_TAGS_REQUEST_BYTES:
            _send_json(handler, {"ok": False, "error": "Tags request exceeds 1 MiB"}, status=413)
            return
        try:
            body = handler.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete tags request body")
            form = urllib.parse.parse_qs(body.decode("utf-8"), strict_parsing=True, errors="strict")
            run_id = (form.get("run_id") or [""])[0]
            tagged = _validate_tagged(json.loads((form.get("tagged") or [""])[0]))
        except ValueError as exc:
            _send_json(handler, {"ok": False, "error": str(exc)}, status=400)
            return
        search = self.one(run_id)
        if search is None:
            _send(handler, b"search not found", "text/plain", status=404)
            return
        candidate_ids = {candidate.person_id for candidate in search.candidates}
        if not set(tagged["assignments"]).issubset(candidate_ids):
            _send_json(handler, {"ok": False, "error": "candidate not found"}, status=404)
            return
        tags_path = self.results_root / run_id / "tags.json"
        try:
            _read_tagged(tags_path)
            write_json(tags_path, tagged)
        except (OSError, ValueError) as exc:
            _send_json(handler, {"ok": False, "error": str(exc)}, status=500)
            return
        _send_json(handler, {"ok": True})

    def _save_feedback(self, handler: BaseHTTPRequestHandler) -> None:
        length = min(int(handler.headers.get("Content-Length", "0")), 32_768)
        form = urllib.parse.parse_qs(handler.rfile.read(length).decode("utf-8"))
        comment = (form.get("comment") or [""])[0].strip()
        run_id = (form.get("run_id") or [""])[0].strip()
        person_id = (form.get("person_id") or [""])[0].strip()
        raw_judgment = (form.get("human_judgment") or [""])[0].strip()
        if len(comment) > 4000 or (not comment and not raw_judgment):
            _send(handler, b"comment or fit review required", "text/plain", status=400)
            return
        search = self.one(run_id)
        if search is None:
            _send(handler, b"search not found", "text/plain", status=404)
            return
        candidate = search.candidate(person_id) if person_id else None
        if person_id and candidate is None:
            _send(handler, b"candidate not found", "text/plain", status=404)
            return
        if raw_judgment and candidate is None:
            _send(handler, b"candidate required for a score", "text/plain", status=400)
            return
        try:
            human_judgment = json.loads(raw_judgment) if raw_judgment else None
            request = build_feedback_request(search, comment, candidate, human_judgment=human_judgment)
            record_fit_label(self.results_root / run_id, request)
        except (json.JSONDecodeError, ValueError) as exc:
            _send_json(handler, {"status": "failed", "error": str(exc)}, status=400)
            return
        try:
            payload = self.feedback_sender(request)
        except (OSError, SystemExit, ValueError) as exc:
            payload = {"status": "failed", "error": str(exc)}
        if payload.get("status") != "submitted":
            _send_json(handler, {"ok": True, "status": "saved_locally", "api": payload})
            return
        _send_json(handler, {"ok": True, **payload})


def make_handler(results_root: Path, load: Callable[[], tuple[SearchResult, ...]],
                 feedback_sender: FeedbackSender = submit_results_feedback, *,
                 catalog: Catalog | None = None, load_one: LoadSearch | None = None,
                 base: str = "") -> type[BaseHTTPRequestHandler]:
    """A handler serving only the viewer routes (under `base`), plus `/healthz`."""
    routes = SearchRoutes(results_root, load, feedback_sender, catalog=catalog, load_one=load_one, base=base)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/healthz":
                _send_json(self, {"primitive": "deep_search_results_web", "ok": True, "searches": routes.count()})
            elif not routes.get(self, parsed):
                _send(self, b"not found", "text/plain", status=404)

        def do_POST(self) -> None:  # noqa: N802
            if not routes.post(self, urllib.parse.urlparse(self.path)):
                _send(self, b"not found", "text/plain", status=404)

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    return Handler


def _run_loader(root: Path) -> LoadSearch:
    """One search per run, re-read only when its results or labels change."""
    cache: dict[str, tuple[tuple[float, ...], SearchResult | None]] = {}

    def stamp(run_id: str) -> tuple[float, ...]:
        return tuple(path.stat().st_mtime if path.exists() else 0.0
                     for path in (root / run_id / "results.json", root / run_id / FIT_LABELS_FILE))

    def load_one(run_id: str) -> SearchResult | None:
        if "/" in run_id or "\\" in run_id or run_id in {"", ".", ".."}:
            return None
        current = stamp(run_id)
        cached = cache.get(run_id)
        if cached is None or cached[0] != current:
            cached = (current, load_search(root, run_id))
            cache[run_id] = cached
        return cached[1]

    return load_one


def search_routes(root: Path, *, base: str = "", run_id: str | None = None) -> SearchRoutes:
    """The routes over one results root. Older manifests get their display
    cells on the first list request (kept as .bkup), so the review server can
    mount the searches without touching the tree before anyone opens them."""
    load_one = _run_loader(root)
    backfilled = False

    def catalog() -> tuple[SearchCard, ...]:
        nonlocal backfilled
        if not backfilled:
            for written in backfill_manifests(root):
                print(f"[results-web] manifest display cells written: {written}", file=sys.stderr)
            backfilled = True
        cards = load_catalog(root)
        return tuple(card for card in cards if card.run_id == run_id) if run_id else cards

    def load() -> tuple[SearchResult, ...]:
        return tuple(search for card in catalog() if (search := load_one(card.run_id)) is not None)

    return SearchRoutes(root, load, catalog=catalog, load_one=load_one, base=base)

