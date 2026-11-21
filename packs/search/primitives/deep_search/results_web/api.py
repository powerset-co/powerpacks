"""JSON routes for the saved search catalog and one search result.

`asdict` serializes dataclass fields and tuples as JSON arrays; derived
`SearchResult.queries` and `Candidate.suggested_pin` are not emitted.

Changelog:
  2026-11-21: /api/search/person: one person's full profile for the drawer; a set run's from <run>/people/, else
      POST /v2/hydrate (saved there), another run's from its stored row.
  2026-11-17: /api/bad.json: the people a Powerset-ranked search turned down, "loading" while they download, "failed" when that broke.
  2026-11-16: the ask routes are gone with local sets (Ask your set, Broadcast).
  2026-11-16: /api/search.json no longer takes step=; the page only ever read the current step.
  2026-11-09: /card-edits saves a chat search card's edits beside its prepare, so the chat shows none of the JSON.
  2026-10-08: the ask routes work out who to ask from the local sets and send agent messages (ask_set.py).
  2026-10-08: /api/ask/preview, /api/ask/status and the ask send for the broadcast button.
  2026-09-26: expose the manifest-only catalog and one saved run as JSON.
"""

from __future__ import annotations

import http.client
import json
import urllib.parse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from packs.search.primitives.hydrate_people.hydrate_people import fetch_api_person_rows
from packs.search.primitives.shared.human_ratings import LEGACY_SCORES, RUBRIC

from .model import PersonProfile, load_bad, person_profile, set_run
from .server import LOCAL_HOSTS, SearchRoutes, _send_json


class SearchApi:
    def __init__(self, routes: SearchRoutes) -> None:
        self.routes = routes

    def get(self, handler: BaseHTTPRequestHandler, parsed: urllib.parse.ParseResult) -> bool:
        path = self.routes._relative(parsed.path)
        if path == "/api/catalog":
            _send_json(handler, {"searches": [asdict(card) for card in self.routes.catalog()]})
            return True
        if path not in ("/api/search.json", "/api/bad.json", "/api/search/person"):
            return False

        query = urllib.parse.parse_qs(parsed.query)
        run_id = (query.get("run_id") or [""])[0]
        if not _file_name(run_id):
            _send_json(handler, {"error": "run_id is required"}, status=HTTPStatus.BAD_REQUEST)
            return True
        if path == "/api/search/person":
            self._person(handler, run_id, (query.get("person_id") or [""])[0])
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

    def _person(self, handler: BaseHTTPRequestHandler, run_id: str, person_id: str) -> None:
        if not _file_name(person_id):  # it names the person's file under <run>/people/
            _send_json(handler, {"error": "person_id is required"}, status=HTTPStatus.BAD_REQUEST)
            return
        root = self.routes.results_root
        if set_run(root, run_id):
            try:
                rows = fetch_api_person_rows([person_id], root.parent.parent / ".env",
                                             cache_dir=root.parent / "search" / run_id / "people")
            except SystemExit:  # signed out
                _send_json(handler, {"error": "Sign in to Powerset again"}, status=HTTPStatus.UNAUTHORIZED)
                return
            except (OSError, http.client.HTTPException) as error:
                _send_json(handler, {"error": f"Couldn't reach Powerset: {error}"}, status=HTTPStatus.BAD_GATEWAY)
                return
            profile = person_profile(rows[0]) if rows else None
        else:  # its rows are already the whole profile
            search = self.routes.one(run_id)
            ponds = search.ponds if search else ()
            row = next((row for pond in ponds for row in pond.candidates if row.person_id == person_id), None)
            profile = PersonProfile(row.headline, row.summary, row.positions, row.education) if row else None
        if profile is None:
            _send_json(handler, {"error": "No profile for this person"}, status=HTTPStatus.NOT_FOUND)
            return
        _send_json(handler, {"profile": asdict(profile)})

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


def _file_name(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and value not in {".", ".."}


def search_api(routes: SearchRoutes) -> SearchApi:
    return SearchApi(routes)
