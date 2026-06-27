"""Unit tests for the $recruit consensus + ground-truth-gap primitives (pure functions)."""
from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIM = ROOT / "packs" / "search" / "primitives" / "recruit"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, PRIM / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


jc = _load("judge_consensus")
sg = _load("score_ground_truth_gaps")
dv = _load("diversify_probe_bm25")

_sb_spec = importlib.util.spec_from_file_location(
    "seniority_bands", ROOT / "packs" / "search" / "primitives" / "shared" / "seniority_bands.py"
)
sb = importlib.util.module_from_spec(_sb_spec)
_sb_spec.loader.exec_module(sb)  # type: ignore[union-attr]


class TestPreserveSemanticQuery(unittest.TestCase):
    def test_preserves_raw_query_and_keeps_bm25_and_filters(self):
        payload = {"role_search_filters": {
            "semantic_query": "Engineers specializing in distributed systems design and implementation",
            "bm25_queries": ["distributed systems engineer", "scheduler engineer"],
            "seniority_bands": ["staff"], "cities": ["San Francisco"],
        }}
        raw = "Distributed systems engineer who built admission control and bin packing for a GPU cluster"
        out = sb.pin_payload_semantic_query(payload, raw)
        f = out["role_search_filters"]
        self.assertEqual(f["semantic_query"], raw)          # raw query becomes the vector
        self.assertTrue(f["semantic_query_preserved"])
        self.assertEqual(f["bm25_queries"], ["distributed systems engineer", "scheduler engineer"])  # bm25 kept
        self.assertEqual(f["seniority_bands"], ["staff"])   # filters kept
        self.assertEqual(f["cities"], ["San Francisco"])
        self.assertTrue(any("semantic_query preserved" in n for n in out["notes"]))

    def test_does_not_mutate_input(self):
        payload = {"role_search_filters": {"semantic_query": "orig", "bm25_queries": ["x"]}}
        sb.pin_payload_semantic_query(payload, "new")
        self.assertEqual(payload["role_search_filters"]["semantic_query"], "orig")


class TestJudgeConsensus(unittest.TestCase):
    def _judges(self):
        # 3 judges; A is unanimous strong, B gated by 2/3 (too_senior), C is a 2/3 in-band split.
        return {
            "j1": [
                {"person_id": "A", "name": "Ada", "in_band": True, "verdict": "top_tier", "score": 0.9, "seniority_fit": "in_band"},
                {"person_id": "B", "name": "Boss", "in_band": False, "verdict": "out", "score": 0.2, "seniority_fit": "too_senior"},
                {"person_id": "C", "name": "Cam", "in_band": True, "verdict": "high_potential", "score": 0.6, "seniority_fit": "in_band"},
            ],
            "j2": [
                {"person_id": "A", "name": "Ada", "in_band": True, "verdict": "high_potential", "score": 0.8, "seniority_fit": "in_band"},
                {"person_id": "B", "name": "Boss", "in_band": False, "verdict": "out", "score": 0.3, "seniority_fit": "too_senior"},
                {"person_id": "C", "name": "Cam", "in_band": True, "verdict": "high_potential", "score": 0.5, "seniority_fit": "in_band"},
            ],
            "j3": [
                {"person_id": "A", "name": "Ada", "in_band": True, "verdict": "top_tier", "score": 1.0, "seniority_fit": "in_band"},
                {"person_id": "B", "name": "Boss", "in_band": True, "verdict": "high_potential", "score": 0.7, "seniority_fit": "in_band"},
                {"person_id": "C", "name": "Cam", "in_band": False, "verdict": "out", "score": 0.3, "seniority_fit": "wrong_track"},
            ],
        }

    def test_strong_requires_majority_inband_and_notout(self):
        rows, strong = jc.build_consensus(self._judges(), {}, min_inband_votes=2, min_notout_votes=2)
        ids = [r["person_id"] for r in strong]
        self.assertIn("A", ids)          # 3/3 in-band, 3/3 not-out
        self.assertIn("C", ids)          # 2/3 in-band, 2/3 not-out
        self.assertNotIn("B", ids)       # only 1/3 in-band -> gated out
        self.assertEqual(ids[0], "A")    # ranked first (highest mean score)

    def test_consensus_fields(self):
        rows, _ = jc.build_consensus(self._judges(), {}, min_inband_votes=2, min_notout_votes=2)
        a = next(r for r in rows if r["person_id"] == "A")
        self.assertEqual(a["inband_votes"], 3)
        self.assertEqual(a["notout_votes"], 3)
        self.assertEqual(a["n_judges"], 3)
        self.assertAlmostEqual(a["mean_score"], 0.9, places=3)
        b = next(r for r in rows if r["person_id"] == "B")
        self.assertEqual(b["gated_votes"], 2)  # two judges said too_senior

    def test_meta_enriches_rows(self):
        meta = {"A": {"person_id": "A", "name": "Ada", "current_company": "Acme", "found_by": ["routing", "scheduler"]}}
        rows, _ = jc.build_consensus(self._judges(), meta, min_inband_votes=2, min_notout_votes=2)
        a = next(r for r in rows if r["person_id"] == "A")
        self.assertEqual(a["current_company"], "Acme")
        self.assertEqual(a["found_by"], ["routing", "scheduler"])


