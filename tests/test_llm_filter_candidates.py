import json
import re
import threading
import time
import subprocess
import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FILTER_PY = ROOT / "packs/search/primitives/llm_filter_candidates/llm_filter_candidates.py"


def _make_mock_handler(state: dict[str, Any]):
    class MockOpenAI(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode())
            with state["lock"]:
                state["in_flight"] += 1
                state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
                state["calls"] += 1
            try:
                time.sleep(state["latency_sec"])
                user_prompt = body["messages"][1]["content"]
                ids = sorted(set(re.findall(r"<person id='([^']+)'>", user_prompt)))
                content = {
                    "candidates": [
                        {"id": pid, "score": 0.9, "reason": "mock pass"}
                        for pid in ids
                    ]
                }
                response = {
                    "id": "chatcmpl-filter-mock",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": body.get("model", "mock"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(content),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
                payload = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            finally:
                with state["lock"]:
                    state["in_flight"] -= 1

    return MockOpenAI


class _MockServer:
    def __init__(self, *, latency_sec: float = 0.05):
        self.state = {
            "calls": 0,
            "in_flight": 0,
            "max_in_flight": 0,
            "latency_sec": latency_sec,
            "lock": threading.Lock(),
        }
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.url = ""

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_mock_handler(self.state))
        port = int(self.server.server_address[1])
        self.url = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2)


class LlmFilterProfileHandoffTests(unittest.TestCase):
    def run_filter_dry_run(self, state: dict, td: str) -> dict:
        state_path = Path(td) / "state.json"
        state_path.write_text(json.dumps(state))
        proc = subprocess.run(
            [sys.executable, str(FILTER_PY), "--state", str(state_path), "--dry-run"],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_auto_uses_compact_profiles_for_current_role_queries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            llm_profiles_path = Path(td) / "llm_profiles.jsonl"
            llm_profiles_path.write_text(json.dumps({
                "person_id": "p1",
                "name": "Ada",
                "positions": [],
                "education": [],
            }) + "\n")
            state = {
                "query": "current software engineers in sf",
                "steps": [
                    {
                        "id": "expand_search_request",
                        "output": {"role_search_filters": {"is_current_role": True}},
                    },
                    {
                        "id": "execute_role_search",
                        "output": {"candidate_ids": ["p1"]},
                    },
                    {
                        "id": "hydrate_people",
                        "output": {
                            "profile_ids": ["p1"],
                            "profiles_path": str(Path(td) / "missing-full-profiles.jsonl"),
                            "llm_profiles_path": str(llm_profiles_path),
                        },
                    },
                ],
            }
            output = self.run_filter_dry_run(state, td)
            self.assertEqual(output["profile_scope"], "current")
            self.assertEqual(output["candidate_count"], 1)
            self.assertEqual(output["missing_hydration_count"], 0)

    def test_auto_uses_full_profiles_for_all_time_queries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profiles_path = Path(td) / "profiles.jsonl"
            profiles_path.write_text(json.dumps({
                "person_id": "p1",
                "name": "Ada",
                "positions": [],
                "education": [],
            }) + "\n")
            state = {
                "query": "software engineers in sf, including past roles",
                "steps": [
                    {
                        "id": "expand_search_request",
                        "output": {"role_search_filters": {"is_current_role": False}},
                    },
                    {
                        "id": "execute_role_search",
                        "output": {"candidate_ids": ["p1"]},
                    },
                    {
                        "id": "hydrate_people",
                        "output": {
                            "profile_ids": ["p1"],
                            "profiles_path": str(profiles_path),
                            "llm_profiles_path": str(Path(td) / "missing-compact-profiles.jsonl"),
                        },
                    },
                ],
            }
            output = self.run_filter_dry_run(state, td)
            self.assertEqual(output["profile_scope"], "all")
            self.assertEqual(output["candidate_count"], 1)
            self.assertEqual(output["missing_hydration_count"], 0)

    def test_filter_batches_run_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as td, _MockServer(latency_sec=0.08) as mock:
            profile_path = Path(td) / "llm_profiles.jsonl"
            profile_path.write_text(
                "".join(
                    json.dumps({
                        "person_id": f"p{i}",
                        "name": f"Person {i}",
                        "positions": [{"position_title": "Head of Sales", "is_current": True}],
                        "education": [],
                    }) + "\n"
                    for i in range(20)
                )
            )
            state_path = Path(td) / "state.json"
            state_path.write_text(json.dumps({
                "query": "heads of sales in fintech",
                "steps": [
                    {
                        "id": "expand_search_request",
                        "output": {"role_search_filters": {"is_current_role": True}},
                    },
                    {
                        "id": "hydrate_people",
                        "output": {
                            "profile_ids": [f"p{i}" for i in range(20)],
                            "llm_profiles_path": str(profile_path),
                        },
                    },
                ],
            }))
            proc = subprocess.run(
                [
                    sys.executable,
                    str(FILTER_PY),
                    "--state",
                    str(state_path),
                    "--api-base",
                    mock.url,
                    "--api-key",
                    "fake",
                    "--batch-size",
                    "1",
                    "--concurrency",
                    "5",
                    "--write-state",
                ],
                cwd=str(ROOT),
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            output = json.loads(proc.stdout)
            self.assertEqual(output["batch_count"], 20)
            self.assertEqual(output["concurrency"], 5)
            self.assertEqual(output["passed_count"], 20)
            token_usage = output["token_usage_estimate"]
            self.assertEqual(token_usage["estimator"], "tiktoken_chat_prompt")
            self.assertEqual(token_usage["request_count"], 20)
            self.assertGreater(token_usage["prompt_tokens_total"], 0)
            self.assertGreater(token_usage["prompt_tokens_per_minute"], 0)
            self.assertEqual(mock.state["calls"], 20)
            self.assertGreater(mock.state["max_in_flight"], 1)
            self.assertLessEqual(mock.state["max_in_flight"], 5)
            self.assertIn("filter: starting candidates=20 batches=20", proc.stderr)
            self.assertIn("filter: completed 20/20 batches", proc.stderr)
            updated = json.loads(state_path.read_text())
            step_output = updated["steps"][-1]["output"]
            self.assertIn("token_usage_estimate", step_output)


if __name__ == "__main__":
    unittest.main()
