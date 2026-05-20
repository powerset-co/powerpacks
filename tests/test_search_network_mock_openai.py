"""Local component coverage for the search-network harness path.

This test exercises the real CLI boundary that harnesses should use:

    search_network_pipeline.py prepare -> expand_search_request -> OpenAI chat API
    search_network_pipeline.py run --search-only -> local search + fixture hydration

The HTTP server below is intentionally tiny but OpenAI-compatible enough for the
SDK calls made by the parallel extractors and embedding client. The component
test combines it with the local DuckDB search backend and a JSON Postgres
fixture, so it validates the no-live-API happy path that replaced the legacy
harness-composed extraction skill.
"""
from __future__ import annotations

import csv
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
        (f"{PERSON_1}-1", PERSON_1, "Senior Software Engineer", "San Francisco", "CA", "United States", "engineer", "senior", "company_1", True, [OPERATOR_ID], ["software_engineer"], ["softwar engin", "backend engin"], ["software", "engineer", "backend", "software engineer"], [1.0, 0.0, 0.0], 8.0),
        (f"{PERSON_2}-1", PERSON_2, "Backend Engineer", "San Francisco", "CA", "United States", "engineer", "mid", "company_2", True, [OPERATOR_ID], ["software_engineer"], ["backend engin"], ["backend", "engineer", "software"], [0.9, 0.1, 0.0], 5.0),
        (f"{PERSON_3}-1", PERSON_3, "Account Executive", "New York City", "NY", "United States", "sales", "mid", "company_3", True, [OPERATOR_ID], ["sales"], ["account execut"], ["account", "executive"], [0.0, 1.0, 0.0], 6.0),
    ]
    conn.executemany("INSERT INTO local_people_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.close()


def write_postgres_fixture(path: Path) -> None:
    def person(pid: str, name: str, title: str, company: str) -> dict[str, object]:
        return {
            "id": pid,
            "public_identifier": name.lower().replace(" ", "-"),
            "public_profile_url": f"https://linkedin.com/in/{name.lower().replace(' ', '-')}",
            "full_name": name,
            "headline": f"{title} at {company}",
            "summary": f"{name} builds production backend systems.",
            "profile_picture_url": None,
            "location_raw": "San Francisco, CA",
            "city": "San Francisco",
            "state": "CA",
            "country": "United States",
            "hydrated_context": {
                "name": name,
                "headline": f"{title} at {company}",
                "location": "San Francisco, CA",
                "linkedin_url": f"https://linkedin.com/in/{name.lower().replace(' ', '-')}",
                "positions": [{"id": f"{pid}-1", "title": title, "company": company, "company_id": company.lower(), "is_current": True}],
                "education": [],
                "tech_skills": ["Python", "Distributed Systems"],
            },
            "x_twitter_handle": None,
            "x_twitter_followers": 0,
            "linkedin_followers": 100,
            "linkedin_connections": 500,
            "ig_handle": None,
            "ig_followers": 0,
            "inferred_birth_year": 1990,
        }

    fixture = {
        "sets": [{"id": SET_ID, "name": "Component Test Set", "created_by": "auth0|component", "is_active": True, "is_personal": True, "created_at": "2026-01-01T00:00:00Z"}],
        "users": [{"id": OPERATOR_ID, "user_id": "auth0|component", "email": "component@example.com", "name": "Component Tester"}],
        "set_members": [{"set_id": SET_ID, "user_id": "auth0|component", "role": "owner", "joined_at": "2026-01-01T00:00:00Z"}],
        "persons": [
            person(PERSON_1, "Ada Backend", "Senior Software Engineer", "Company One"),
            person(PERSON_2, "Grace Systems", "Backend Engineer", "Company Two"),
        ],
        "person_source_summary": [
            {"person_id": PERSON_1, "operator_id": OPERATOR_ID, "total_interactions": 7},
            {"person_id": PERSON_2, "operator_id": OPERATOR_ID, "total_interactions": 3},
        ],
    }
    path.write_text(json.dumps(fixture, indent=2, sort_keys=True), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, object]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class SearchNetworkMockOpenAITests(unittest.TestCase):
    def test_prepare_runs_parallel_expansion_against_mock_openai(self) -> None:
        MockOpenAIHandler.request_count = 0
        MockOpenAIHandler.request_paths = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                proc, out = run_prepare(Path(tmp), server)
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

    def test_component_prepare_run_search_only_with_local_search_and_postgres_fixture(self) -> None:
        MockOpenAIHandler.request_count = 0
        MockOpenAIHandler.request_paths = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp_raw:
                tmp = Path(tmp_raw)
                local_db = tmp / "local-search.duckdb"
                pg_fixture = tmp / "postgres-fixture.json"
                env_file = tmp / "component.env"
                write_local_search_db(local_db)
                write_postgres_fixture(pg_fixture)
                env_file.write_text("", encoding="utf-8")
                component_env = {
                    "OPENAI_API_KEY": "test-key",
                    "OPENAI_API_BASE": f"http://127.0.0.1:{server.server_port}",
                    "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                    "POWERPACKS_LOCAL_SEARCH_DB": str(local_db),
                    "POWERPACKS_POSTGRES_FIXTURE_JSON": str(pg_fixture),
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
                        "--ledger",
                        str(prepare["ledger"]),
                        "--query",
                        "software engineers in sf",
                        "--payload-json",
                        str(prepare["payload_json"]),
                        "--env-file",
                        str(env_file),
                        "--search-only",
                        "--execute-approved",
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
                with csv_path.open(newline="") as handle:
                    self.assertEqual(len(list(csv.DictReader(handle))), 2)

                step_ids = [step["id"] for step in state["steps"]]
                self.assertIn("resolve_set_operators", step_ids)
                self.assertIn("execute_role_search", step_ids)
                self.assertIn("hydrate_people", step_ids)
                self.assertNotIn("llm_filter_candidates", step_ids)
                self.assertTrue(any(path.endswith("/embeddings") for path in MockOpenAIHandler.request_paths))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
