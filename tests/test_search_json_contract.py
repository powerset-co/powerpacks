"""Pin the Searches JSON routes and their TypeScript dataclass fields.

Changelog:
  2026-09-26: cover catalog-only reads, one run, errors, and client fields.
"""

from __future__ import annotations

import json
import re
import socket
import tempfile
import unittest
import urllib.parse
from dataclasses import fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from packs.search.primitives.deep_search.results_web import model
from packs.search.primitives.deep_search.results_web.api import search_api
from packs.search.primitives.deep_search.results_web.server import _send, search_routes
from test_deep_search_catalog import _write_run

TYPES = Path(__file__).resolve().parents[1] / "web/src/types/searches.ts"
DATACLASSES = {
    name: getattr(model, name) for name in (
        "SearchCard", "SearchResult", "Pond", "PondCandidate", "TraitScore", "Position",
        "Education", "Candidate", "CandidatePond", "CandidateGroup", "MoveLikelihood",
        "CandidateJudgment", "PinJudgment", "PersonAttribution", "NetworkSource",
        "NetworkOperator", "GmailAccountDetail", "TeamMember", "TeamSimilarity",
    )
}


class SearchJsonContractTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        run = _write_run(self.root, "jordan-role", title="Backend Engineer", company="Example Labs",
                         created_at="2026-09-26T00:00:00Z", status="completed",
                         found_by=[{"run": "jordan-role", "pond": 1, "query": "Backend Engineer query"}])
        model.index_search(self.root, "jordan-role", json.loads((run / "manifest.json").read_text()))
        api = search_api(search_routes(self.root, base="/searches"))

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if not api.get(self, urllib.parse.urlparse(self.path)):
                    _send(self, b"not found", "text/plain", status=404)

            def log_message(self, fmt: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
        self.addCleanup(self.server.server_close)
        self.api = api

    def get(self, path: str) -> tuple[int, dict[str, object]]:
        client, server = socket.socketpair()
        with client, server:
            client.sendall(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
            self.server.RequestHandlerClass(server, ("localhost", 0), self.server)
            server.shutdown(socket.SHUT_WR)
            response = b""
            while chunk := client.recv(65536):
                response += chunk
        head, body = response.split(b"\r\n\r\n", 1)
        return int(head.split(b" ", 2)[1]), json.loads(body)

    def test_catalog_reads_only_the_index(self) -> None:
        with mock.patch.object(model, "_payload", wraps=model._payload) as reads:
            status, payload = self.get("/searches/api/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"searches"})
        self.assertEqual(payload["searches"][0]["run_id"], "jordan-role")
        self.assertEqual([call.args[0].name for call in reads.call_args_list], ["catalog.json"])

    def test_one_search_and_errors(self) -> None:
        status, payload = self.get("/searches/api/search.json?run_id=jordan-role")
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"search", "ratings"})
        self.assertEqual(set(payload["ratings"]), {"rubric", "legacy"})
        self.assertEqual(payload["search"]["run_id"], "jordan-role")
        self.assertNotIn("queries", payload["search"])
        self.assertEqual(self.get("/searches/api/search.json?run_id=missing"),
                         (404, {"error": "unknown search: missing"}))
        self.assertEqual(self.get("/searches/api/search.json"),
                         (400, {"error": "run_id is required"}))
        self.assertFalse(self.api.get(mock.Mock(), urllib.parse.urlparse("/searches/tags")))
        self.assertFalse(self.api.get(mock.Mock(), urllib.parse.urlparse("/other/api/catalog")))

    def test_client_interfaces_match_dataclasses(self) -> None:
        source = TYPES.read_text(encoding="utf-8")
        blocks = dict(re.findall(r"export interface (\w+) \{([^}]*)\}", source, re.S))
        for name, cls in DATACLASSES.items():
            with self.subTest(interface=name):
                self.assertIn(name, blocks)
                properties = set(re.findall(r"^  ([a-z_][a-z_0-9]*):", blocks[name], re.M))
                self.assertEqual(properties, {field.name for field in fields(cls)})


if __name__ == "__main__":
    unittest.main()
