"""JSON routes for the saved search catalog and one search result.

`asdict` serializes dataclass fields and tuples as JSON arrays; derived
`SearchResult.queries` and `Candidate.suggested_pin` are not emitted.

Changelog:
  2026-10-08: the ask routes work out who to ask from the local sets and send agent messages (ask_set.py).
  2026-10-08: /api/ask/preview, /api/ask/status and the ask send for the broadcast button.
  2026-09-26: expose the manifest-only catalog and one saved run as JSON.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Callable

from packs.ingestion.primitives.share.web.sets import CloudError, NeedsSignIn, Sets
from packs.powerset.primitives.agent_inbox.messages import Rejected
from packs.search.primitives.ask_set import ask_set
from packs.search.primitives.shared.human_ratings import LEGACY_SCORES, RUBRIC

from .server import LOCAL_HOSTS, SearchRoutes, _send_json


class SearchApi:
    def __init__(self, routes: SearchRoutes, sets: Sets, store_lock: threading.Lock) -> None:
        self.routes = routes
        self.sets = sets
        self.store_lock = store_lock  # the sets read the one store connection the People routes use

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        path = self.routes._relative(parsed.path)
        if path == "/api/catalog":
            _send_json(handler, {"searches": [asdict(card) for card in self.routes.catalog()]})
            return True
        if path in {"/api/ask/preview", "/api/ask/status"}:
            run_id = (urllib.parse.parse_qs(parsed.query).get("run_id") or [""])[0]
            run_dir = self.routes.results_root / run_id
            if not run_id or not (run_dir / "results.json").is_file():
                _send_json(handler, {"error": f"unknown search: {run_id}"}, status=HTTPStatus.NOT_FOUND)
                return True
            self._ask(handler, lambda: ask_set.preview(run_dir, self.sets) if path.endswith("preview")
                      else ask_set.status(run_dir, self.sets))
            return True
        if path != "/api/search.json":
            return False

        run_id = (urllib.parse.parse_qs(parsed.query).get("run_id") or [""])[0]
        if not run_id:
            _send_json(handler, {"error": "run_id is required"}, status=HTTPStatus.BAD_REQUEST)
            return True
        search = self.routes.one(run_id)
        if search is None:
            _send_json(handler, {"error": f"unknown search: {run_id}"}, status=HTTPStatus.NOT_FOUND)
            return True
        _send_json(handler, {"search": asdict(search), "ratings": {"rubric": RUBRIC, "legacy": LEGACY_SCORES}})
        return True

    def post(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        if self.routes._relative(parsed.path) != "/ask":
            return False
        origin = (handler.headers.get("Origin") or "").strip()
        if origin and (urllib.parse.urlparse(origin).hostname or "").lower() not in LOCAL_HOSTS:
            _send_json(handler, {"error": "cross-origin request rejected"}, status=HTTPStatus.FORBIDDEN)
            return True
        length = int(handler.headers.get("Content-Length", "0"))
        request = json.loads(handler.rfile.read(length).decode("utf-8")) if length > 0 else {}
        run_id = str(request.get("run_id") or "")
        question = str(request.get("question") or "").strip()
        run_dir = self.routes.results_root / run_id
        if not run_id or not (run_dir / "results.json").is_file() or not question:
            _send_json(handler, {"error": "run_id and question are required"}, status=HTTPStatus.BAD_REQUEST)
            return True
        self._ask(handler, lambda: ask_set.send(run_dir, question, self.sets))
        return True

    def _ask(self, handler: BaseHTTPRequestHandler, call: Callable[[], dict]) -> None:
        """One ask call under the store lock; a missing sign-in is 401 needs_auth, a relay refusal 502."""
        try:
            with self.store_lock:
                answer = call()
        except NeedsSignIn as error:
            _send_json(handler, {"status": "needs_auth", "error": str(error)}, status=HTTPStatus.UNAUTHORIZED)
            return
        except CloudError as error:
            _send_json(handler, {"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return
        except Rejected as error:
            _send_json(handler, {"error": f"The ask is too big to send: {error}"}, status=HTTPStatus.BAD_REQUEST)
            return
        _send_json(handler, answer)


def search_api(routes: SearchRoutes, sets: Sets, store_lock: threading.Lock) -> SearchApi:
    return SearchApi(routes, sets, store_lock)
