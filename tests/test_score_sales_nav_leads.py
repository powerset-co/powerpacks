import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from packs.shared.csv_io import CsvIO


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVE = ROOT / "packs/sales-nav/primitives/score_sales_nav_leads/score_sales_nav_leads.py"


def _make_handler(state: dict[str, Any]):
    class MockOpenAI(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
            body = json.loads(raw)
            prompt = body["messages"][1]["content"]
            lead_section = prompt.split("Lead JSON:", 1)[-1].lower()
            score = 0.92 if "real estate" in lead_section or "brookfield" in lead_section else 0.25
            payload = json.dumps({
                "id": "mock",
                "choices": [{"message": {"content": json.dumps({
                    "score": score,
                    "verdict": "include" if score >= 0.7 else "exclude",
                    "reason": "mock reason",
                    "confidence": 0.9,
                    "matched_traits": ["real estate"] if score >= 0.7 else [],
                })}}],
            }).encode()
            with state["lock"]:
                state["calls"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    return MockOpenAI


class MockServer:
    def __init__(self):
        self.state = {"lock": threading.Lock(), "calls": 0}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"


class ScoreSalesNavLeadsTests(unittest.TestCase):
    def test_scores_leads_and_writes_only_matches(self) -> None:
        with tempfile.TemporaryDirectory() as td, MockServer() as mock:
            run_dir = Path(td) / "run"
            run_dir.mkdir()
            leads_path = run_dir / "leads.jsonl"
            mutuals_path = run_dir / "mutuals.jsonl"
            state_path = run_dir / "state.json"
            leads = [
                {
                    "member_id": "1",
                    "name": "Ada",
                    "title": "Investor",
                    "company": "Brookfield",
                    "summary": "Real estate exposure across infrastructure funds.",
                    "experiences": [{"company_name": "Brookfield", "title": "Real Estate Investor"}],
                    "education": [],
                    "linkedin_url": "https://linkedin.com/in/ada",
                    "mutual_count": 1,
                },
                {
                    "member_id": "2",
                    "name": "Grace",
                    "title": "Software Engineer",
                    "company": "TechCo",
                    "summary": "Distributed systems.",
                    "experiences": [],
                    "education": [],
                    "linkedin_url": "https://linkedin.com/in/grace",
                    "mutual_count": 0,
                },
            ]
            leads_path.write_text("".join(json.dumps(row) + "\n" for row in leads))
            mutuals_path.write_text(json.dumps({
                "lead_member_id": "1",
                "mutual_member_id": "9",
                "mutual_name": "Mallory",
                "mutual_linkedin_url": "https://linkedin.com/in/mallory",
                "operators": [{"operator_id": "op", "operator_name": "Op", "total_interactions": 7}],
            }) + "\n")
            state = {
                "query": "who works at Brookfield",
                "files": {
                    "leads_jsonl": str(leads_path),
                    "mutuals_jsonl": str(mutuals_path),
                    "manifest": str(run_dir / "manifest.json"),
                },
            }
            state_path.write_text(json.dumps(state))
            proc = subprocess.run([
                sys.executable, str(PRIMITIVE),
                "--state", str(state_path),
                "--criteria", "real estate exposure",
                "--api-base", mock.url,
                "--api-key", "fake",
                "--threshold", "0.7",
            ], cwd=str(ROOT), text=True, capture_output=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["lead_count"], 2)
            self.assertEqual(out["match_count"], 1)
            self.assertEqual(mock.state["calls"], 2)
            with Path(out["outputs"]["matches_csv"]).open(newline="") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["member_id"], "1")
            self.assertIn("Mallory", rows[0]["top_mutuals_json"])
            self.assertFalse((Path(out["outputs"]["matches_csv"]).parent / "raw_scores.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