class TestScoreGroundTruthGaps(unittest.TestCase):
    def test_recall_precision_and_missed(self):
        gt = {"x", "y", "z"}
        epoch = ["x", "q", "y"]  # finds x,y ; misses z ; q is net-new
        self.assertEqual(sg.recall_at_k(gt, epoch, 10), round(2 / 3, 4))
        self.assertEqual(sg.precision_at_k(gt, epoch, 3), round(2 / 3, 4))
        self.assertEqual(sg.recall_at_k(gt, epoch, 1), round(1 / 3, 4))

    def test_ranked_ids_honors_rank_then_score(self):
        recs = [{"person_id": "a", "rank": 2}, {"person_id": "b", "rank": 1}]
        self.assertEqual(sg.ranked_ids(recs), ["b", "a"])
        recs2 = [{"person_id": "a", "score": 0.1}, {"person_id": "b", "score": 0.9}]
        self.assertEqual(sg.ranked_ids(recs2), ["b", "a"])

    def test_load_records_json_and_jsonl(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.json").write_text(json.dumps([{"person_id": "a"}]), encoding="utf-8")
        (d / "b.jsonl").write_text('{"person_id": "a"}\n{"person_id": "b"}\n', encoding="utf-8")
        self.assertEqual(len(sg.load_records(d / "a.json")), 1)
        self.assertEqual(len(sg.load_records(d / "b.jsonl")), 2)

    def test_main_writes_gaps_and_convergence(self):
        d = Path(tempfile.mkdtemp())
        gt = [{"person_id": "x", "name": "X"}, {"person_id": "y", "name": "Y"}, {"person_id": "z", "name": "Z"}]
        (d / "gt.json").write_text(json.dumps(gt), encoding="utf-8")
        (d / "epoch.jsonl").write_text('{"person_id":"x","rank":1}\n{"person_id":"y","rank":2}\n', encoding="utf-8")
        import sys
        argv = sys.argv
        sys.argv = ["score", "--ground-truth", str(d / "gt.json"), "--epoch-candidates", str(d / "epoch.jsonl"),
                    "--epoch-dir", str(d / "epochs" / "epoch-01"), "--epoch-label", "epoch-01",
                    "--convergence-csv", str(d / "convergence.csv"), "--ks", "10"]
        try:
            sg.main()
        finally:
            sys.argv = argv
        gaps = json.loads((d / "epochs" / "epoch-01" / "gaps.json").read_text())
        self.assertEqual(gaps["overall_recall"], round(2 / 3, 4))
        self.assertEqual(gaps["missed_count"], 1)
        self.assertEqual(gaps["missed"][0]["person_id"], "z")
        with (d / "convergence.csv").open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["epoch"], "epoch-01")


class TestDiversifyProbeBm25(unittest.TestCase):
    def _p(self, *bm25):
        return {"role_search_filters": {"semantic_query": "x", "bm25_queries": list(bm25)}}

    def test_shared_terms_are_high_df_lead_terms(self):
        payloads = [
            self._p("distributed systems engineer", "scheduler engineer"),
            self._p("distributed systems engineer", "routing engineer"),
            self._p("distributed systems engineer", "consensus engineer"),
            self._p("distributed systems engineer", "observability engineer"),
        ]
        shared = dv.shared_bm25_terms(payloads, df_threshold=0.35, min_df=1)
        self.assertIn("distributed systems engineer", shared)   # in all 4
        self.assertNotIn("scheduler engineer", shared)          # distinctive, in 1

    def test_diversify_drops_shared_keeps_distinctive(self):
        f = {"semantic_query": "x", "bm25_queries": ["distributed systems engineer", "raft engineer"]}
        out = dv.diversify_filters(f, {"distributed systems engineer"})
        self.assertEqual(out["bm25_queries"], ["raft engineer"])
        self.assertTrue(out["bm25_diversified"])

    def test_min_df_guards_small_sets(self):
        # 2 probes sharing a term: with min_df=3 nothing is dropped (too small to trust)
        payloads = [self._p("ai product engineer", "rag"), self._p("ai product engineer", "agents")]
        self.assertEqual(dv.shared_bm25_terms(payloads, df_threshold=0.35, min_df=3), set())


if __name__ == "__main__":
    unittest.main()
