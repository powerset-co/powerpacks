import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PEOPLE = ROOT / "tests/fixtures/indexing/people.csv"
PIPELINE = ROOT / "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"
HYDRATE = ROOT / "packs/search/primitives/hydrate_people/hydrate_people.py"


def run_json(args: list[str]) -> dict:
    proc = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {proc.stderr}\nstdout={proc.stdout}")
    return json.loads(proc.stdout)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class LocalHydrationTests(unittest.TestCase):
    def test_hydrate_people_local_db_state_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / ".powerpacks/search-index"
            run_json([str(PIPELINE), "run", "--output-dir", str(out), "--run-id", "hydrate", "--input", str(FIXTURE_PEOPLE), "--force"])
            run_dir = out / "hydrate"
            db = run_dir / "local-search.duckdb"
            people_records = read_jsonl(run_dir / "records/people.records.jsonl")
            first = people_records[0]
            person_id = first["base_id"]
            state_path = Path(td) / "state.json"
            state = {
                "task_id": "local-hydrate-test",
                "query": "engineer founder",
                "steps": [
                    {
                        "id": "execute_role_search",
                        "status": "completed",
                        "output": {
                            "candidate_ids": [person_id],
                            "candidates": [
                                {
                                    "person_id": person_id,
                                    "position_id": first["id"],
                                    "matched_position_ids": [first["id"]],
                                    "position_title": first.get("position_title"),
                                    "base_score": 0.75,
                                    "score": 0.75,
                                    "vertical_sources": ["hybrid"],
                                }
                            ],
                        },
                    }
                ],
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            out_json = run_json([str(HYDRATE), "--state", str(state_path), "--local-db", str(db), "--no-compress-profiles"])
            self.assertEqual(out_json["requested"], 1)
            self.assertEqual(out_json["hydrated"], 1)
            self.assertEqual(out_json["profile_ids"], [person_id])
            self.assertEqual(out_json["source"]["backend"], "duckdb")
            self.assertEqual(out_json["source"]["profiles_table"], "local_profiles")
            profiles_path = Path(out_json["profiles_path"])
            llm_path = Path(out_json["llm_profiles_path"])
            self.assertTrue(profiles_path.exists())
            self.assertTrue(llm_path.exists())
            profiles = read_jsonl(profiles_path)
            self.assertEqual(profiles[0]["person_id"], person_id)
            self.assertIn("hybrid", profiles[0]["vertical_sources"])
            self.assertEqual(profiles[0]["matched_position_indexes"], [0])
            self.assertEqual(profiles[0]["base_score"], 0.75)


if __name__ == "__main__":
    unittest.main()
