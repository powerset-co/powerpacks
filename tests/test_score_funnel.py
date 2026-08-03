"""Unit tests for score_funnel (GT survival funnel) and the score_ground_truth_gaps NDCG/usage additions."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.search.pipeline.frontier import CandidateFrontier, CandidateRecord, ProbeMatch
from packs.search.pipeline.stage_membership import (
    SCHEMA_VERSION,
    SearchStageMembership,
    StageMembershipRecord,
)

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


def _frontier_record(pid: str, name: str, disposition: str, *, score: float = 0.5) -> CandidateRecord:
    eligible = disposition in {"shortlisted", "gate_passed_not_shortlisted"}
    gates = {
        "location": True,
        "core_groups": disposition not in {"core_gated"},
        "seniority_track": disposition not in {"seniority_gated"},
        "founder_c_suite_hireable": True,
        "categorical_not_out": disposition not in {"judge_out"},
        "score_floor": disposition not in {"below_floor"},
        "shortlist": eligible,
        "sendable": disposition == "shortlisted",
    }
    judge = {"status": "error"} if disposition == "never_judged" else {
        "status": "judged", "score": score, "seniority_fit": "ideal",
    }
    return CandidateRecord(
        pid,
        hydrated_profile={"name": name},
        hydration_disposition="hydrated",
        judge=judge,
        deterministic_score=score,
        hard_filter_evidence={"disposition": "accepted", "violations": [], "unknowns": []},
        deterministic_gates=gates if disposition != "never_judged" else {},
    )


class FunnelFixture:
    """A synthetic run dir exercising every disposition. All people are fictional."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        rows = [
            ("p1", "Jordan Bravo", ("q00",), "shortlisted", True, True, "judged"),
            ("p2", "Casey Delta", ("q00", "q01"), "core_gated", True, True, "judged"),
            ("p3", "Sam Echo", ("q01",), "seniority_gated", True, True, "judged"),
            ("p4", "Riley Fox", ("q02",), "below_floor", True, True, "judged"),
            ("p5", "Alex Golf", ("q00",), "judge_out", True, True, "judged"),
            ("p6", "Kai Hotel", ("q03",), "gate_passed_not_shortlisted", True, True, "judged"),
            ("p7", "Morgan India", ("q03",), "triage_dropped", True, False, "not_run"),
            ("p9", "Rowan Kilo", ("q04",), "hydration_missing", False, False, "not_run"),
            ("p10", "Devon Lima", ("q02",), "never_judged", True, True, "error"),
            ("p11", "Harper Mike", ("q05",), "core_gated", True, True, "judged"),
            ("p20", "Blake Nectar", ("q00",), "shortlisted", True, True, "judged"),
        ]
        memberships = tuple(
            StageMembershipRecord(
                person_id=pid,
                name=name,
                found_by=tuple(ProbeMatch("synthetic", index + 1, probe, "synthetic", 0.5) for index, probe in enumerate(found_by)),
                hydrated=disposition != "hydration_missing",
                hard_filter_passed=hard_filter,
                triage_survived=triage,
                judge_status=judge_status,
                shortlisted=disposition == "shortlisted",
                disposition=disposition,
                detail="synthetic",
            )
            for pid, name, found_by, disposition, hard_filter, triage, judge_status in rows
        )
        membership = SearchStageMembership(
            SCHEMA_VERSION, "completed_capped", 0, len(memberships), 0.4, 0.55, 2, memberships
        )
        (self.dir / "stage-membership.json").write_text(json.dumps(membership.to_dict()) + "\n")
        dispositions = {pid: disposition for pid, _name, _found, disposition, _hard, triage, _judge in rows if triage}
        names = {pid: name for pid, name, *_rest in rows}
        order = ("p20", "p1", "p2", "p3", "p4", "p5", "p6", "p10", "p11")
        frontier_rows = tuple(
            _frontier_record(
                pid,
                names[pid],
                dispositions[pid],
                score=0.9 if pid == "p20" else 0.8 if pid == "p1" else 0.25
                if dispositions[pid] == "below_floor" else 0.5,
            )
            for pid in order
        )
        frontier = CandidateFrontier(frontier_rows, len(frontier_rows), len(frontier_rows), 100, False)
        (self.dir / "candidate-frontier.json").write_text(json.dumps(frontier.to_dict()) + "\n")

    def gt_flat(self) -> Path:
        gt = _reflect_gt([(f"p{i}", "eligible_bench") for i in range(1, 12)])
        path = self.dir / "gt.json"
        path.write_text(json.dumps(gt), encoding="utf-8")
        return path

    def run_main(self, gt_path: Path) -> dict:
        out = self.dir / "reflect" / "funnel.json"
        argv = sys.argv
        sys.argv = [
            "score_funnel",
            "--stage-membership", str(self.dir / "stage-membership.json"),
            "--candidate-frontier", str(self.dir / "candidate-frontier.json"),
            "--ground-truth", str(gt_path),
        ]
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
            "p9": "hydration_missing",
            "p10": "never_judged",
            "p11": "core_gated",           # closure: judged-only person is not "never_sourced"
        })

    def test_funnel_counts_and_line(self) -> None:
        stages = {s["stage"]: s["gt_survived"] for s in self.payload["funnel"]}
        self.assertEqual(stages, {"ground_truth": 11, "sourced": 10, "hydrated": 9,
                                  "hard_filter_survived": 9, "triage_survived": 8,
                                  "judged": 7, "shortlisted": 1})
        self.assertIn("11 GT", self.payload["funnel_line"])

    def test_probe_attribution(self) -> None:
        q00 = next(r for r in self.payload["probe_attribution"] if r["probe"] == "q00")
        self.assertEqual(q00["sourced"], 4)          # p1 p2 p5 p20
        self.assertEqual(q00["gt_sourced"], 3)       # p1 p2 p5
        self.assertEqual(q00["gt_exclusive_count"], 2)  # p1, p5 (p2 also found by q01)

    def test_membership_name_restores_ground_truth_name(self) -> None:
        p1 = next(row for row in self.payload["gt_members"] if row["person_id"] == "p1")
        self.assertEqual(p1["name"], "Jordan Bravo")

    def test_stage_membership_disposition_is_exact_first_rule_wins(self) -> None:
        source = next(row for row in SearchStageMembership.read(self.fx.dir / "stage-membership.json").candidates
                      if row.person_id == "p9")
        from dataclasses import replace

        with self.assertRaisesRegex(ValueError, "disposition must be hydration_missing"):
            replace(source, disposition="hard_filter_quarantined")

    def test_ranked_membership_must_match_canonical_candidate(self) -> None:
        path = self.fx.dir / "candidate-frontier.json"
        document = json.loads(path.read_text())
        document["candidates"][0]["hydration_disposition"] = "missing_profile"
        path.write_text(json.dumps(document) + "\n")
        with self.assertRaisesRegex(ValueError, "canonical stage membership: p20"):
            self.fx.run_main(self.fx.gt_flat())

    def test_shortlist_must_equal_eligible_frontier_prefix(self) -> None:
        path = self.fx.dir / "stage-membership.json"
        document = json.loads(path.read_text())
        document["frontier_limit"] = 1
        path.write_text(json.dumps(document) + "\n")
        with self.assertRaisesRegex(ValueError, "eligible ranked prefix"):
            self.fx.run_main(self.fx.gt_flat())

    def test_contradictory_shortlist_and_sendable_gates_are_rejected(self) -> None:
        for gate in ("shortlist", "sendable"):
            fx = FunnelFixture()
            path = fx.dir / "candidate-frontier.json"
            document = json.loads(path.read_text())
            document["candidates"][0]["deterministic_gates"][gate] = False
            path.write_text(json.dumps(document) + "\n")
            with self.assertRaisesRegex(ValueError, f"{gate} gate contradicts"):
                fx.run_main(fx.gt_flat())


