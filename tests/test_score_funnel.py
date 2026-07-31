"""Unit tests for score_funnel (GT survival funnel) and the score_ground_truth_gaps NDCG/usage additions."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PRIM = ROOT / "packs" / "search" / "primitives" / "deep_search"
if str(PRIM) not in sys.path:
    sys.path.insert(0, str(PRIM))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, PRIM / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


sf = _load("score_funnel")
sg = _load("score_ground_truth_gaps")


def _reflect_gt(labels: list[tuple[str, str]]) -> dict:
    evidence = {person_id: (str(index + 1) * 64)[:64] for index, (person_id, _) in enumerate(labels)}
    return {
        "schema_version": "reflect.ground_truth.v1", "case_id": "synthetic-case", "case_hash": "a" * 64,
        "corpus_snapshot_hash": "b" * 64,
        "review_pool_evidence_hash": __import__("hashlib").sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "review_pool_evidence_hashes": evidence,
        "labels": [{"person_id": person_id, "evidence_hash": evidence[person_id], "decision": decision,
                    "reason_codes": ["synthetic"], "notes": "", "reviewer": "Synthetic Reviewer",
                    "reviewed_at": "2026-07-31T00:00:00Z"} for person_id, decision in labels],
        "finalized_at": "2026-07-31T00:00:00Z",
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _consensus(pid: str, name: str, *, inband=1, notout=1, gated=0, score=0.5, core=False) -> dict:
    return {"person_id": pid, "name": name, "inband_votes": inband, "notout_votes": notout,
            "gated_votes": gated, "mean_score": score, "core_met": core}


class FunnelFixture:
    """A synthetic run dir exercising every disposition. All people are fictional."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        union = [
            {"person_id": "p1", "name": "Jordan Bravo", "found_by": ["q00"]},
            {"person_id": "p2", "name": "Casey Delta", "found_by": ["q00", "q01"]},
            {"person_id": "p3", "name": "Sam Echo", "found_by": ["q01"]},
            {"person_id": "p4", "name": "Riley Fox", "found_by": ["q02"]},
            {"person_id": "p5", "name": "Alex Golf", "found_by": ["q00"]},
            {"person_id": "p6", "name": "Kai Hotel", "found_by": ["q03"]},
            {"person_id": "p7", "name": "Morgan India", "found_by": ["q03"]},
            {"person_id": "p9", "name": "Rowan Kilo", "found_by": ["q04"]},
            {"person_id": "p10", "name": "Devon Lima", "found_by": ["q02"]},
            {"person_id": "p20", "name": "Blake Nectar", "found_by": ["q00"]},
        ]
        _write_jsonl(self.dir / "master_union.jsonl", union)
        frontier = [{"candidate_id": p} for p in ("p1", "p2", "p3", "p4", "p5", "p6", "p7", "p10", "p20")]
        _write_jsonl(self.dir / "epoch0" / "candidate_frontier.full.jsonl", frontier)
        to_judge = [{"candidate_id": p} for p in ("p1", "p2", "p3", "p4", "p5", "p6", "p10", "p20")]
        _write_jsonl(self.dir / "epoch0" / "candidate_frontier.to_judge.jsonl", to_judge)
        judges = [{"candidate_id": p, "verdict": "strong"} for p in ("p1", "p2", "p3", "p4", "p5", "p6", "p11", "p20")]
        _write_jsonl(self.dir / "judges" / "loop.jsonl", judges)
        consensus = [
            _consensus("p1", "Jordan Bravo", score=0.8, core=True),
            _consensus("p2", "Casey Delta", score=0.55, core=False),
            _consensus("p3", "Sam Echo", inband=0, notout=0, gated=1, score=0.30),
            _consensus("p4", "Riley Fox", score=0.25, core=True),
            _consensus("p5", "Alex Golf", notout=0, score=0.50, core=True),
            _consensus("p6", "Kai Hotel", score=0.60, core=True),
            _consensus("p11", "Harper Mike", score=0.55, core=False),
            _consensus("p20", "Blake Nectar", score=0.9, core=True),
        ]
        (self.dir / "shortlist").mkdir()
        (self.dir / "shortlist" / "consensus.json").write_text(json.dumps(consensus), encoding="utf-8")
        ranked = [{"person_id": "p20", "name": "Blake Nectar"}, {"person_id": "p1", "name": "Jordan Bravo"}]
        (self.dir / "shortlist" / "ranked_final.json").write_text(json.dumps(ranked), encoding="utf-8")

    def gt_flat(self) -> Path:
        gt = [{"person_id": f"p{i}", "name": n} for i, n in
              [(1, "Jordan Bravo"), (2, "Casey Delta"), (3, "Sam Echo"), (4, "Riley Fox"),
               (5, "Alex Golf"), (6, "Kai Hotel"), (7, "Morgan India"), (8, "Quinn Juliet"),
               (9, "Rowan Kilo"), (10, "Devon Lima"), (11, "Harper Mike")]]
        path = self.dir / "gt.json"
        path.write_text(json.dumps(gt), encoding="utf-8")
        return path

    def run_main(self, gt_path: Path) -> dict:
        out = self.dir / "shortlist" / "funnel.json"
        argv = sys.argv
        sys.argv = ["score_funnel", "--run-dir", str(self.dir), "--ground-truth", str(gt_path)]
        try:
            sf.main()
        finally:
            sys.argv = argv
        return json.loads(out.read_text(encoding="utf-8"))


