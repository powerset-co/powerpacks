"""Local component coverage for the search-network harness path.

This test exercises the real CLI boundary that harnesses should use:

    search_network_pipeline.py prepare -> expand_search_request -> OpenAI chat API
    search_network_pipeline.py run --search-only -> local search + local hydration

The HTTP server below is intentionally tiny but OpenAI-compatible enough for the
SDK calls made by the parallel extractors and query embedding client. The
component test combines it with the local DuckDB search backend, so it validates
the no-live-API search path that replaced the legacy harness-composed extraction
skill.
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.shared.csv_io import CsvIO
from packs.search.primitives.deep_search import company_context, search_harness


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "packs/search/primitives/search_network_pipeline/search_network_pipeline.py"
PERSON_1 = "00000000-0000-0000-0000-000000000001"
PERSON_2 = "00000000-0000-0000-0000-000000000002"
PERSON_3 = "00000000-0000-0000-0000-000000000003"
SET_ID = "10000000-0000-0000-0000-000000000001"
OPERATOR_ID = "20000000-0000-0000-0000-000000000001"


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

        if self.path.endswith("/embeddings"):
            self._send_json({
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [1.0, 0.0, 0.0]}],
                "model": body.get("model") or "text-embedding-3-small",
            })
            return

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

        self._send_json({
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
        })

    def _send_json(self, payload: dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_prepare(tmp: Path, server: ThreadingHTTPServer, *, env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    env_file = tmp / "component.env"
    env_file.write_text("", encoding="utf-8")
    output_dir = tmp / "run"
    child_env = dict(os.environ)
    child_env.update(env or {})
    child_env.update({
        "OPENAI_API_KEY": "test-key",
        "OPENAI_API_BASE": f"http://127.0.0.1:{server.server_port}",
        "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
    })
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
        env=child_env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return proc, json.loads(proc.stdout) if proc.returncode == 0 else {}


def write_local_search_db(path: Path) -> None:
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise unittest.SkipTest("duckdb is required for component search test") from exc

    conn = duckdb.connect(str(path))
    conn.execute(
        """
        CREATE TABLE local_people_positions (
          id VARCHAR,
          base_id VARCHAR,
          position_title VARCHAR,
          city VARCHAR,
          state VARCHAR,
          country VARCHAR,
          metro_areas VARCHAR[],
          role_track VARCHAR,
          seniority_band VARCHAR,
          company_id VARCHAR,
          is_current BOOLEAN,
          allowed_operator_ids VARCHAR[],
          role_ids VARCHAR[],
          phrase_tokens VARCHAR[],
          word_tokens VARCHAR[],
          vector DOUBLE[],
          total_years_experience DOUBLE
        )
        """
    )
    rows = [
        (f"{PERSON_1}-1", PERSON_1, "Senior Software Engineer", "San Francisco", "CA", "United States", ["San Francisco Bay Area"], "engineer", "senior", "company_1", True, [OPERATOR_ID], ["software_engineer"], ["softwar engin", "backend engin"], ["software", "engineer", "backend", "software engineer"], [1.0, 0.0, 0.0], 8.0),
        (f"{PERSON_2}-1", PERSON_2, "Backend Engineer", "San Francisco", "CA", "United States", ["San Francisco Bay Area"], "engineer", "mid", "company_2", True, [OPERATOR_ID], ["software_engineer"], ["backend engin"], ["backend", "engineer", "software"], [0.9, 0.1, 0.0], 5.0),
        (f"{PERSON_3}-1", PERSON_3, "Account Executive", "New York City", "NY", "United States", ["New York Metropolitan Area"], "sales", "mid", "company_3", True, [OPERATOR_ID], ["sales"], ["account execut"], ["account", "executive"], [0.0, 1.0, 0.0], 6.0),
    ]
    conn.executemany("INSERT INTO local_people_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.close()


def read_jsonl(path: Path) -> list[dict[str, object]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class SearchNetworkMockOpenAITests(unittest.TestCase):
    def test_search_harness_compile_accepts_hiring_company_domain(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        filters_seen: list[tuple] = []

        class Namespace:
            def query(self, **kwargs: object) -> SimpleNamespace:
                filters_seen.append(kwargs["filters"])
                return SimpleNamespace(rows=[SimpleNamespace(
                    id="acme", company_name="Acme", website_domain="acme.example",
                    linkedin_url="https://www.linkedin.com/company/acme",
                )])

        try:
            with tempfile.TemporaryDirectory() as raw:
                run_dir = Path(raw)
                env_file = run_dir / "component.env"
                env_file.write_text("", encoding="utf-8")
                jd = run_dir / "jd.txt"
                jd.write_text("Build and operate production backend systems.", encoding="utf-8")
                plan_path = run_dir / "epoch0" / "plan.json"
                plan_path.parent.mkdir()
                plan_path.write_text(json.dumps({
                    "job_id": "jd-1", "job_title": "Backend Engineer",
                    "normalized_archetype": "software engineer", "target_level": "senior_ic",
                    "source_url": "https://acme.example/careers/backend",
                    "set_scope": {"set_id": SET_ID},
                    "hiring_company": {"name": "Acme", "website_url": "https://acme.example"},
                    "search_scope": {"location": "San Francisco Bay Area",
                                     "filters": {"metro_areas": ["San Francisco Bay Area"]}},
                    "filters": [], "retrieval_filters": {},
                    "traits": {"must_have": [{"trait": "backend systems", "tier": "core"}]},
                }), encoding="utf-8")
                queries = run_dir / "queries.json"
                queries.write_text(json.dumps([{
                    "key": "q00", "query": "Software Engineer in San Francisco Bay Area",
                }]), encoding="utf-8")
                (run_dir / "decision.json").write_text(json.dumps({
                    "surface": "people", "backend": "powerset", "depth": "deep",
                }), encoding="utf-8")
                search_harness.initialize_run(
                    run_dir=run_dir, jd_path=jd, plan_path=plan_path, queries_path=queries)
                environment = {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_API_BASE": f"http://127.0.0.1:{server.server_port}",
                    "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                }
                rapidapi_stats = {
                    "cache_hits": 0, "cache_misses": 1, "live_lookups": 0,
                    "unresolved": 1, "cost_usd": 0.0, "unit_cost_usd": 0.0,
                    "billing_basis": "unit_price_not_configured",
                }
                with mock.patch.dict(os.environ, environment), \
                     mock.patch.object(company_context.company_search.turbopuffer_backend,
                                       "namespace", return_value=Namespace()), \
                     mock.patch.object(search_harness, "resolve_company_contexts",
                                       return_value=([{}], rapidapi_stats)):
                    search_harness.compile_pond(run_dir=run_dir, env_file=str(env_file))
                saved = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(saved["status"], "awaiting_payload_review")
        self.assertIn(("website_domain", "Eq", "acme.example"), filters_seen)

    def test_prepare_runs_parallel_expansion_against_mock_openai(self) -> None:
        MockOpenAIHandler.request_count = 0
        MockOpenAIHandler.request_paths = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                proc, out = run_prepare(Path(tmp), server)
                payload_path = Path(out.get("payload_json", ""))
                payload_exists = payload_path.exists()
                payload_value = json.loads(payload_path.read_text()) if payload_exists else {}
                prompt_manifest = Path(out.get("expand_prompt_bundle", "")) / "manifest.json"
                prompt_manifest_exists = prompt_manifest.exists()
                prompt_manifest_value = json.loads(prompt_manifest.read_text()) if prompt_manifest_exists else {}
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(out["status"], "preview_ready")
        self.assertEqual(out["quality_issues"], [])
        self.assertEqual(out["preview"]["filters"]["metro_areas"], ["San Francisco Bay Area"])
        self.assertNotIn("cities", out["preview"]["filters"])
        self.assertEqual(out["preview"]["filters"]["seniority_bands"], ["mid", "senior"])
        self.assertIn("--execute-approved", out["execute_command"])
        self.assertTrue(payload_exists)
        self.assertEqual(payload_value["traits"], [])
        self.assertIs(payload_value["has_domain_intent"], False)
        self.assertIs(payload_value["role_search_filters"]["has_domain_intent"], False)
        self.assertTrue(prompt_manifest_exists)
        manifest = prompt_manifest_value
        self.assertEqual(manifest["bundle_sha256"], out["expand_prompt_bundle_sha256"])
        self.assertEqual(set(manifest["files"]), {
            "company.txt", "education.txt", "location.txt", "role.txt",
            "seniority.txt", "social.txt", "temporal.txt", "trait_generation.txt",
        })
        self.assertGreaterEqual(MockOpenAIHandler.request_count, 8)
        self.assertTrue(all(path.endswith("/chat/completions") for path in MockOpenAIHandler.request_paths))

    def test_component_prepare_run_search_only_with_local_duckdb(self) -> None:
        MockOpenAIHandler.request_count = 0
        MockOpenAIHandler.request_paths = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp_raw:
                tmp = Path(tmp_raw)
                local_db = tmp / "local-search.duckdb"
                env_file = tmp / "component.env"
                write_local_search_db(local_db)
                env_file.write_text("", encoding="utf-8")
                component_env = {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_API_BASE": f"http://127.0.0.1:{server.server_port}",
                    "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                    "POWERPACKS_LOCAL_SEARCH_DB": str(local_db),
                    "POWERPACKS_DEFAULT_SET_ID": SET_ID,
                    "POWERSET_DEFAULT_SET_ID": SET_ID,
                    "DATABASE_URL": "",
                    "SUPABASE_DATABASE_URL": "",
                    "SUPABASE_DB_URL": "",
                    "POSTGRES_HOST": "",
                }
                prepare_proc, prepare = run_prepare(tmp, server, env=component_env)
                self.assertEqual(prepare_proc.returncode, 0, prepare_proc.stderr + prepare_proc.stdout)

                run_proc = subprocess.run(
                    [
                        sys.executable,
                        str(PIPELINE),
                        "run",
                        "--backend",
                        "local",
                        "--db",
                        str(local_db),
                        "--ledger",
                        str(tmp / "local-pipeline.ledger.json"),
                        "--query",
                        "software engineers in sf",
                        "--payload-json",
                        str(prepare["payload_json"]),
                        "--search-only",
                        "--limit",
                        "10",
                        "--top-k",
                        "10",
                        "--timeout",
                        "30",
                    ],
                    cwd=ROOT,
                    env={**os.environ, **component_env},
                    text=True,
                    capture_output=True,
                    timeout=90,
                )
                self.assertEqual(run_proc.returncode, 0, run_proc.stderr + run_proc.stdout)
                out = json.loads(run_proc.stdout)
                state = json.loads(Path(str(out["state"])).read_text())
                artifacts = out["artifacts"]
                csv_path = Path(artifacts["csv"])
                jsonl_path = Path(artifacts["jsonl"])
                manifest_path = Path(artifacts["manifest"])

                self.assertEqual(out["status"], "completed")
                self.assertGreaterEqual(out["summary"]["returned_people"], 2)
                self.assertEqual(out["summary"]["hydrated"], 2)
                self.assertEqual(out["summary"]["rows"], 2)
                self.assertTrue(csv_path.exists())
                self.assertTrue(jsonl_path.exists())
                self.assertTrue(manifest_path.exists())
                self.assertEqual(read_jsonl(jsonl_path)[0]["person_id"], PERSON_1)
                hydrate_step = next(step for step in state["steps"] if step["id"] == "hydrate_people")
                self.assertEqual(hydrate_step["output"]["source"]["backend"], "duckdb")
                self.assertEqual(hydrate_step["output"]["source"]["type"], "local_duckdb")
                with csv_path.open(newline="") as handle:
                    self.assertEqual(len(list(CsvIO.dict_reader(handle))), 2)

                step_ids = [step["id"] for step in state["steps"]]
                self.assertNotIn("resolve_set_operators", step_ids)
                self.assertIn("execute_role_search", step_ids)
                self.assertIn("hydrate_people", step_ids)
                self.assertNotIn("llm_filter_candidates", step_ids)
                self.assertTrue(any(path.endswith("/embeddings") for path in MockOpenAIHandler.request_paths))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
