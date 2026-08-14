"""Offline checks for the dossier content-policy eval (no LLM calls).

Covers the scoring half of packs/ingestion/evals/run_content_policy_eval.py:
cases parse into typed Cases with compiling regexes, the scanner flags leaky
output and passes clean output, and the shipped fixtures stay obviously
synthetic per the repo privacy contract.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL_PATH = ROOT / "packs/ingestion/evals/run_content_policy_eval.py"

spec = importlib.util.spec_from_file_location("run_content_policy_eval", EVAL_PATH)
eval_mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = eval_mod  # dataclasses resolve annotations via sys.modules
spec.loader.exec_module(eval_mod)


class ContentPolicyEvalTests(unittest.TestCase):
    def setUp(self):
        self.categories, self.cases = eval_mod.load_cases(eval_mod.DEFAULT_CASES)

    def test_cases_parse_and_regexes_compile(self):
        self.assertGreaterEqual(len(self.cases), 3)
        self.assertIn("drugs", self.categories)
        self.assertIn("sexual", self.categories)
        for case in self.cases:
            self.assertTrue(case.messages)
            self.assertTrue(any(m["direction"] == "from_them" for m in case.messages))

    def test_fixtures_are_synthetic(self):
        for case in self.cases:
            for email in case.emails:
                self.assertIn("example.com", email, f"{case.full_name}: non-synthetic email")
            for phone in case.phones:
                self.assertTrue(phone.startswith("+1555"), f"{case.full_name}: non-synthetic phone")

    def test_scanner_flags_leaks_and_passes_clean(self):
        case = self.cases[0]  # Jordan Bravo: keeps bravo robotics/ceo + daughter born
        leaky = {"summary": "CEO of Bravo Robotics; daughter born; his weed dealer has a new strain"}
        result = eval_mod.scan_facts(leaky, self.categories, case)
        self.assertEqual(list(result["leaks"]), ["drugs"])
        self.assertFalse(result["passed"])

        clean = {"summary": "CEO of Bravo Robotics, closed a seed round; daughter born in June 2024"}
        result = eval_mod.scan_facts(clean, self.categories, case)
        self.assertEqual(result["leaks"], {})
        self.assertTrue(result["kept_professional"])
        self.assertTrue(result["kept_milestone"])
        self.assertTrue(result["passed"])

    def test_over_scrubbed_output_fails(self):
        # A prompt that deletes everything must not pass just by leaking nothing.
        case = self.cases[0]
        result = eval_mod.scan_facts({"summary": "A contact."}, self.categories, case)
        self.assertEqual(result["leaks"], {})
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
