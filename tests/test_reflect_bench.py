"""Unit tests for the Reflect bench CLI (score/report/gate) and cost_report."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from test_score_funnel import FunnelFixture  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


bench = _load("bench", ROOT / "packs" / "search" / "reflect" / "bench.py")
cr = _load("cost_report", ROOT / "packs" / "search" / "reflect" / "cost_report.py")


class BenchSandbox:
    """Redirect bench's fixed state paths into a tmp dir so tests never touch repo state."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.results = self.tmp / "results"
        self.report = self.tmp / "report.json"
        self.suite = self.tmp / "suite"

    def patches(self):
        return [mock.patch.object(bench, "RESULTS_DIR", self.results),
                mock.patch.object(bench, "REPORT_PATH", self.report),
                mock.patch.object(bench, "SUITE_DIR", self.suite)]


def _score_args(fx: FunnelFixture, gt: Path) -> argparse.Namespace:
    return argparse.Namespace(run_dir=str(fx.dir), gt=str(gt), slug=None, ks="10",
                              score_threshold=0.40, usage_log=None)


class TestBenchScoreAndReport(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = BenchSandbox()
        self.fx = FunnelFixture()
        self.patchers = self.sandbox.patches()
        for p in self.patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patchers])

    def test_score_writes_result_json(self) -> None:
        rc = bench.cmd_score(_score_args(self.fx, self.fx.gt_flat()))
        self.assertEqual(rc, 0)
        result = json.loads((self.sandbox.results / self.fx.dir.name / "result.json").read_text())
        self.assertEqual(result["funnel"]["gt_size"], 11)
        self.assertEqual(result["funnel"]["dispositions"]["shortlisted"], 1)
        self.assertIsNotNone(result["gaps"])
        self.assertIn("ndcg@10", result["gaps"])
        self.assertIsNone(result["cost"])  # no usage.jsonl in the fixture

    def test_report_aggregates_and_lists_pending(self) -> None:
        bench.cmd_score(_score_args(self.fx, self.fx.gt_flat()))
        pending_dir = self.sandbox.suite / "some-unscored-jd"
        pending_dir.mkdir(parents=True)
        (pending_dir / "meta.json").write_text(json.dumps({"job_family": "eng-data", "gt": "pending"}))
        rc = bench.cmd_report(argparse.Namespace())
        self.assertEqual(rc, 0)
        report = json.loads(self.sandbox.report.read_text())
        self.assertEqual(len(report["jds"]), 1)
        self.assertEqual(report["gt_pending"], [{"slug": "some-unscored-jd", "job_family": "eng-data", "gt": "pending"}])
        fam = report["by_job_family"]["unknown"]  # fixture has no suite meta
        self.assertEqual(fam["jds"], 1)
        self.assertIsNotNone(fam["mean_recall"])


class TestBenchGate(unittest.TestCase):
    def _report(self, recall: float) -> Path:
        d = Path(tempfile.mkdtemp())
        path = d / "report.json"
        path.write_text(json.dumps({"jds": [{"slug": "jd-1", "overall_recall": recall, "cost_usd": 1.0}]}))
        return path

    def _gate_args(self, baseline: Path, current: Path, enforce: bool = False, **kw) -> argparse.Namespace:
        return argparse.Namespace(baseline=str(baseline), current=str(current), epsilon=0.02,
                                  min_recall=kw.get("min_recall"), max_cost=kw.get("max_cost"), enforce=enforce)

    def test_identical_reports_pass(self) -> None:
        base = self._report(0.8)
        self.assertEqual(bench.cmd_gate(self._gate_args(base, base)), 0)

    def test_regression_warns_but_exits_zero_by_default(self) -> None:
        rc = bench.cmd_gate(self._gate_args(self._report(0.8), self._report(0.5)))
        self.assertEqual(rc, 0)  # warn-only

    def test_regression_fails_with_enforce(self) -> None:
        rc = bench.cmd_gate(self._gate_args(self._report(0.8), self._report(0.5), enforce=True))
        self.assertEqual(rc, 1)

    def test_floors(self) -> None:
        base = self._report(0.8)
        rc = bench.cmd_gate(self._gate_args(base, base, enforce=True, min_recall=0.9))
        self.assertEqual(rc, 1)
        rc = bench.cmd_gate(self._gate_args(base, base, enforce=True, max_cost=0.5))
        self.assertEqual(rc, 1)

    def test_missing_jd_in_current_fails(self) -> None:
        base = self._report(0.8)
        empty = Path(tempfile.mkdtemp()) / "report.json"
        empty.write_text(json.dumps({"jds": []}))
        self.assertEqual(bench.cmd_gate(self._gate_args(base, empty, enforce=True)), 1)


class TestCostReport(unittest.TestCase):
    def test_by_stage_and_model_with_unpriced_fallback(self) -> None:
        rows = [
            {"model": "priced-model", "stage": "triage", "prompt_tokens": 1_000_000, "completion_tokens": 0, "reasoning_tokens": 0, "latency_ms": 100},
            {"model": "priced-model", "stage": "judge", "prompt_tokens": 0, "completion_tokens": 500_000, "reasoning_tokens": 0, "latency_ms": 200},
            {"model": "mystery-model", "stage": "judge", "prompt_tokens": 10, "completion_tokens": 10, "reasoning_tokens": 0, "latency_ms": 50},
        ]
        prices = {"priced-model": {"input_per_1m": 1.0, "output_per_1m": 2.0}}
        report = cr.build_report(rows, prices)
        self.assertEqual(report["totals"]["calls"], 3)
        self.assertEqual(report["totals"]["cost_usd"], 2.0)  # $1 prompt + $1 completion
        self.assertFalse(report["totals"]["fully_priced"])   # mystery-model is unpriced
        self.assertEqual(report["by_stage"]["triage"]["cost_usd"], 1.0)
        self.assertTrue(report["by_stage"]["triage"]["fully_priced"])
        self.assertFalse(report["by_stage"]["judge"]["fully_priced"])
        self.assertEqual(report["by_model"]["mystery-model"]["cost_usd"], 0.0)
        self.assertEqual(report["latency_total_ms"], 350)

    def test_committed_price_table_parses_and_nulls_are_unpriced(self) -> None:
        prices = cr.load_prices(cr.DEFAULT_PRICES_PATH)
        self.assertIn("gpt-4.1-mini", prices)
        row = {"model": "gpt-4.1-mini", "prompt_tokens": 1_000_000, "completion_tokens": 0, "reasoning_tokens": 0}
        self.assertAlmostEqual(cr.row_cost_usd(row, prices), 0.4)
        self.assertIsNone(cr.row_cost_usd({"model": "gpt-5.2", "prompt_tokens": 10}, prices))  # delisted, kept null


if __name__ == "__main__":
    unittest.main()
