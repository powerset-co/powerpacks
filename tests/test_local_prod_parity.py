from __future__ import annotations

import argparse
import importlib.util
import unittest
from pathlib import Path

from packs.search.pipeline.frontier import CandidateFrontier, CandidateRecord, StageResult
from packs.search.pipeline.models import Backend, LocalCorpus, PowersetCorpus

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packs/search/evals/run_local_prod_parity.py"


def load_module():
    spec = importlib.util.spec_from_file_location("run_local_prod_parity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


parity = load_module()


class LocalProdParityTests(unittest.TestCase):
    def test_choose_personal_set_prefers_alias_then_count(self) -> None:
        sets = [
            {"id": "wrong", "name": "Someone Else", "is_personal": True, "person_count": 9320},
            {"id": "zero", "name": "Jordan Bravo Connections", "is_personal": True, "person_count": 0},
            {"id": "right", "name": "Jordan Bravo Connections", "is_personal": True, "person_count": 9424},
        ]
        selected = parity.choose_personal_set(sets, slug="jordan", aliases=["jordan bravo"], local_count=9320)
        self.assertEqual(selected["id"], "right")
        self.assertEqual(selected["_selection_reason"], "alias_and_count")

    def test_one_logical_intent_binds_local_and_exact_remote_corpora(self) -> None:
        intent = parity.default_intent(Path("/tmp/local.duckdb"), "engineers", limit=50)
        local = parity.replace(intent, backend=Backend.LOCAL, corpus=LocalCorpus("/tmp/other.duckdb"))
        remote = parity.replace(
            intent,
            backend=Backend.POWERSET,
            corpus=PowersetCorpus("set-id", ("operator-id",)),
        )
        self.assertEqual(parity.intent_dict(local), parity.intent_dict(remote))
        self.assertEqual(remote.corpus.operator_ids, ("operator-id",))

    def test_compare_stage_results_reports_frontiers_counts_and_filters(self) -> None:
        local = StageResult(
            "gtm", "completed", CandidateFrontier.merge([CandidateRecord("a"), CandidateRecord("b")]),
            counts={"eligible_pool": 3}, hard_filter_validation={"violation_count": 0},
        )
        prod = StageResult(
            "gtm", "completed", CandidateFrontier.merge([CandidateRecord("b"), CandidateRecord("c")]),
            counts={"eligible_pool": 4}, hard_filter_validation={"violation_count": 1},
        )
        comparison = parity.compare_stage_results(local, prod)
        self.assertEqual(comparison["overlap_count"], 1)
        self.assertEqual(comparison["local_frontier"]["output_count"], 2)
        self.assertEqual(comparison["counts"]["eligible_pool"], {"local": 3, "prod": 4})
        self.assertFalse(comparison["hard_filter_validation"]["equal"])

    def test_execute_search_uses_typed_searchspec_contract(self) -> None:
        intent = parity.default_intent(Path("/tmp/local.duckdb"), "engineers", limit=10)
        seen = []

        def search(spec):
            seen.append(spec)
            return StageResult("gtm", "completed_empty", CandidateFrontier.merge([]))

        execution = parity.execute_search(intent, search=search)
        self.assertEqual(execution["status"], "ok")
        self.assertIs(seen[0], intent)

    def test_explicit_operator_scope_is_required_before_discovery(self) -> None:
        args = argparse.Namespace(
            operator=[], operators="operator-a", set_id=[], operator_id=[], mcp_url="https://example.invalid",
            timeout=1, output_dir=None, spec_json=None, query="q", max_results=10,
            min_precision=0.95, min_recall=0.95,
        )
        with self.assertRaisesRegex(parity.ParityError, "explicit remote scope"):
            parity.run(args)

    def test_compare_ids_reports_precision_and_recall(self) -> None:
        comparison = parity.compare_ids(["a", "b", "c"], ["b", "c", "d", "e"])
        self.assertEqual(comparison["overlap_count"], 2)
        self.assertEqual(comparison["local_precision_vs_prod"], 0.6667)
        self.assertEqual(comparison["local_recall_vs_prod"], 0.5)


if __name__ == "__main__":
    unittest.main()
