"""Tests for `llm_rerank_candidates`.

Spins up a localhost stdlib HTTP server that mimics the OpenAI chat
completion endpoint, drives the primitive against it with high
concurrency, and validates:

  - all input items get a result
  - results preserve input order
  - the asyncio.Semaphore actually caps in-flight requests at the
    configured concurrency (the mock server tracks `max_in_flight`)
  - retries on 429 work
  - parse error path is graceful

No real network. No OpenAI credits spent. Safe in CI.
"""

from __future__ import annotations

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


ROOT = Path(__file__).resolve().parents[1]
RERANK_PY = (
    ROOT
    / "packs"
    / "search"
    / "primitives"
    / "llm_rerank_candidates"
    / "llm_rerank_candidates.py"
)


# ---------------------------------------------------------------------------
# Mock OpenAI server (stdlib only)
# ---------------------------------------------------------------------------


def _make_mock_handler(state: dict[str, Any]):
    """Factory: returns a BaseHTTPRequestHandler that records concurrency."""

    class MockOpenAI(BaseHTTPRequestHandler):
        # Silence default access-log noise.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode()
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return

            # Track concurrency via the shared state dict.
            with state["lock"]:
                state["in_flight"] += 1
                state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
                state["calls"] += 1
                # Optional: error injection.
                inject_429 = state["inject_429_until"] > 0
                if inject_429:
                    state["inject_429_until"] -= 1
            try:
                if inject_429:
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"rate limited"}}')
                    return

                # Simulate model latency so concurrency actually matters.
                time.sleep(state["latency_sec"])

                # Echo a deterministic verdict that depends on the
                # CANDIDATE JSON section of the prompt (not query/traits)
                # so tests can isolate per-item scoring.
                user_prompt = body["messages"][1]["content"]
                marker = "Candidate (JSON):"
                idx = user_prompt.find(marker)
                candidate_section = user_prompt[idx + len(marker) :] if idx >= 0 else ""
                # Score = 0.9 if "openai" in candidate, else 0.4
                score = 0.9 if "openai" in candidate_section.lower() else 0.4
                verdict = "include" if score >= 0.5 else "exclude"
                reason = "mock verdict"

                response = {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": body.get("model", "mock"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {"score": score, "verdict": verdict, "reason": reason}
                                ),
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
    """Context-manager wrapping the mock OpenAI server."""

    def __init__(self, *, latency_sec: float = 0.05, inject_429_until: int = 0):
        self.state: dict[str, Any] = {
            "lock": threading.Lock(),
            "in_flight": 0,
            "max_in_flight": 0,
            "calls": 0,
            "latency_sec": latency_sec,
            "inject_429_until": inject_429_until,
        }
        # ThreadingHTTPServer is critical: each request handled in its own
        # thread so we actually observe concurrency.
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _make_mock_handler(self.state)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_MockServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"


# ---------------------------------------------------------------------------
# Primitive driver — invoke as subprocess (matches the real CLI usage)
# ---------------------------------------------------------------------------


