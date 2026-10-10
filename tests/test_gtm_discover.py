"""GTM's CLI preserves full API evidence and reports bounded source coverage."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "packs/gtm/primitives/discover/discover.py"


def ranked_candidate() -> dict:
    return {"fit": {"candidate": {"profile": {
        "person_id": "synthetic-jordan", "name": "Jordan Bravo",
        "summary": "Full profile evidence survives the minimal dataclass projection.",
        "positions": [{"position_title": "Marketing Director", "company_name": "ExampleCo",
                       "company_id": "exampleco", "is_current": True, "company_headcount": 120}],
    }, "sources": ["network"], "evidence": [{"source": "network", "reference": "synthetic-role"}]},
        "status": "qualified", "matched_position_index": 0}, "paths": []}


class GtmCliTests(unittest.TestCase):
    def _run(self, request: dict, response: object, *, operation: str = "discover") -> tuple:
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers["Authorization"],
                                 json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                credentials = base / "credentials.json"
                credentials.write_text(json.dumps({"access_token": "synthetic-token", "expires_at": time.time() + 3600}))
                env_file = base / ".env"
                env_file.write_text(f"POWERSET_API_URL=http://127.0.0.1:{server.server_port}\n"
                                    f"POWERPACKS_CREDENTIALS_PATH={credentials}\n")
                request_file = base / "request.json"
                request_file.write_text(json.dumps(request))
                output = base / "output"
                proc = subprocess.run([sys.executable, str(CLI), operation, "--request", str(request_file),
                                       "--env-file", str(env_file), "--output-dir", str(output)],
                                      cwd=base, capture_output=True, text=True,
                                      env={key: value for key, value in os.environ.items()
                                           if key not in {"POWERSET_API_URL", "POWERPACKS_CREDENTIALS_PATH"}})
                files = {path.name: path.read_text() for path in output.glob("*")}
                return proc, received, files
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_actual_cli_keeps_request_filters_and_full_response(self):
        request = {"query": "current marketing leaders at ExampleCo", "set_id": "synthetic-set",
                   "sources": ["network", "company"], "filters": {"company_ids": ["exampleco"],
                   "role_function": "marketing", "is_current": True},
                   "allow_provider_calls": False, "max_provider_calls": 0}
        response = {"query": request["query"], "filters": request["filters"], "sources": request["sources"],
                    "candidates": [ranked_candidate()], "coverage": [
                        {"source": "network", "loaded": 1, "total": 1, "status": "complete"},
                        {"source": "company", "loaded": 0, "status": "unavailable", "error": "fixture account unavailable"},
                        {"source": "sales_nav", "loaded": 0, "queries": [
                            {"filters": ["(type:FUNCTION,values:List((id:13,selectionType:INCLUDED)))"],
                             "start": 25, "count": 25, "keywords": "marketing"}]}],
                    "stage_timings": [{"stage": "discover", "elapsed_ms": 12}]}
        proc, received, files = self._run(request, response)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(received, [("/v2/gtm/discover", "Bearer synthetic-token", request)])
        self.assertEqual(json.loads(files["response.json"]), response)
        self.assertEqual(json.loads(files["candidates.jsonl"]), ranked_candidate())
        manifest = json.loads(proc.stdout)
        self.assertEqual((manifest["status"], manifest["count"], manifest["qualified"]), ("partial", 1, 1))
        self.assertEqual(manifest["coverage"][1]["error"], "fixture account unavailable")
        self.assertEqual(manifest["filters"]["company_ids"], ["exampleco"])
        self.assertEqual(manifest["stage_timings"], response["stage_timings"])
        self.assertEqual(manifest["coverage"][2]["queries"], response["coverage"][2]["queries"])

    def test_missing_current_role_evidence_stays_unknown(self):
        candidate = ranked_candidate()
        candidate["fit"]["status"] = "unknown"
        candidate["fit"]["candidate"]["profile"]["positions"][0]["is_current"] = None
        response = {"query": "current marketers", "filters": {"is_current": True},
                    "sources": ["network"], "candidates": [candidate], "coverage": []}
        proc, _received, files = self._run({"sources": ["network"]}, response)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["qualified"], 0)
        saved = json.loads(files["candidates.jsonl"])
        self.assertIsNone(saved["fit"]["candidate"]["profile"]["positions"][0]["is_current"])

    def test_invalid_predicate_does_not_authenticate_or_post(self):
        proc, received, files = self._run({"filters": {"invented_predicate": "litigation"}}, {})
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(received, [])
        self.assertEqual(files, {})
        self.assertIn("invented_predicate", json.loads(proc.stdout)["error"])

    def test_sort_transmits_exact_full_selection(self):
        candidate = ranked_candidate()
        request = {"candidates": [candidate], "sort": "company_headcount", "descending": True}
        proc, received, files = self._run(request, [candidate], operation="sort")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(received[0][0], "/v2/gtm/sort")
        self.assertEqual(received[0][2], request)
        self.assertEqual(json.loads(files["response.json"]), [candidate])
        self.assertEqual(json.loads(proc.stdout)["count"], 1)


if __name__ == "__main__":
    unittest.main()
