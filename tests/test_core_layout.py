import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CoreLayoutTests(unittest.TestCase):
    def test_user_facing_skills(self) -> None:
        skills = sorted(path.name for path in (ROOT / "skills").iterdir() if path.is_dir())
        self.assertEqual(skills, ["search-company", "search-network"])

    def test_no_legacy_add_skill_references_in_core_skill(self) -> None:
        text = (ROOT / "skills" / "search-network" / "SKILL.md").read_text()
        self.assertNotIn("skills/add-", text)
        self.assertNotIn("view_search_results", text)
        self.assertIn("docs/workflows/query-decomposition.md", text)

    def test_search_company_skill_uses_company_resolver(self) -> None:
        text = (ROOT / "skills" / "search-company" / "SKILL.md").read_text()
        self.assertIn("resolve_companies", text)
        self.assertIn("company_semantic_queries", text)

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


if __name__ == "__main__":
    unittest.main()
