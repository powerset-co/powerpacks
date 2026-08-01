from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

from packs.search.pipeline.models import Backend, ResolvedSources, RunnerCapabilities

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


company_harness = load_module("company_harness", ROOT / "packs/search/evals/run_company_search_harness.py")


class CompanySearchHarnessTests(unittest.TestCase):
    def test_company_cases_dry_run_are_typed_and_network_free(self) -> None:
        cases = company_harness.load_cases(company_harness.DEFAULT_CASES)
        self.assertGreaterEqual(len(cases), 8)
        results = [company_harness.dry_run_case(case) for case in cases]
        self.assertFalse(any(row["status"] == "fail" for row in results))
        self.assertTrue(all(row["search_spec"]["profile"] == "gtm" for row in results))
        self.assertTrue(all(row["search_spec"]["backend"] == "powerset" for row in results))

    def test_semantic_only_case_is_explicitly_unsupported(self) -> None:
        case = company_harness.CompanyCase(
            "semantic", "database companies",
            {"company_semantic_queries": ["companies building databases"]}, {},
        )
        result = company_harness.dry_run_case(case)
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("semantic-only", result["errors"][0])
        self.assertEqual(result["search_spec"]["rank_mode"], "semantic")

    def test_unrepresentable_required_company_scope_is_not_dropped(self) -> None:
        case = company_harness.CompanyCase(
            "city", "series a startups in sf",
            {"funding_stage_min": "series_a", "company_cities": ["San Francisco"]}, {},
        )
        result = company_harness.dry_run_case(case)
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["unsupported_fields"], ["company_cities"])

    def test_live_case_calls_runner_resolve_sources_directly(self) -> None:
        case = company_harness.CompanyCase("exact", "people at Meta", {"company_names": ["Meta"]}, {})
        capabilities = RunnerCapabilities(
            Backend.POWERSET, ("company_ids",), ("role",), False, True,
        )
        with mock.patch.object(company_harness, "TurboPufferSearchRunner") as runner_class:
            runner = runner_class.return_value
            runner.capabilities.return_value = capabilities
            runner.resolve_sources.return_value = ResolvedSources(
                company_ids=("meta-id",),
                records=({"source": "company", "input": "Meta", "required": True, "disposition": "resolved"},),
            )
            result = company_harness.live_case(case, set_id="set", operator_ids=("operator",))
        self.assertEqual(result["status"], "pass")
        runner.resolve_sources.assert_called_once()
        observed = runner.resolve_sources.call_args.args[0]
        self.assertEqual(observed.corpus.operator_ids, ("operator",))


if __name__ == "__main__":
    unittest.main()