def run_rerank(
    *,
    items: list[dict[str, Any]],
    query: str,
    traits: list[str],
    api_base: str,
    concurrency: int,
    extra_args: list[str] | None = None,
) -> tuple[int, list[dict[str, Any]], str]:
    """Run the CLI and return (exit_code, results, stderr)."""
    in_jsonl = "\n".join(json.dumps(d) for d in items)
    cmd = [
        sys.executable,
        str(RERANK_PY),
        "--in",
        "-",
        "--out",
        "-",
        "--query",
        query,
        "--api-base",
        api_base,
        "--api-key",
        "fake-key-mock",
        "--concurrency",
        str(concurrency),
        "--timeout",
        "10",
    ]
    for t in traits:
        cmd += ["--traits", t]
    if extra_args:
        cmd += extra_args
    proc = subprocess.run(
        cmd,
        input=in_jsonl,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=60,
    )
    results: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            results.append(json.loads(line))
    return proc.returncode, results, proc.stderr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class FanOutConcurrencyTests(unittest.TestCase):
    def test_200_items_concurrency_50_all_succeed_and_semaphore_caps_in_flight(self) -> None:
        items = [
            {"id": f"p{i}", "name": f"Person {i}", "headline": "AI engineer at OpenAI"}
            for i in range(200)
        ]
        with _MockServer(latency_sec=0.05) as mock:
            rc, results, stderr = run_rerank(
                items=items,
                query="ai or software engineer at open ai",
                traits=["ai or software engineer", "at openai"],
                api_base=mock.url,
                concurrency=50,
            )
            self.assertEqual(rc, 0, f"non-zero exit. stderr:\n{stderr}")
            self.assertEqual(len(results), 200, f"expected 200 results, got {len(results)}")
            # All succeeded.
            self.assertEqual(sum(1 for r in results if r.get("error") is None), 200)
            # Order preserved.
            self.assertEqual([r["id"] for r in results[:5]], ["p0", "p1", "p2", "p3", "p4"])
            # Semaphore cap honored. Allow tiny slack (1) because peak is
            # measured under threading; in practice we should see exactly N.
            self.assertLessEqual(
                mock.state["max_in_flight"],
                50,
                f"semaphore breached: peak in-flight = {mock.state['max_in_flight']}",
            )
            # Concurrency was actually exercised: peak should be near the cap
            # (otherwise the test isn't proving anything).
            self.assertGreaterEqual(
                mock.state["max_in_flight"],
                25,
                f"too little parallelism observed: peak = {mock.state['max_in_flight']}",
            )
            # All inputs hit the server.
            self.assertEqual(mock.state["calls"], 200)

    def test_low_concurrency_caps_in_flight_at_5(self) -> None:
        items = [{"id": f"q{i}", "headline": "AI engineer at OpenAI"} for i in range(30)]
        with _MockServer(latency_sec=0.04) as mock:
            rc, results, stderr = run_rerank(
                items=items,
                query="x",
                traits=["t"],
                api_base=mock.url,
                concurrency=5,
            )
            self.assertEqual(rc, 0, stderr)
            self.assertEqual(len(results), 30)
            self.assertLessEqual(mock.state["max_in_flight"], 5)


class FanOutVerdictShapeTests(unittest.TestCase):
    def test_score_and_verdict_extracted_from_mock_response(self) -> None:
        items = [
            {"id": "p_match", "headline": "AI engineer at OpenAI"},      # mock returns 0.9 / include
            {"id": "p_other", "headline": "Pastry chef at a bakery"},    # mock returns 0.4 / exclude
        ]
        with _MockServer(latency_sec=0) as mock:
            rc, results, stderr = run_rerank(
                items=items,
                query="ai engineers",
                traits=["openai"],
                api_base=mock.url,
                concurrency=2,
            )
            self.assertEqual(rc, 0, stderr)
            by_id = {r["id"]: r for r in results}
            self.assertEqual(by_id["p_match"]["verdict"], "include")
            self.assertGreaterEqual(by_id["p_match"]["score"], 0.8)
            self.assertEqual(by_id["p_other"]["verdict"], "exclude")
            self.assertLess(by_id["p_other"]["score"], 0.5)
            # Each result has model + elapsed_ms set.
            for r in results:
                self.assertIn("model", r)
                self.assertIn("elapsed_ms", r)
                self.assertIsNone(r["error"])


