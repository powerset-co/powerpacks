"""Pin the Searches JSON routes and their TypeScript dataclass fields.

Changelog:
  2026-09-26: pin nullability, the group and pin-decision vocabularies, and the payloads.
  2026-09-26: cover catalog-only reads, one run, errors, and client fields.
"""

from __future__ import annotations

import inspect
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
from packs.search.primitives.deep_search.results_web import server as results_server
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


def _interfaces() -> dict[str, dict[str, str]]:
    """Each `export interface` in types/searches.ts: field name -> its TypeScript type."""
    source = TYPES.read_text(encoding="utf-8")
    blocks = dict(re.findall(r"export interface (\w+) \{([^}]*)\}", source, re.S))
    return {name: dict(re.findall(r"^  ([a-z_][a-z_0-9]*): (.*)$", body, re.M)) for name, body in blocks.items()}


def _union(name: str) -> set[str]:
    """The string literals of `export type <name> = "a" | "b"` in types/searches.ts."""
    source = TYPES.read_text(encoding="utf-8")
    union = re.search(rf"^export type {name} =(.*?)$", source, re.M)
    assert union is not None, name
    return set(re.findall(r'"(\w+)"', union.group(1)))


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
        interfaces = _interfaces()
        for name, cls in DATACLASSES.items():
            with self.subTest(interface=name):
                self.assertIn(name, interfaces)
                self.assertEqual(set(interfaces[name]), {field.name for field in fields(cls)})

    def test_nullable_fields_match(self) -> None:
        # A field the server can send as null is `| null` in the client, and only such a field.
        interfaces = _interfaces()
        for name, cls in DATACLASSES.items():
            for field in fields(cls):
                with self.subTest(field=f"{name}.{field.name}"):
                    self.assertEqual("| null" in interfaces[name][field.name], "None" in str(field.type))

    def test_closed_vocabularies_match(self) -> None:
        self.assertEqual(_union("SearchGroupKey"), {key for key, _label in model.GROUPS})
        decisions = re.search(r"not in \(None, (.*?)\):", inspect.getsource(model._pin_judgment))
        self.assertIsNotNone(decisions)
        self.assertEqual(_union("PinDecision"), set(re.findall(r'"(\w+)"', decisions.group(1))))
        feedback = inspect.getsource(results_server.SearchRoutes._save_feedback)
        self.assertEqual({"submitted", "saved_locally"} & set(re.findall(r'"(\w+)"', feedback)),
                         {"submitted", "saved_locally"})

    def test_payload_interfaces_match_the_routes(self) -> None:
        interfaces = _interfaces()
        _status, catalog = self.get("/searches/api/catalog")
        self.assertEqual(set(interfaces["CatalogPayload"]), set(catalog))
        _status, run = self.get("/searches/api/search.json?run_id=jordan-role")
        self.assertEqual(set(interfaces["SearchRunPayload"]), set(run))
        self.assertEqual(set(interfaces["Ratings"]), set(run["ratings"]))
        tags = inspect.getsource(results_server.SearchRoutes.get)
        self.assertIn('{"tagged": tagged}', tags)
        self.assertEqual(set(interfaces["TagsPayload"]), {"tagged"})


if __name__ == "__main__":
    unittest.main()
