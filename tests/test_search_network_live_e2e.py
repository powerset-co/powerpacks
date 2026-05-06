import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATE_PY = ROOT / "packs/search/primitives/task_state/task_state.py"
RESOLVE_SET_PY = ROOT / "packs/search/primitives/resolve_set_operators/resolve_set_operators.py"
EXECUTE_ROLE_PY = ROOT / "packs/search/primitives/execute_role_search/execute_role_search.py"
HYDRATE_PY = ROOT / "packs/search/primitives/hydrate_people/hydrate_people.py"
LLM_FILTER_PY = ROOT / "packs/search/primitives/llm_filter_candidates/llm_filter_candidates.py"
LLM_RERANK_PY = ROOT / "packs/search/primitives/llm_rerank_candidates/llm_rerank_candidates.py"

QUERY_RESULTS_FIELDS = [
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
]


def line_count(path: Path) -> int:
    with path.open() as handle:
        return sum(1 for line in handle if line.strip())


def load_dotenv(env: dict[str, str]) -> dict[str, str]:
    out = dict(env)
    env_path = ROOT / ".env"
    if not env_path.exists():
        return out
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        out.setdefault(key, value)
    return out


@unittest.skipUnless(
    os.environ.get("POWERPACKS_RUN_LIVE_SEARCH_E2E") == "1",
    "set POWERPACKS_RUN_LIVE_SEARCH_E2E=1 to run live TurboPuffer/Postgres/OpenAI search e2e",
)
class SearchNetworkLiveE2ETests(unittest.TestCase):
    maxDiff = None

    def run_json(self, cmd: list[str], *, env: dict[str, str], timeout: int = 300) -> dict:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env, text=True, capture_output=True, timeout=timeout)
        self.assertEqual(proc.returncode, 0, f"command failed: {' '.join(cmd)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"command did not emit JSON: {' '.join(cmd)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}\n{exc}")

    def test_software_engineers_sf_personal_network_llm_rerank_csv(self) -> None:
        env = load_dotenv(os.environ)
        required = ["TURBOPUFFER_API_KEY", "DATABASE_URL", "OPENAI_API_KEY", "POWERPACKS_DEFAULT_SET_ID"]
        missing = [key for key in required if not env.get(key)]
        if missing:
            self.skipTest(f"missing live-search env keys: {', '.join(missing)}")

        with tempfile.TemporaryDirectory(prefix="powerpacks-search-e2e-") as td:
            run_dir = Path(td) / "runs"
            run_dir.mkdir(parents=True)
            task_id = "search-network-live-e2e-software-engineers-sf"
            state_path = run_dir / f"{task_id}.json"
            role_filters = {
                "semantic_query": (
                    "Writes code and builds software applications, designs and implements features, "
                    "debugs and fixes issues, participates in code reviews, and works as a software "
                    "engineer, backend engineer, frontend engineer, full stack engineer, platform "
                    "engineer, infrastructure engineer, systems engineer, DevOps engineer, SRE, or "
                    "mobile engineer in the San Francisco Bay Area."
                ),
                "bm25_queries": [
                    "Software Engineer",
                    "Senior Software Engineer",
                    "Staff Software Engineer",
                    "Software Developer",
                    "Backend Engineer",
                    "Frontend Engineer",
                    "Full Stack Engineer",
                    "Platform Engineer",
                    "Infrastructure Engineer",
                    "Systems Engineer",
                    "DevOps Engineer",
                    "Site Reliability Engineer",
                    "SRE",
                    "SWE",
                    "Mobile Engineer",
                ],
                "metro_areas": ["San Francisco Bay Area"],
                "is_current": True,
                "set_id": env["POWERPACKS_DEFAULT_SET_ID"],
            }

            self.run_json([
                sys.executable,
                str(STATE_PY),
                "init",
                "--task-id",
                task_id,
                "--query",
                "software engineers in sf",
                "--out",
                str(state_path),
            ], env=env)
            self.run_json([
                sys.executable,
                str(STATE_PY),
                "record-step",
                "--state",
                str(state_path),
                "--step-id",
                "expand_search_request",
                "--status",
                "completed",
                "--output-json",
                json.dumps({"role_search_filters": role_filters}),
            ], env=env)
            resolved = self.run_json([
                sys.executable,
                str(RESOLVE_SET_PY),
                "--state",
                str(state_path),
                "--env-file",
                ".env",
                "--write-state",
            ], env=env)
            self.assertGreater(resolved.get("operator_count", 0), 0)

            retrieved = self.run_json([
                sys.executable,
                str(EXECUTE_ROLE_PY),
                "--state",
                str(state_path),
                "--env-file",
                ".env",
                "--write-state",
                "--write-artifact",
                "--top-k",
                "500",
                "--limit",
                "25",
            ], env=env)
            self.assertGreater(retrieved.get("returned_people", 0), 0)

            hydrated = self.run_json([
                sys.executable,
                str(HYDRATE_PY),
                "--state",
                str(state_path),
                "--env-file",
                ".env",
                "--write-state",
            ], env=env)
            self.assertEqual(hydrated["requested"], retrieved["returned_people"])
            self.assertEqual(hydrated["hydrated"], retrieved["returned_people"])
            self.assertNotIn("profiles", hydrated)
            profiles_path = Path(hydrated["profiles_path"])
            llm_profiles_path = Path(hydrated["llm_profiles_path"])
            self.assertTrue(profiles_path.exists())
            self.assertTrue(llm_profiles_path.exists())
            self.assertEqual(line_count(profiles_path), hydrated["hydrated"])
            self.assertEqual(line_count(llm_profiles_path), hydrated["hydrated"])

            filtered = self.run_json([
                sys.executable,
                str(LLM_FILTER_PY),
                "--state",
                str(state_path),
                "--profile-scope",
                "auto",
                "--write-state",
                "--batch-size",
                "5",
            ], env=env, timeout=600)
            self.assertEqual(filtered["candidate_count"], hydrated["hydrated"])
            self.assertEqual(filtered["profile_scope"], "current")
            self.assertEqual(filtered["artifacts"], {})
            self.assertGreater(filtered["passed_count"], 0)

            reranked = self.run_json([
                sys.executable,
                str(LLM_RERANK_PY),
                "--state",
                str(state_path),
                "--concurrency",
                "25",
                "--write-state",
            ], env=env, timeout=600)
            self.assertEqual(set(reranked["artifacts"]), {"query_results_csv"})
            self.assertEqual(reranked["profile_scope"], "full")
            self.assertEqual(reranked["ranked_count"], filtered["passed_count"])

            csv_path = Path(reranked["artifacts"]["query_results_csv"])
            self.assertEqual(csv_path.name, "query_results.csv")
            self.assertTrue(csv_path.exists())
            with csv_path.open(newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                self.assertEqual(reader.fieldnames, QUERY_RESULTS_FIELDS)
            self.assertEqual(len(rows), reranked["ranked_count"])
            for index, row in enumerate(rows):
                self.assertEqual(int(row["result_index"]), index)
                self.assertEqual(row["query"], "software engineers in sf")
                self.assertTrue(row["person_id"])
                self.assertTrue(row["overall_reasoning"])
                self.assertGreaterEqual(float(row["final_score"]), 0.0)
                self.assertLessEqual(float(row["final_score"]), 1.0)
                self.assertIsInstance(json.loads(row["matched_position_indexes"]), list)
                self.assertIsInstance(json.loads(row["trait_scores"]), dict)

            rerank_dir = csv_path.parent
            self.assertFalse((rerank_dir / "query_results_v2.csv").exists())
            self.assertFalse((rerank_dir / "query_results_v2.jsonl").exists())
            self.assertFalse((rerank_dir / "raw_rerank_results.jsonl").exists())
            self.assertFalse((rerank_dir / "system_prompt.txt").exists())
            self.assertNotIn('"profiles"', state_path.read_text())


if __name__ == "__main__":
    unittest.main()
