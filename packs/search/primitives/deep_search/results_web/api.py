"""JSON routes for the saved search catalog and one search result.

`asdict` serializes dataclass fields and tuples as JSON arrays; derived
`SearchResult.queries` and `Candidate.suggested_pin` are not emitted.

Changelog:
  2026-11-17: /api/bad.json: the people a Powerset-ranked search turned down, "loading" while they download, "failed" when that broke.
  2026-11-16: the ask routes are gone with local sets (Ask your set, Broadcast).
  2026-11-16: /api/search.json no longer takes step=; the page only ever read the current step.
  2026-11-09: /card-edits saves a chat search card's edits beside its prepare, so the chat shows none of the JSON.
  2026-10-08: the ask routes work out who to ask from the local sets and send agent messages (ask_set.py).
  2026-10-08: /api/ask/preview, /api/ask/status and the ask send for the broadcast button.
  2026-09-26: expose the manifest-only catalog and one saved run as JSON.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from packs.search.primitives.shared.human_ratings import LEGACY_SCORES, RUBRIC

from .model import load_bad
from .server import LOCAL_HOSTS, SearchRoutes, _send_json


class SearchApi:
    def __init__(self, routes: SearchRoutes) -> None:
        self.routes = routes

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        path = self.routes._relative(parsed.path)
        if path == "/api/catalog":
            _send_json(handler, {"searches": [asdict(card) for card in self.routes.catalog()]})
            return True
        if path not in ("/api/search.json", "/api/bad.json"):
            return False

        run_id = (urllib.parse.parse_qs(parsed.query).get("run_id") or [""])[0]
        if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            _send_json(handler, {"error": "run_id is required"}, status=HTTPStatus.BAD_REQUEST)
            return True
        if path == "/api/bad.json":
            rows = load_bad(self.routes.results_root, run_id)
            _send_json(handler, {"status": "loading", "candidates": []} if rows is None
                       else {"status": "failed", "candidates": [], "error": rows} if isinstance(rows, str)
                       else {"status": "ready", "candidates": [asdict(row) for row in rows]})
            return True
        search = self.routes.one(run_id)
        if search is None:
            _send_json(handler, {"error": f"unknown search: {run_id}"}, status=HTTPStatus.NOT_FOUND)
            return True
        _send_json(handler, {"search": asdict(search), "ratings": {"rubric": RUBRIC, "legacy": LEGACY_SCORES}})
        return True

    def post(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        path = self.routes._relative(parsed.path)
        if path != "/api/card-edits":
            return False
        origin = (handler.headers.get("Origin") or "").strip()
        if origin and (urllib.parse.urlparse(origin).hostname or "").lower() not in LOCAL_HOSTS:
            _send_json(handler, {"error": "cross-origin request rejected"}, status=HTTPStatus.FORBIDDEN)
            return True
        length = int(handler.headers.get("Content-Length", "0"))
        request = json.loads(handler.rfile.read(length).decode("utf-8")) if length > 0 else {}
        self._card_edits(handler, request)
        return True

    def _card_edits(self, handler: BaseHTTPRequestHandler, request: dict) -> None:
        """The card's edits as `<run>/card-edits.json` in a chat prepare's folder (.powerpacks/search/<run>)."""
        run = str(request.get("run") or "")
        edits = request.get("edits")
        run_dir = self.routes.results_root.parent / "search" / run
        if not run or run != Path(run).name or run.startswith(".") or not run_dir.is_dir() or not isinstance(edits, dict):
            _send_json(handler, {"error": "run and edits are required"}, status=HTTPStatus.BAD_REQUEST)
            return
        (run_dir / "card-edits.json").write_text(json.dumps(edits, indent=1) + "\n", encoding="utf-8")
        _send_json(handler, {"ok": True})


def search_api(routes: SearchRoutes) -> SearchApi:
    return SearchApi(routes)
