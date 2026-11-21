"""Pin the Searches JSON routes and their TypeScript dataclass fields.

Changelog:
  2026-11-21: /api/search/person: a set run's person from <run>/people/ or one POST /v2/hydrate, a local run's row.
  2026-11-09: the chat card's edits save as <run>/card-edits.json under .powerpacks/search.
  2026-09-26: pin nullability, the group and pin-decision vocabularies, and the payloads.
  2026-09-26: cover catalog-only reads, one run, errors, and client fields.
"""

from __future__ import annotations

import hashlib
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
from packs.search.primitives.deep_search.results_web.snapshot import export_snapshot, validate_snapshot
from packs.shared import powerset_api
from test_deep_search_catalog import CANDIDATE, _write_run

TYPES = Path(__file__).resolve().parents[1] / "web/src/types/searches.ts"
DATACLASSES = {
    name: getattr(model, name) for name in (
        "SearchCard", "SearchResult", "SearchStep", "Pond", "PondCandidate", "TraitScore", "Position",
        "Education", "Candidate", "CandidatePond", "CandidateGroup", "MoveLikelihood",
        "CandidateJudgment", "PinJudgment", "PersonAttribution", "NetworkSource",
        "NetworkOperator", "GmailAccountDetail", "TeamMember", "TeamSimilarity", "PersonProfile",
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


def _get(http_server: ThreadingHTTPServer, path: str) -> tuple[int, dict[str, object]]:
    client, server = socket.socketpair()
    with client, server:
        client.sendall(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
        http_server.RequestHandlerClass(server, ("localhost", 0), http_server)
        server.shutdown(socket.SHUT_WR)
        response = b""
        while chunk := client.recv(65536):
            response += chunk
    head, body = response.split(b"\r\n\r\n", 1)
    return int(head.split(b" ", 2)[1]), json.loads(body)


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
        return _get(self.server, path)

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

    def test_old_run_preserves_json_bytes(self) -> None:
        status, payload = self.get("/searches/api/search.json?run_id=jordan-role")
        self.assertEqual(status, 200)
        self.assertEqual(payload["search"].pop("steps"), [])
        self.assertIsNone(payload["search"].pop("current_step"))
        self.assertEqual((payload["search"].pop("bad_count"), payload["search"].pop("partial")), (0, False))
        # Same fixture serialized by BASE 18f5f39, before step fields were added.
        self.assertEqual(hashlib.sha256(json.dumps(payload).encode()).hexdigest(),
                         "62c21bb4dab3d32640ef0d0605c5937a9754f716e21979aa406fa72a53be7a31")

    def test_step_rows_and_snapshot(self) -> None:
        original = self.root / "original.jsonl"
        refined = self.root / "refined.jsonl"
        original.write_text(json.dumps({"person_id": "jordan", "name": "Jordan Bravo", "final_score": 0.8}) + "\n"
                            + json.dumps({"person_id": "casey", "name": "Casey Example", "final_score": 0.6}) + "\n")
        refined.write_text(json.dumps({"person_id": "casey", "name": "Casey Example", "final_score": 0.9}) + "\n")
        model.record_quick_search(self.root, "quick", query="Engineers", filters={}, traits=[],
                                 created_at="2026-11-09", jsonl=str(original), profiles_path="", status="completed")
        path = self.root / "quick/results.json"
        saved = json.loads(path.read_text())
        saved["steps"].append(dict(id="s2", parent="s1", criteria="only startup people", sql=None, query_type="filter",
                                   shown=1, scored=2, cost_usd=0.01, created_at="2026-11-09", jsonl=str(refined)))
        model.select_quick_step(saved, saved["steps"][1])
        path.write_text(json.dumps(saved))
        before = path.read_bytes()
        status, payload = self.get("/searches/api/search.json?run_id=quick")
        self.assertEqual(status, 200)
        search = payload["search"]
        self.assertEqual([row["person_id"] for row in search["candidates"]], ["casey"])
        self.assertNotIn("criteria", search)
        self.assertEqual(search["current_step"], "s2")
        self.assertEqual(search["steps"], [{key: value for key, value in step.items() if key != "jsonl"}
                                          for step in saved["steps"]])
        validate_snapshot({"schema_version": 1, "search": search, "tags": {"tags": [], "assignments": {}}})
        self.assertEqual(path.read_bytes(), before)
        snapshot = export_snapshot(path.parent)
        self.assertEqual(snapshot["search"]["current_step"], "s2")
        self.assertEqual(len(snapshot["search"]["steps"]), 2)

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


class PersonRouteTest(unittest.TestCase):
    """GET /searches/api/search/person: one person's full profile for the drawer."""

    CONTEXT = {"headline": "Builds payment systems", "summary": "Ten years of payments.",
               "positions": [{"position_title": "Staff Engineer", "company_name": "Example Labs", "is_current": True},
                             {"position_title": "Founder", "company_name": "Example Startup"}],
               "education": [{"school_name": "Example University", "degree": "BS"}]}

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name) / ".powerpacks"
        root = self.data / "deep-search"
        _write_run(root, "jordan-role", title="Backend Engineer", company="Example Labs",
                   created_at="2026-09-26T00:00:00Z", status="completed",
                   found_by=[{"run": "jordan-role", "pond": 1, "query": "Backend Engineer query"}])
        api = search_api(search_routes(root, base="/searches"))

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if not api.get(self, urllib.parse.urlparse(self.path)):
                    _send(self, b"not found", "text/plain", status=404)

            def log_message(self, fmt: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
        self.addCleanup(self.server.server_close)

    def get(self, path: str) -> tuple[int, dict[str, object]]:
        return _get(self.server, path)

    def set_run(self, run_id: str) -> Path:
        run = self.data / "search" / run_id
        run.mkdir(parents=True)
        (run / "pipeline.ledger.json").write_text(json.dumps({"state": "state.json"}))
        return run

    def test_a_set_runs_person_is_fetched_once_then_read_from_the_run(self) -> None:
        run = self.set_run("jordan-role")
        with mock.patch.object(powerset_api, "call", return_value={"profiles": {"p-jordan": self.CONTEXT}}) as call:
            first = self.get("/searches/api/search/person?run_id=jordan-role&person_id=p-jordan")
            again = self.get("/searches/api/search/person?run_id=jordan-role&person_id=p-jordan")
        self.assertEqual(first, again)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[1:], ("/v2/hydrate", {"person_ids": ["p-jordan"]}))
        self.assertEqual(json.loads((run / "people/p-jordan.json").read_text()), self.CONTEXT)
        status, payload = first
        self.assertEqual(status, 200)
        profile = payload["profile"]
        self.assertEqual((profile["headline"], profile["summary"]), ("Builds payment systems", "Ten years of payments."))
        self.assertEqual([(position["title"], position["position_index"]) for position in profile["positions"]],
                         [("Staff Engineer", 0), ("Founder", 1)])
        self.assertEqual(profile["education"][0]["school"], "Example University")
        self.assertEqual(set(_interfaces()["PersonPayload"]), set(payload))

    def test_a_set_runs_failure_is_the_reason(self) -> None:
        self.set_run("jordan-role")
        with mock.patch.object(powerset_api, "call", side_effect=OSError("timed out")):
            status, payload = self.get("/searches/api/search/person?run_id=jordan-role&person_id=p-jordan")
        self.assertEqual((status, payload), (502, {"error": "Couldn't reach Powerset: timed out"}))
        with mock.patch.object(powerset_api, "call", side_effect=SystemExit("not signed in")):
            self.assertEqual(self.get("/searches/api/search/person?run_id=jordan-role&person_id=p-jordan"),
                             (401, {"error": "Sign in to Powerset again"}))
        with mock.patch.object(powerset_api, "call", return_value={"profiles": {}}):
            self.assertEqual(self.get("/searches/api/search/person?run_id=jordan-role&person_id=p-gone"),
                             (404, {"error": "No profile for this person"}))
        self.assertFalse((self.data / "search/jordan-role/people").exists())

    def test_another_runs_person_is_its_stored_row(self) -> None:
        for ledger in (None, {"mode": "local_duckdb"}):
            if ledger is not None:
                self.set_run("jordan-role").joinpath("pipeline.ledger.json").write_text(json.dumps(ledger))
            with self.subTest(ledger=ledger), mock.patch.object(powerset_api, "call") as call:
                status, payload = self.get(f"/searches/api/search/person?run_id=jordan-role&person_id={CANDIDATE}")
                missing = self.get("/searches/api/search/person?run_id=jordan-role&person_id=p-nobody")
                call.assert_not_called()
                self.assertEqual(status, 200)
                self.assertEqual(set(payload["profile"]), {"headline", "summary", "positions", "education"})
                self.assertEqual(missing[0], 404)

    def test_refuses_names_that_leave_the_run(self) -> None:
        self.set_run("jordan-role")
        with mock.patch.object(powerset_api, "call") as call:
            for query in ("run_id=jordan-role&person_id=..", "run_id=jordan-role&person_id=a%2Fb",
                          "run_id=jordan-role", "run_id=..&person_id=p-jordan"):
                self.assertEqual(self.get(f"/searches/api/search/person?{query}")[0], 400, query)
        call.assert_not_called()


class CardEditsTest(unittest.TestCase):
    """POST /searches/api/card-edits writes a chat prepare's edits beside it, nowhere else."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        data = Path(temp.name)
        self.run_dir = data / "search" / "ab12-vp-finance"
        self.run_dir.mkdir(parents=True)
        api = search_api(search_routes(data / "deep-search", base="/searches"))

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if not api.post(self, urllib.parse.urlparse(self.path)):
                    _send(self, b"not found", "text/plain", status=404)

            def log_message(self, fmt: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
        self.addCleanup(self.server.server_close)

    def post(self, request: object) -> int:
        data = json.dumps(request).encode()
        client, server = socket.socketpair()
        with client, server:
            client.sendall(b"POST /searches/api/card-edits HTTP/1.0\r\nHost: localhost\r\n"
                           + f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
            self.server.RequestHandlerClass(server, ("localhost", 0), self.server)
            server.shutdown(socket.SHUT_WR)
            response = b""
            while chunk := client.recv(65536):
                response += chunk
        return int(response.split(b" ", 2)[1])

    def test_saves_the_edits_in_the_run_folder(self) -> None:
        edits = {"filters": {"metro_areas": ["New York"], "company_names": None}}
        self.assertEqual(self.post({"run": "ab12-vp-finance", "edits": edits}), 200)
        self.assertEqual(json.loads((self.run_dir / "card-edits.json").read_text()), edits)

    def test_refuses_other_folders(self) -> None:
        for run in ("", "missing", "../search", "ab12-vp-finance/../..", "."):
            self.assertEqual(self.post({"run": run, "edits": {"filters": {}}}), 400, run)
        self.assertEqual(self.post({"run": "ab12-vp-finance", "edits": "nope"}), 400)
        self.assertFalse((self.run_dir / "card-edits.json").exists())
