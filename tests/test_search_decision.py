"""Deterministic tests for the canonical $search SearchRoute contract."""

from __future__ import annotations
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import jsonschema

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "packs/search/skills/search/SKILL.md"
SCHEMA = ROOT / "packs/search/schemas/search-decision.schema.json"
CASES = ROOT / "packs/search/evals/decision/cases.json"
RUNNER = ROOT / "packs/search/evals/run_decision_eval.py"
_spec = importlib.util.spec_from_file_location("run_decision_eval", RUNNER)
rde = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(rde)
SCHEMA_DOC = json.loads(SCHEMA.read_text())
CASES_DOC = json.loads(CASES.read_text())
SKILL_TEXT = SKILL.read_text()
VALID = {"target": "engine", "profile": "gtm", "backend": "powerset", "reason": "people search"}


class TestDecisionSchema(unittest.TestCase):
    def test_valid_routes(self):
        for value in (
            VALID,
            {"target": "sql", "profile": None, "backend": None, "reason": "relational"},
            {"target": "contacts", "profile": None, "backend": None, "reason": "contacts"},
        ):
            jsonschema.validate(value, SCHEMA_DOC)

    def test_invalid_routes(self):
        bad = [
            {k: v for k, v in VALID.items() if k != "profile"},
            {**VALID, "extra": True},
            {**VALID, "target": "company"},
            {**VALID, "target": "sql"},
            {"target": "engine", "profile": None, "backend": None, "reason": "x"},
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(value, SCHEMA_DOC)


class TestCasesIntegrity(unittest.TestCase):
    def test_cases(self):
        ids = [c["id"] for c in CASES_DOC]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 64)
        for case in CASES_DOC:
            self.assertTrue(case["query"].strip())
            self.assertIn(case["target"], rde.ENUMS["target"])
            self.assertNotIn("surface", case)
            self.assertNotEqual(case["target"], "company")
            if case["target"] == "engine":
                self.assertIn(case["profile"], rde.ENUMS["profile"])
                self.assertIn(case["backend"], rde.ENUMS["backend"])
            else:
                self.assertIsNone(case["profile"])
                self.assertIsNone(case["backend"])
        people = next(c for c in CASES_DOC if c["id"] == "net-people-at-openai")
        self.assertEqual((people["target"], people["profile"]), ("engine", "gtm"))
        self.assertFalse(any(c["id"].startswith("co-") for c in CASES_DOC))
        self.assertNotIn("adv-find-candidates-bare", ids)


class TestSkillAndScorer(unittest.TestCase):
    def test_rules_and_pre_cutover_boundary(self):
        rules = rde.extract_rules(SKILL)
        for values in rde.ENUMS.values():
            for value in values:
                self.assertIn(f"`{value}`", rules)
        self.assertIn("SearchRoute", SKILL_TEXT)
        self.assertIn("Execute this search or modify it?", SKILL_TEXT)
        self.assertIn("packs.search.pipeline.search", SKILL_TEXT)
        self.assertIn("search_network_pipeline.py prepare", SKILL_TEXT)
        self.assertIn("deep_search_loop.py", SKILL_TEXT)
        self.assertIn("live `$search-company` surface", rules)
        self.assertIn("stop with `needs_input`", rules)
        self.assertIn("perform no retrieval", rules)
        self.assertIn("bare-person lookup", SKILL_TEXT)
        self.assertIn("email and phone return `unsupported_capability`", SKILL_TEXT)
        self.assertIn("additive,\nexplicit opt-in candidate path", SKILL_TEXT)
        self.assertIn("none is authorization for paid", SKILL_TEXT)

    def test_prompt_extract_score(self):
        self.assertIn('"target": ...', rde.build_prompt("RULES", {"query": "q", "env": {"remote_creds": False}}))
        raw = json.dumps(VALID)
        self.assertEqual(rde.extract_json(f"```json\n{raw}\n```")["profile"], "gtm")
        cases = [
            {"id": "a", "query": "q", "target": "engine", "profile": "gtm", "backend": "powerset"},
            {"id": "b", "query": "q", "target": "sql", "profile": None, "backend": None},
        ]
        report = rde.score(
            cases, {"a": (VALID, None), "b": ({"target": "engine", "profile": "gtm", "backend": "local"}, None)}
        )
        self.assertEqual(report["strict_accuracy"], 0.5)
        invalid = rde.score(
            [cases[1]], {"b": ({"target": "sql", "profile": "gtm", "backend": "local", "reason": "bad"}, None)}
        )
        self.assertEqual(invalid["errors"], 1)
        self.assertEqual(invalid["strict_accuracy"], 0)

    def test_stub_template_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "stub.py"
            stub.write_text('print(\'{"target":"engine","profile":"gtm","backend":"powerset","reason":"stub"}\')\n')
            report = Path(tmp) / "report.json"
            cp = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--command-template",
                    f"{sys.executable} {stub} {{prompt_path}}",
                    "--only",
                    "net-staff-backend-sf",
                    "--report",
                    str(report),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(json.loads(report.read_text())["strict_accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
