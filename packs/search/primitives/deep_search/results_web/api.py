"""JSON routes for the saved search catalog and one search result.

`asdict` serializes dataclass fields and tuples as JSON arrays; derived
`SearchResult.queries` and `Candidate.suggested_pin` are not emitted.

Changelog:
  2026-10-08: /api/ask/preview, /api/ask/status and the ask send for the broadcast button.
  2026-09-26: expose the manifest-only catalog and one saved run as JSON.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from packs.search.primitives.ask_status import ask_status
from packs.search.primitives.deep_search.results_web import snapshot
from packs.search.primitives.shared.human_ratings import LEGACY_SCORES, RUBRIC
from packs.search.primitives.upload_search_results import upload_search_results as upload

from .server import SearchRoutes, _send_json


class SearchApi:
    def __init__(self, routes: SearchRoutes) -> None:
        self.routes = routes

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
            _send_json(handler, self.ask_preview(run_dir) if path.endswith("preview") else self.ask_status(run_dir))
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

    @property
    def env_file(self) -> Path:
        return self.routes.results_root.parents[1] / ".env"

    def ask_preview(self, run_dir: Path) -> dict:
        """The pinned candidates and who the relay would ask, without sending."""
        rendered = snapshot.export_snapshot(run_dir)
        candidates, skipped = upload.pinned_candidates(rendered)
        preview = {"candidates": [], "operators": []}
        if candidates:
            set_id = upload.pg.fetch_default_set_id(env_file=self.env_file)["set_id"]
            preview = _post_json(upload.auth.api_base(self.env_file), "/v2/asks/preview",
                                 upload.auth.bearer_token(self.env_file),
                                 {"question": "preview", "set_id": set_id, "candidates": candidates})
        return {"pinned": candidates, "skipped": skipped, **preview}

    def ask_status(self, run_dir: Path) -> dict:
        if not (run_dir / "ask.json").is_file():
            return {"ask": None}
        return {"ask": json.loads((run_dir / "ask.json").read_text(encoding="utf-8")),
                "answers": ask_status.run(run_dir, env_file=self.env_file)}

    def ask_send(self, run_dir: Path, question: str) -> dict:
        """Upload the snapshot with the ask; the relay routes it to the owners."""
        result = upload.UploadSearchResults(run_dir, env_file=self.env_file, ask=question).run()
        ask = run_dir / "ask.json"
        return {**result, "ask": json.loads(ask.read_text(encoding="utf-8")) if ask.is_file() else None}


def _post_json(base: str, path: str, token: str, body: dict) -> dict:
    request = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                              "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def search_api(routes: SearchRoutes) -> SearchApi:
    return SearchApi(routes)
