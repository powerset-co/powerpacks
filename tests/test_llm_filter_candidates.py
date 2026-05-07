import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FILTER_PY = ROOT / "packs/search/primitives/llm_filter_candidates/llm_filter_candidates.py"


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


if __name__ == "__main__":
    unittest.main()