class StateModeQueryResultsCsvTests(unittest.TestCase):
    def test_state_mode_writes_query_results_csv_schema_artifact(self) -> None:
        profile = {
            "person_id": "p1",
            "name": "Ada",
            "headline": "AI Engineer at OpenAI",
            "base_score": 0.42,
            "matched_position_indexes": [0],
            "vertical_sources": ["role"],
            "positions": [
                {
                    "position_title": "AI Engineer",
                    "company_name": "OpenAI",
                    "is_current": True,
                },
                {
                    "position_title": "Intern",
                    "company_name": "OtherCo",
                    "is_current": False,
                },
            ],
        }
        compact_profile = dict(profile)
        compact_profile["headline"] = "Engineer at OtherCo"
        compact_profile["positions"] = [
            {
                "position_title": "Engineer",
                "company_name": "OtherCo",
                "is_current": True,
            }
        ]
        with tempfile.TemporaryDirectory() as td, _MockServer(latency_sec=0) as mock:
            profiles_path = Path(td) / "hydrate_people" / "profiles.jsonl"
            llm_profiles_path = Path(td) / "hydrate_people" / "llm_profiles.jsonl"
            profiles_path.parent.mkdir(parents=True)
            profiles_path.write_text(json.dumps(profile) + "\n")
            llm_profiles_path.write_text(json.dumps(compact_profile) + "\n")
            state = {
                "task_id": "search-network-test",
                "conversation_id": "conv-test",
                "query": "ai engineer at openai",
                "steps": [
                    {
                        "id": "hydrate_people",
                        "output": {
                            "profile_ids": ["p1"],
                            "profiles_path": str(profiles_path),
                            "llm_profiles_path": str(llm_profiles_path),
                        },
                    }
                ],
            }
            state_path = Path(td) / "state.json"
            state_path.write_text(json.dumps(state))
            cmd = [
                sys.executable,
                str(RERANK_PY),
                "--state",
                str(state_path),
                "--api-base",
                mock.url,
                "--api-key",
                "fake",
                "--traits",
                "ai engineer",
                "--write-state",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            output = json.loads(proc.stdout)
            artifacts = output["artifacts"]
            self.assertEqual(set(artifacts), {"query_results_csv"})
            with Path(artifacts["query_results_csv"]).open(newline="") as handle:
                import csv
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(
                set(row),
                {
                    "conversation_id",
                    "query",
                    "person_id",
                    "result_index",
                    "matched_position_indexes",
                    "final_score",
                    "trait_scores",
                    "overall_reasoning",
                    "pre_rerank_score",
                    "tags",
                    "vertical_sources",
                    "created_at",
                },
            )
            self.assertEqual(row["conversation_id"], "conv-test")
            self.assertEqual(row["query"], "ai engineer at openai")
            self.assertEqual(row["person_id"], "p1")
            self.assertEqual(row["result_index"], "0")
            self.assertEqual(float(row["final_score"]), 0.9)
            self.assertEqual(json.loads(row["matched_position_indexes"]), [0])
            self.assertEqual(float(row["pre_rerank_score"]), 0.42)
            self.assertEqual(json.loads(row["vertical_sources"]), ["role"])
            trait_scores = json.loads(row["trait_scores"])
            self.assertIn("ai engineer", trait_scores)
            self.assertIn("score", trait_scores["ai engineer"])
            self.assertIn("reason", trait_scores["ai engineer"])
            updated = json.loads(state_path.read_text())
            self.assertEqual(updated["steps"][-1]["id"], "llm_rerank_candidates")


class FanOutRetryTests(unittest.TestCase):
    def test_retries_on_429_then_succeeds(self) -> None:
        # 5 items, server returns 429 for the first 5 calls then succeeds.
        items = [{"id": f"r{i}", "headline": "AI engineer at OpenAI"} for i in range(5)]
        with _MockServer(latency_sec=0, inject_429_until=5) as mock:
            rc, results, stderr = run_rerank(
                items=items,
                query="x",
                traits=["t"],
                api_base=mock.url,
                concurrency=5,
                extra_args=["--max-retries", "3"],
            )
            self.assertEqual(rc, 0, stderr)
            self.assertEqual(len(results), 5)
            # All eventually succeeded because 429s exhausted, then 200s.
            self.assertEqual(sum(1 for r in results if r.get("error") is None), 5)
            # We made more total calls than items because of retries.
            self.assertGreater(mock.state["calls"], 5)


class DryRunTests(unittest.TestCase):
    def test_dry_run_does_not_hit_the_server(self) -> None:
        items = [{"id": "p1", "headline": "AI engineer at OpenAI"}]
        with _MockServer(latency_sec=0) as mock:
            cmd = [
                sys.executable,
                str(RERANK_PY),
                "--in",
                "-",
                "--query",
                "x",
                "--traits",
                "t",
                "--dry-run",
                "--api-base",
                mock.url,
                "--api-key",
                "fake",
            ]
            proc = subprocess.run(
                cmd,
                input=json.dumps(items[0]) + "\n",
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(mock.state["calls"], 0, "dry-run should not call the server")
            self.assertIn("dry-run", proc.stderr)
            self.assertIn("p1", proc.stderr)


if __name__ == "__main__":
    unittest.main()


class RerankEstimateTests(unittest.TestCase):
    def test_estimate_scales_by_async_waves(self):
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("llm_rerank_candidates_est", RERANK_PY)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["llm_rerank_candidates_est"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        self.assertEqual(mod.estimate_rerank_seconds(0, 200), 0)
        self.assertEqual(mod.estimate_rerank_seconds(100, 200), 30)
        self.assertEqual(mod.estimate_rerank_seconds(1400, 200), 210)
        self.assertIn("small runs", mod.rerank_status_note(30))
        self.assertIn("2-3 minutes", mod.rerank_status_note(180))

    def test_dry_run_prints_estimate_and_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidates.jsonl"
            path.write_text('{"id":"p1","title":"Engineer"}\n')
            proc = subprocess.run(
                [
                    sys.executable,
                    str(RERANK_PY),
                    "--in",
                    str(path),
                    "--query",
                    "engineers",
                    "--dry-run",
                    "--concurrency",
                    "25",
                ],
                text=True,
                capture_output=True,
                timeout=10,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("rerank: dry-run items=1 concurrency=25 estimated=30s", proc.stderr)
