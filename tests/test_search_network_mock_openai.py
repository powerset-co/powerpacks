"""Mock-OpenAI integration coverage for the search-network prepare path.

This test exercises the real CLI boundary that harnesses should use:

    search_network_pipeline.py prepare -> expand_search_request -> OpenAI chat API

The HTTP server below is intentionally tiny but OpenAI-compatible enough for the
SDK calls made by the parallel extractors. It lets us validate the no-live-API
happy path that replaced the legacy harness-composed extraction skill.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "packs/search/primitives/search_network_pipeline/search_network_pipeline.py"


class MockOpenAIHandler(BaseHTTPRequestHandler):
    request_count = 0
    request_paths: list[str] = []
    lock = threading.Lock()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or "0")
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            body = {}

        with self.lock:
            type(self).request_count += 1
            type(self).request_paths.append(self.path)

        body_text = json.dumps(body)
        system_prompt = "\n".join(
            msg.get("content", "")
            for msg in body.get("messages", [])
            if msg.get("role") == "system"
        )

        content: dict[str, object]
        if "Generate traits for this query" in body_text:
            content = {"traits": [], "has_domain_intent": False}
        elif "extracting company" in system_prompt:
            content = {}
        elif "extracting location" in system_prompt:
            content = {"cities": ["San Francisco"]}
        elif "extracting education" in system_prompt:
            content = {}
        elif "time-related information" in system_prompt:
            content = {"is_current_role": True}
        elif "detecting seniority" in system_prompt:
            content = {"seniority_bands": ["mid", "senior"]}
        elif "social media criteria" in system_prompt:
            content = {}
        else:
            content = {
                "semantic_query": (
                    "Experienced software engineers who build production systems, "
                    "own backend or full-stack implementation, and show evidence "
                    "of technical execution in product or infrastructure teams."
                ),
                "bm25_queries": ["software engineer", "backend engineer"],
            }

        payload = {
            "id": "chatcmpl_mock",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model") or "mock-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(content),
                    },
                }
            ],
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class SearchNetworkMockOpenAITests(unittest.TestCase):
    def test_prepare_runs_parallel_expansion_against_mock_openai(self) -> None:
        MockOpenAIHandler.request_count = 0
        MockOpenAIHandler.request_paths = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                env_file = Path(tmp) / "empty.env"
                env_file.write_text("", encoding="utf-8")
                output_dir = Path(tmp) / "run"
                env = dict(os.environ)
                env.update(
                    {
                        "OPENAI_API_KEY": "test-key",
                        "OPENAI_API_BASE": f"http://127.0.0.1:{server.server_port}",
                    }
                )
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(PIPELINE),
                        "prepare",
                        "--query",
                        "software engineers in sf",
                        "--env-file",
                        str(env_file),
                        "--output-dir",
                        str(output_dir),
                        "--timeout",
                        "10",
                    ],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                out = json.loads(proc.stdout) if proc.returncode == 0 else {}
                payload_exists = Path(out.get("payload_json", "")).exists()
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(out["status"], "preview_ready")
        self.assertEqual(out["quality_issues"], [])
        self.assertEqual(out["preview"]["filters"]["cities"], ["San Francisco"])
        self.assertEqual(out["preview"]["filters"]["seniority_bands"], ["mid", "senior"])
        self.assertIn("--execute-approved", out["execute_command"])
        self.assertTrue(payload_exists)
        self.assertGreaterEqual(MockOpenAIHandler.request_count, 8)
        self.assertTrue(all(path.endswith("/chat/completions") for path in MockOpenAIHandler.request_paths))


if __name__ == "__main__":
    unittest.main()