class TestScoreFunnelDispositions(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = FunnelFixture()
        self.payload = self.fx.run_main(self.fx.gt_flat())
        self.by_pid = {m["person_id"]: m["disposition"] for m in self.payload["gt_members"]}

    def test_every_disposition_is_reached(self) -> None:
        self.assertEqual(self.by_pid, {
            "p1": "shortlisted",
            "p2": "core_gated",
            "p3": "seniority_gated",
            "p4": "below_floor",           # ladder: floor beats core_met=True
            "p5": "judge_out",             # core met, score fine, verdict out
            "p6": "gate_passed_not_shortlisted",
            "p7": "triage_dropped",
            "p8": "never_sourced",
            "p9": "lost_at_frontier",
            "p10": "never_judged",
            "p11": "core_gated",           # closure: judged-only person is not "never_sourced"
        })

    def test_funnel_counts_and_line(self) -> None:
        stages = {s["stage"]: s["gt_survived"] for s in self.payload["funnel"]}
        self.assertEqual(stages, {"ground_truth": 11, "sourced": 10, "frontier": 9,
                                  "triage_survived": 8, "judged": 7, "shortlisted": 1})
        self.assertIn("11 GT", self.payload["funnel_line"])

    def test_probe_attribution(self) -> None:
        q00 = next(r for r in self.payload["probe_attribution"] if r["probe"] == "q00")
        self.assertEqual(q00["sourced"], 4)          # p1 p2 p5 p20
        self.assertEqual(q00["gt_sourced"], 3)       # p1 p2 p5
        self.assertEqual(q00["gt_exclusive_count"], 2)  # p1, p5 (p2 also found by q01)


class TestScoreFunnelTieredGt(unittest.TestCase):
    def test_tiers_resolve_by_name_and_removed_is_excluded(self) -> None:
        fx = FunnelFixture()
        tiered = {"tiers": {
            "A_would_move_fast": [{"name": "Jordan Bravo"}],
            "B_strong_interview": [{"name": "Casey Delta"}],
            "C_marginal_keep": [{"name": "Nobody Known"}],
            "REMOVED_from_gt": [{"name": "Sam Echo"}],
        }}
        path = fx.dir / "gt_tiers.json"
        path.write_text(json.dumps(tiered), encoding="utf-8")
        payload = fx.run_main(path)
        self.assertEqual(payload["gt_size"], 3)  # A + B + unresolved C; REMOVED excluded
        dispositions = {m["name"]: m["disposition"] for m in payload["gt_members"]}
        self.assertEqual(dispositions["Jordan Bravo"], "shortlisted")
        self.assertEqual(dispositions["Casey Delta"], "core_gated")
        self.assertEqual(dispositions["Nobody Known"], "unresolved_identity")

    def test_finalized_human_gt_excludes_ineligible_labels(self) -> None:
        fx = FunnelFixture()
        path = fx.dir / "human_gt.json"
        path.write_text(json.dumps(_reflect_gt([
            ("p1", "eligible_strong"), ("p2", "eligible_bench"), ("p3", "ineligible")
        ])), encoding="utf-8")
        payload = fx.run_main(path)
        self.assertEqual(payload["gt_size"], 2)
        self.assertEqual({row["person_id"] for row in payload["gt_members"]}, {"p1", "p2"})


class TestNdcg(unittest.TestCase):
    def test_perfect_order_is_one(self) -> None:
        gains = {"a": 3.0, "b": 2.0, "c": 1.0}
        self.assertEqual(sg.ndcg_at_k(gains, ["a", "b", "c"], 3), 1.0)

    def test_reversed_order_is_less(self) -> None:
        gains = {"a": 3.0, "b": 2.0, "c": 1.0}
        self.assertLess(sg.ndcg_at_k(gains, ["c", "b", "a"], 3), 1.0)

    def test_rank_sensitivity_where_recall_is_blind(self) -> None:
        gains = {"a": 1.0}
        best, worst = sg.ndcg_at_k(gains, ["a", "x", "y"], 3), sg.ndcg_at_k(gains, ["x", "y", "a"], 3)
        self.assertGreater(best, worst)
        self.assertEqual(sg.recall_at_k({"a"}, ["a", "x", "y"], 3), sg.recall_at_k({"a"}, ["x", "y", "a"], 3))

    def test_flat_gt_defaults_to_binary_gains(self) -> None:
        path = Path(tempfile.mkdtemp()) / "gt.json"
        path.write_text(json.dumps(
            [{"person_id": "a", "name": "Jordan Bravo"}, {"person_id": "b", "name": "Casey Delta", "tier": "A"}]),
            encoding="utf-8")
        gt, gains = sg.load_ground_truth(path, [])
        self.assertEqual(gains, {"a": 1.0, "b": 3.0})
        self.assertEqual(len(gt), 2)

    def test_finalized_human_gt_scores_only_eligible_resolved_labels(self) -> None:
        path = Path(tempfile.mkdtemp()) / "gt.json"
        path.write_text(json.dumps(_reflect_gt([
            ("strong", "eligible_strong"), ("bench", "eligible_bench"), ("out", "ineligible")
        ])), encoding="utf-8")
        gt, gains = sg.load_ground_truth(path, [])
        self.assertEqual([row["person_id"] for row in gt], ["strong", "bench"])
        self.assertEqual(gains, {"strong": 3.0, "bench": 2.0})

    def test_precision_uses_conventional_k_denominator(self) -> None:
        self.assertEqual(sg.precision_at_k({"positive"}, ["positive"], 25), 0.04)

    def test_forged_reflect_v1_is_rejected_by_both_standalone_scorers(self) -> None:
        path = Path(tempfile.mkdtemp()) / "forged.json"
        path.write_text(json.dumps({"schema_version": "reflect.ground_truth.v1", "labels": []}) + "\n")
        with self.assertRaises(ValueError):
            sg.load_ground_truth(path, [])
        with self.assertRaises(ValueError):
            sf.load_ground_truth(path, {})


class TestUsageCost(unittest.TestCase):
    def _usage_file(self) -> Path:
        d = Path(tempfile.mkdtemp())
        rows = [
            {"model": "test-model", "stage": "triage", "prompt_tokens": 1_000_000, "completion_tokens": 500_000, "reasoning_tokens": 0},
            {"model": "test-model", "stage": "judge", "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 1_000_000},
        ]
        path = d / "usage.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return path

    def test_priced_math(self) -> None:
        usage = self._usage_file()
        prices = usage.parent / "prices.json"
        prices.write_text(json.dumps({"test-model": {"input_per_1m": 1.0, "output_per_1m": 2.0}}), encoding="utf-8")
        with mock.patch.object(sg, "PRICES_PATH", prices):
            totals = sg.usage_cost(usage)
        # 1M prompt * $1 + 0.5M completion * $2 + 1M reasoning * $2 (falls back to output price)
        self.assertEqual(totals["cost_usd"], 4.0)
        self.assertTrue(totals["fully_priced"])
        self.assertEqual(totals["calls"], 2)

    def test_missing_price_table_reports_tokens_only(self) -> None:
        usage = self._usage_file()
        with mock.patch.object(sg, "PRICES_PATH", usage.parent / "absent.json"):
            totals = sg.usage_cost(usage)
        self.assertEqual(totals["cost_usd"], 0.0)
        self.assertFalse(totals["fully_priced"])
        self.assertEqual(totals["prompt_tokens"], 1_000_000)


if __name__ == "__main__":
    unittest.main()
