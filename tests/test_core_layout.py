import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CoreLayoutTests(unittest.TestCase):
    def test_user_facing_skills(self) -> None:
        skills = sorted(path.name for path in (ROOT / "skills").iterdir() if path.is_dir())
        self.assertEqual(skills, ["extract-search-query", "search-company", "search-network"])

    def test_no_legacy_add_skill_references_in_core_skill(self) -> None:
        text = (ROOT / "skills" / "search-network" / "SKILL.md").read_text()
        self.assertNotIn("skills/add-", text)
        self.assertNotIn("view_search_results", text)
        self.assertIn("docs/workflows/query-decomposition.md", text)

    def test_search_company_skill_uses_company_resolver(self) -> None:
        text = (ROOT / "skills" / "search-company" / "SKILL.md").read_text()
        self.assertIn("resolve_companies", text)
        self.assertIn("company_semantic_queries", text)

    def test_search_network_uses_extraction_skill(self) -> None:
        task = json.loads((ROOT / "tasks" / "search-network.task.json").read_text())
        task_step_ids = [step["id"] for step in task["steps"]]
        self.assertIn("resolve_investors", task_step_ids)
        self.assertIn("count_candidates", task_step_ids)
        self.assertIn("execute_role_search", task_step_ids)
        self.assertNotIn("direct_count", task_step_ids)
        self.assertNotIn("direct_execute", task_step_ids)

        expand_step = next(step for step in task["steps"] if step["id"] == "expand_search_request")
        self.assertEqual(expand_step["skill"], "extract-search-query")

        text = (ROOT / "skills" / "search-network" / "SKILL.md").read_text()
        self.assertIn("## Skill Composition", text)
        self.assertIn("extract-search-query", text)
        self.assertIn("search-company", text)
        self.assertIn("handoff", text.lower())
        self.assertIn("Do not hide query extraction inside eval or", text)

    def test_public_primitives_exclude_host_adapter_tools(self) -> None:
        text = (ROOT / "primitives" / "README.md").read_text()
        self.assertNotIn("nanoclaw_plan_harness", text)
        self.assertNotIn("view_search_results", text)
        self.assertIn("Host-specific", text)

    def test_json_contracts_and_schemas_parse(self) -> None:
        roots = [
            ROOT / "contracts",
            ROOT / "schemas",
            ROOT / "tasks",
            ROOT / "evals",
        ]
        for root in roots:
            with self.subTest(root=root):
                for path in root.rglob("*.json"):
                    json.loads(path.read_text())

    def test_search_network_offers_rerank_approval_mode(self) -> None:
        text = (ROOT / "skills" / "search-network" / "SKILL.md").read_text()
        self.assertIn("search only", text)
        self.assertIn("rerank", text)
        self.assertIn("--execution-mode rerank", text)

    def test_task_state_tracks_planned_steps_separately_from_execution_log(self) -> None:
        task_state = ROOT / "primitives" / "task_state" / "task_state.py"
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "run.json"
            subprocess.run(
                [
                    sys.executable,
                    str(task_state),
                    "init",
                    "--query",
                    "software engineers in sf",
                    "--out",
                    str(state_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(task_state),
                    "request-approval",
                    "--state",
                    str(state_path),
                    "--reason",
                    "test",
                    "--proposed-next-step",
                    "run planned steps",
                    "--plan-json",
                    json.dumps({"planned_steps": ["resolve_education", "count_candidates"]}),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(task_state),
                    "record-step",
                    "--state",
                    str(state_path),
                    "--step-id",
                    "resolve_education",
                    "--output-json",
                    "{}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            state = json.loads(state_path.read_text())
            self.assertEqual([step["id"] for step in state["steps"]], ["resolve_education"])
            planned_by_id = {step["id"]: step for step in state["planned_steps"]}
            self.assertEqual(planned_by_id["resolve_education"]["status"], "completed")
            self.assertIn("completed_at", planned_by_id["resolve_education"])
            self.assertEqual(planned_by_id["count_candidates"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
