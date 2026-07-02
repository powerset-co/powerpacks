"""Deterministic CI tests for the $search decision contract.

The routing decision is made by a real agent (benchmarked on demand by
packs/search/evals/run_decision_eval.py — never in CI). These tests pin
everything that CAN be checked without spawning an agent: the decision.json
schema, the labeled case fixture's integrity/coverage, the SKILL.md contract
text (drift guard), and the eval runner's prompt/scoring plumbing.
"""
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
_spec.loader.exec_module(rde)

SCHEMA_DOC = json.loads(SCHEMA.read_text(encoding="utf-8"))
CASES_DOC = json.loads(CASES.read_text(encoding="utf-8"))
SKILL_TEXT = SKILL.read_text(encoding="utf-8")

VALID = {"surface": "people", "backend": "powerset", "depth": "fast", "reason": "plain people search"}


class TestDecisionSchema(unittest.TestCase):
    def test_valid_decision_validates(self):
        jsonschema.validate(VALID, SCHEMA_DOC)

    def test_missing_field_rejected(self):
        for field in ("surface", "backend", "depth", "reason"):
            bad = {k: v for k, v in VALID.items() if k != field}
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(bad, SCHEMA_DOC)

    def test_bad_enum_rejected(self):
        for field, value in (("surface", "network"), ("backend", "turbopuffer"), ("depth", "recruit")):
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate({**VALID, field: value}, SCHEMA_DOC)

    def test_extra_property_rejected(self):
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({**VALID, "route": "network"}, SCHEMA_DOC)


class TestCasesIntegrity(unittest.TestCase):
    def test_ids_unique_and_queries_nonempty(self):
        ids = [c["id"] for c in CASES_DOC]
        self.assertEqual(len(ids), len(set(ids)))
        for case in CASES_DOC:
            self.assertTrue(case["query"].strip(), case["id"])

    def test_labels_in_enums(self):
        for case in CASES_DOC:
            self.assertIn(case["surface"], rde.ENUMS["surface"], case["id"])
            self.assertIn(case["backend"], rde.ENUMS["backend"], case["id"])
            if case.get("depth") is not None:
                self.assertIn(case["depth"], rde.ENUMS["depth"], case["id"])
            for field in rde.FIELDS:
                for alt in case.get(f"acceptable_{field}") or []:
                    self.assertIn(alt, rde.ENUMS[field], case["id"])
            for key, value in (case.get("env") or {}).items():
                self.assertIn(key, {"local_db", "remote_creds"}, case["id"])
                self.assertIsInstance(value, bool, case["id"])

    def test_coverage_floors(self):
        surfaces = [c["surface"] for c in CASES_DOC]
        backends = [c["backend"] for c in CASES_DOC]
        depths = [c["depth"] for c in CASES_DOC if c.get("depth")]
        for surface in rde.ENUMS["surface"]:
            self.assertGreaterEqual(surfaces.count(surface), 8, surface)
        for backend in rde.ENUMS["backend"]:
            self.assertGreaterEqual(backends.count(backend), 6, backend)
        self.assertGreaterEqual(depths.count("deep"), 6)
        self.assertGreaterEqual(len(CASES_DOC), 64)

    def test_regression_cases_present(self):
        ids = {c["id"] for c in CASES_DOC}
        for required in ("reg-worked-with-tech", "reg-career-early", "reg-lookup-person",
                         "exp-powerset-staff-sf", "exp-local-pms-nyc", "env-both-default",
                         "dep-deep-local", "cross-jd-local"):
            self.assertIn(required, ids)


class TestSkillContract(unittest.TestCase):
    def test_rules_markers_present_and_ordered(self):
        self.assertIn(rde.RULES_START, SKILL_TEXT)
        self.assertIn(rde.RULES_END, SKILL_TEXT)
        self.assertLess(SKILL_TEXT.index(rde.RULES_START), SKILL_TEXT.index(rde.RULES_END))

    def test_rules_block_carries_all_enum_literals(self):
        rules = rde.extract_rules(SKILL)
        for values in rde.ENUMS.values():
            for value in values:
                self.assertIn(f"`{value}`", rules, value)

    def test_contract_strings(self):
        self.assertIn("decision.json", SKILL_TEXT)
        self.assertIn("Execute this search or modify it?", SKILL_TEXT)
        self.assertIn("Decide + record the search decision", SKILL_TEXT)
        self.assertIn("search_network_pipeline.py prepare", SKILL_TEXT)
        self.assertNotIn("primitives/route_query", SKILL_TEXT)


class TestPromptAndScorer(unittest.TestCase):
    def test_build_prompt_renders_env(self):
        prompt = rde.build_prompt("RULES", {"query": "q", "env": {"remote_creds": False}})
        self.assertIn("local DuckDB search index: present", prompt)
        self.assertIn("remote credentials: absent", prompt)
        self.assertIn("RULES", prompt)

    def test_extract_json_variants(self):
        raw = '{"surface": "people", "backend": "local", "depth": "fast", "reason": "r"}'
        self.assertEqual(rde.extract_json(raw)["backend"], "local")
        self.assertEqual(rde.extract_json(f"prose\n```json\n{raw}\n```\nmore")["backend"], "local")
        self.assertEqual(rde.extract_json(f"noise {raw} trailing")["surface"], "people")
        self.assertEqual(rde.extract_json("no json here"), {})

    def test_score_strict_lenient_and_errors(self):
        cases = [
            {"id": "a", "query": "q", "surface": "people", "backend": "powerset", "depth": "fast"},
            {"id": "b", "query": "q", "surface": "people", "backend": "powerset", "depth": "fast",
             "acceptable_depth": ["deep"]},
            {"id": "c", "query": "q", "surface": "company", "backend": "powerset", "depth": None},
            {"id": "d", "query": "q", "surface": "people", "backend": "local", "depth": "fast"},
        ]
        results = {
            "a": ({"surface": "people", "backend": "powerset", "depth": "fast"}, None),
            "b": ({"surface": "people", "backend": "powerset", "depth": "deep"}, None),
            "c": ({"surface": "sql", "backend": "powerset", "depth": "fast"}, None),
            "d": ({}, "timeout"),
        }
        report = rde.score(cases, results)
        self.assertEqual(report["cases"], 4)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["strict_accuracy"], 0.25)   # only a
        self.assertEqual(report["lenient_accuracy"], 0.5)   # a + b
        self.assertEqual({m["id"] for m in report["misses"]}, {"c", "d"})
        # depth is unlabeled for c -> not scored
        self.assertNotIn("depth", report["confusion"]["depth"].get("None", {}))


class TestRunnerEndToEnd(unittest.TestCase):
    def test_stub_template_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "stub.py"
            stub.write_text(
                "import sys, pathlib\n"
                "assert 'Query:' in pathlib.Path(sys.argv[1]).read_text()\n"
                "print('{\"surface\": \"people\", \"backend\": \"powerset\", "
                "\"depth\": \"fast\", \"reason\": \"stub\"}')\n",
                encoding="utf-8",
            )
            report_path = Path(tmp) / "report.json"
            cp = subprocess.run(
                [sys.executable, str(RUNNER),
                 "--command-template", f"{sys.executable} {stub} {{prompt_path}}",
                 "--only", "net-staff-backend-sf", "--only", "env-both-default",
                 "--report", str(report_path)],
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(cp.returncode, 0, cp.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["cases"], 2)
            self.assertEqual(report["strict_accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
