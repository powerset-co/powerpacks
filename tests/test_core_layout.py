import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CoreLayoutTests(unittest.TestCase):
    def test_user_facing_skills(self) -> None:
        powerset_pack = sorted(
            path.name for path in (ROOT / "packs/powerset/skills").iterdir() if path.is_dir()
        )
        self.assertEqual(powerset_pack, ["powerset", "powerset-login", "powerset-set"])
        search_pack = sorted(
            path.name for path in (ROOT / "packs/search/skills").iterdir() if path.is_dir()
        )
        self.assertEqual(
            search_pack, ["extract-search-query", "search-company", "search-network"]
        )
        messages_pack = sorted(
            path.name for path in (ROOT / "packs/messages/skills").iterdir() if path.is_dir()
        )
        self.assertEqual(messages_pack, ["import-contacts"])

    def test_no_legacy_add_skill_references_in_core_skill(self) -> None:
        text = (ROOT / "packs/search/skills/search-network/SKILL.md").read_text()
        self.assertNotIn("skills/add-", text)
        self.assertNotIn("view_search_results", text)
        self.assertIn("workflows/query-decomposition.md", text)

    def test_search_company_skill_uses_company_resolver(self) -> None:
        text = (ROOT / "packs/search/skills/search-company/SKILL.md").read_text()
        self.assertIn("resolve_companies", text)
        self.assertIn("company_semantic_queries", text)
        self.assertIn("investor_names", text)
        self.assertIn("company_sector_strategy", text)

    def test_pi_adapter_installs_skills(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skills_dir = Path(td) / "skills"
            proc = subprocess.run(
                [str(ROOT / "install.sh"), "pi", str(skills_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env={**os.environ, "POWERPACKS_SKIP_UV_SYNC": "1"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((skills_dir / "powerset" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "search-network" / "SKILL.md").exists())
            self.assertTrue((skills_dir / "powerset" / "powerpacks" / "packs").is_dir())
            self.assertTrue((skills_dir / "search-network" / "powerpacks" / "pyproject.toml").exists())
            self.assertIn(
                "turbopuffer",
                (skills_dir / "search-network" / "powerpacks" / "pyproject.toml").read_text(),
            )
            self.assertFalse(
                (skills_dir / "powerset" / "powerpacks" / "packs" / "powerset" / "skills" / "powerset" / "SKILL.md").exists()
            )

    def test_powerset_login_skill_uses_provisioning_primitives(self) -> None:
        text = (ROOT / "packs/powerset/skills/powerset-login/SKILL.md").read_text()
        self.assertIn("@powerset.co", text)
        # The setup checker is still the diagnosis entrypoint, but the skill's
        # user-facing contract should stay quiet.
        self.assertIn("packs/powerset/primitives/doctor/doctor.py run", text)
        self.assertIn("Updating your credentials...", text)
        self.assertIn("Credentials updated. Please restart Codex", text)
        self.assertIn("do not\nrun nested fix commands", text)
        # Per-user secret naming.
        self.assertIn("powerpacks-users-", text)
        # gcloud login is part of the interactive happy path.
        self.assertIn("gcloud auth login", text)
        # Maintainer onboarding command must be discoverable.
        self.assertIn("provision_user_secrets", text)
        # The setup classification must be documented.
        self.assertIn("fix_kind", text)

    def test_search_surface_documents_company_entrypoint(self) -> None:
        text = (ROOT / "packs/search/docs/search-surface.md").read_text()
        self.assertIn("/search-network <query>", text)
        self.assertIn("/search-company <query>", text)
        self.assertIn("company lookup", text.lower())

    def test_search_network_uses_extraction_skill(self) -> None:
        task = json.loads((ROOT / "packs/search/tasks/search-network.task.json").read_text())
        task_step_ids = [step["id"] for step in task["steps"]]
        self.assertIn("resolve_investors", task_step_ids)
        self.assertIn("count_candidates", task_step_ids)
        self.assertIn("execute_role_search", task_step_ids)
        self.assertNotIn("direct_count", task_step_ids)
        self.assertNotIn("direct_execute", task_step_ids)

        expand_step = next(step for step in task["steps"] if step["id"] == "expand_search_request")
        self.assertEqual(expand_step["skill"], "extract-search-query")

        text = (ROOT / "packs/search/skills/search-network/SKILL.md").read_text()
        self.assertIn("## Skill Composition", text)
        self.assertIn("extract-search-query", text)
        self.assertIn("search-company", text)
        self.assertIn("handoff", text.lower())
        self.assertIn("Do not hide query extraction inside eval or", text)

    def test_json_contracts_and_schemas_parse(self) -> None:
        roots = [
            ROOT / "packs/powerset/schemas",
            ROOT / "packs/search/contracts",
            ROOT / "packs/search/schemas",
            ROOT / "packs/search/tasks",
            ROOT / "packs/search/evals",
            ROOT / "packs/messages/schemas",
            ROOT / "packs/messages/tasks",
        ]
        for root in roots:
            with self.subTest(root=root):
                for path in root.rglob("*.json"):
                    json.loads(path.read_text())

    def test_import_contacts_documents_guided_flow(self) -> None:
        text = (ROOT / "packs/messages/skills/import-contacts/SKILL.md").read_text()
        self.assertIn("Ask once at the beginning", text)
        self.assertIn("Check iMessage access", text)
        self.assertIn("Link WhatsApp", text)
        self.assertIn("review_contacts_web", text)
        self.assertIn("review_research_web", text)

    def test_search_network_uses_single_execute_preview_gate(self) -> None:
        text = (ROOT / "packs/search/skills/search-network/SKILL.md").read_text()
        self.assertIn("execute`, `modify`, or `search only`", text)
        self.assertIn("--execute-approved", text)
        self.assertIn("without a second approval gate", text)

    def test_task_state_tracks_planned_steps_separately_from_execution_log(self) -> None:
        task_state = ROOT / "packs/search/primitives/task_state/task_state.py"
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

    def test_task_state_accepts_bare_planned_steps_array(self) -> None:
        task_state = ROOT / "packs/search/primitives/task_state/task_state.py"
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
                    json.dumps(["resolve_education", "count_candidates"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            state = json.loads(state_path.read_text())
            self.assertEqual(
                [step["id"] for step in state["planned_steps"]],
                ["resolve_education", "count_candidates"],
            )


if __name__ == "__main__":
    unittest.main()
