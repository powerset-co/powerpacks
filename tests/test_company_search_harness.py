import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


company_harness = load_module("company_harness", ROOT / "evals" / "run_company_search_harness.py")


class CompanySearchHarnessTests(unittest.TestCase):
    def test_company_cases_dry_run(self) -> None:
        cases = company_harness.load_cases(company_harness.DEFAULT_CASES)
        self.assertGreaterEqual(len(cases), 8)

        results = [company_harness.dry_run_case(case) for case in cases]
        failures = {row["id"]: row["errors"] for row in results if row["status"] != "pass"}

        self.assertEqual(failures, {})

    def test_company_cases_cover_public_lookup_surface(self) -> None:
        cases = company_harness.load_cases(company_harness.DEFAULT_CASES)
        payloads = [case.payload for case in cases]

        self.assertTrue(any(payload.get("company_names") for payload in payloads))
        self.assertTrue(any(payload.get("company_semantic_queries") for payload in payloads))
        self.assertTrue(any(payload.get("sector_types") for payload in payloads))
        self.assertTrue(any(payload.get("headcount_min") for payload in payloads))
        self.assertTrue(any(payload.get("funding_stage_min") for payload in payloads))
        self.assertTrue(any(payload.get("investor_names") for payload in payloads))


if __name__ == "__main__":
    unittest.main()