class TestScoreFunnelGroundTruthContract(unittest.TestCase):
    def test_legacy_tiered_ground_truth_is_rejected(self) -> None:
        fx = FunnelFixture()
        tiered = {"tiers": {
            "A_would_move_fast": [{"name": "Jordan Bravo"}],
            "B_strong_interview": [{"name": "Casey Delta"}],
            "C_marginal_keep": [{"name": "Nobody Known"}],
            "REMOVED_from_gt": [{"name": "Sam Echo"}],
        }}
        path = fx.dir / "gt_tiers.json"
        path.write_text(json.dumps(tiered), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "reflect.ground_truth.v1"):
            fx.run_main(path)

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

    def test_legacy_flat_gt_is_rejected(self) -> None:
        path = Path(tempfile.mkdtemp()) / "gt.json"
        path.write_text(json.dumps(
            [{"person_id": "a", "name": "Jordan Bravo"}, {"person_id": "b", "name": "Casey Delta", "tier": "A"}]),
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "reflect.ground_truth.v1"):
            sg.load_ground_truth(path, [])

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
            sf.load_ground_truth(path)

    def test_duplicate_and_truncated_frontiers_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate person IDs"):
            CandidateFrontier((_frontier_record("p", "Jordan Bravo", "shortlisted"),) * 2, 2, 2, 2, False)
        fx = FunnelFixture()
        path = fx.dir / "candidate-frontier.json"
        document = json.loads(path.read_text())
        document["truncated"] = True
        path.write_text(json.dumps(document) + "\n")
        with self.assertRaisesRegex(ValueError, "truncated candidate frontier"):
            fx.run_main(fx.gt_flat())


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
