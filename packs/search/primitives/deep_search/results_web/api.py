"""JSON routes for the saved search catalog and one search result.

`asdict` serializes dataclass fields and tuples as JSON arrays; derived
`SearchResult.queries` and `Candidate.suggested_pin` are not emitted.

Changelog:
  2026-09-26: expose the manifest-only catalog and one saved run as JSON.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler

from packs.search.primitives.shared.human_ratings import LEGACY_SCORES, RUBRIC

from .server import SearchRoutes, _send_json


class SearchApi:
    def __init__(self, routes: SearchRoutes) -> None:
        self.routes = routes

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        path = self.routes._relative(parsed.path)
        if path == "/api/catalog":
            _send_json(handler, {"searches": [asdict(card) for card in self.routes.catalog()]})
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


def search_api(routes: SearchRoutes) -> SearchApi:
    return SearchApi(routes)
