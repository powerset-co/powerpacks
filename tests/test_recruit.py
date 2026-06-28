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
dj = _load("decompose_jd")
ea = _load("expand_from_anchor")
bei = _load("build_eval_inputs")
tc = _load("triage_candidates")
cj_judge = _load("codex_judge")


class TestDecomposeJd(unittest.TestCase):
    def test_parse_seeds_strings_and_objects(self):
        self.assertEqual(dj.parse_seeds({"seeds": ["a", "b"]}),
                         [{"key": "q00", "query": "a"}, {"key": "q01", "query": "b"}])
        self.assertEqual(dj.parse_seeds({"seeds": [{"query": "x"}, {"seed": "y"}]}),
                         [{"key": "q00", "query": "x"}, {"key": "q01", "query": "y"}])

    def test_parse_seeds_truncates_and_skips_empty(self):
        out = dj.parse_seeds({"seeds": ["a", "", "b", "c"]}, n=2)
        self.assertEqual([s["query"] for s in out], ["a", "b"])

    def test_parse_seeds_raises_on_empty(self):
        with self.assertRaises(ValueError):
            dj.parse_seeds({"seeds": []})

    def test_build_messages_includes_jd_and_count(self):
        msgs = dj.build_messages("Build RAG systems", 7)
        self.assertIn("Build RAG systems", msgs[-1]["content"])
        self.assertIn("7", msgs[-1]["content"])


class TestExpandFromAnchor(unittest.TestCase):
    def test_anchor_to_seed_from_profile(self):
        prof = {"name": "Ada", "headline": "AI Engineer at Notion",
                "positions": [{"title": "AI Engineer", "company_name": "Notion", "company_description": "productivity"}],
                "tech_skills": ["RAG", "LLM"]}
        seed = ea.anchor_to_seed(prof)
        self.assertEqual(seed["anchor"], "Ada")
        self.assertIn("Notion", seed["query"])
        self.assertIn("proven-strong profile", seed["query"])

    def test_anchor_to_seed_fallback_and_none(self):
        self.assertIn("Acme", ea.anchor_to_seed({"current_title": "Eng", "current_company": "Acme"})["query"])
        self.assertIsNone(ea.anchor_to_seed({"name": "x"}))  # no usable text

    def test_build_seeds_takes_top_k_by_score_and_keys(self):
        recs = [{"name": "lo", "headline": "h1", "mean_score": 0.3},
                {"name": "hi", "headline": "h2", "mean_score": 0.9}]
        seeds = ea.build_seeds(recs, top_k=1)
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["anchor"], "hi")
        self.assertEqual(seeds[0]["key"], "anchor00")

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


class TestBuildEvalInputs(unittest.TestCase):
    def test_build_frontier_scores_by_probe_count(self):
        union = [
            {"person_id": "a", "name": "A", "found_by": ["q0", "q1", "q2"]},
            {"person_id": "b", "name": "B", "found_by": ["q0"]},
            {"name": "no-id"},  # dropped
        ]
        front = bei.build_frontier(union)
        self.assertEqual({c["person_id"] for c in front}, {"a", "b"})
        a = next(c for c in front if c["person_id"] == "a")
        self.assertEqual(a["matched_probe_ids"], ["q0", "q1", "q2"])
        self.assertEqual(a["source_rows"][0]["score"], 3.0)
        self.assertEqual(a["candidate_id"], "a")

    def test_build_frontier_single_probe_has_score_one(self):
        front = bei.build_frontier([{"person_id": "b", "found_by": ["q0"]}])
        self.assertEqual(front[0]["source_rows"][0]["score"], 1.0)

    def test_plan_from_obj_shapes_traits_and_scope(self):
        plan = bei.plan_from_obj(
            {"job_title": "MTS", "normalized_archetype": "distsys engineer",
             "hire_stage": "scale", "usable_cutoff": "Senior IC in band.",
             "must_have": ["schedulers", "control plane", ""], "nice_to_have": ["gpus"]},
            set_name="s", set_id="sid", source_url=None, created_at="2026-01-01T00:00:00Z")
        self.assertEqual([t["trait"] for t in plan["traits"]["must_have"]], ["schedulers", "control plane"])
        self.assertEqual(plan["traits"]["nice_to_have"], [{"trait": "gpus"}])
        self.assertEqual(plan["set_scope"], {"name": "s", "set_id": "sid"})
        self.assertEqual(plan["normalized_archetype"], "distsys engineer")
        self.assertFalse(plan["retrieval_ran"])

    def test_plan_from_obj_requires_must_have(self):
        with self.assertRaises(ValueError):
            bei.plan_from_obj({"must_have": []}, set_name="s", set_id="i", source_url=None, created_at="t")

    def test_build_plan_messages_carries_jd(self):
        msgs = bei.build_plan_messages("Design schedulers")
        self.assertIn("Design schedulers", msgs[-1]["content"])


class TestTriageCandidates(unittest.TestCase):
    def test_compact_card_merges_front_and_profile(self):
        card = tc.compact_card(
            "p1",
            {"current_title": "MTS", "current_company": "xAI", "name": "fallback"},
            {"name": "Real Name", "headline": "Systems eng",
             "positions": [{"title": "SWE", "company_name": "Google"}, {"title": "", "company_name": ""}],
             "education": [{"school_name": "MIT", "degree": "BS", "field_of_study": "CS"}],
             "tech_skills": ["go", "k8s", 5]})
        self.assertEqual(card["id"], "p1")
        self.assertEqual(card["name"], "Real Name")  # profile wins over front fallback
        self.assertEqual(card["current"], "MTS @ xAI")
        self.assertEqual(card["positions"], ["SWE @ Google"])  # empty position dropped
        self.assertEqual(card["education"], ["MIT BS CS"])
        self.assertEqual(card["skills"], ["go", "k8s"])  # non-str skill dropped

    def test_compact_card_handles_missing_profile(self):
        card = tc.compact_card("p2", {"name": "Only Front", "current_company": "Acme"}, None)
        self.assertEqual(card["name"], "Only Front")
        self.assertEqual(card["current"], "@ Acme")
        self.assertEqual(card["positions"], [])

    def test_parse_verdicts_lowercases(self):
        out = tc.parse_verdicts('{"verdicts":[{"id":"a","v":"KEEP"},{"id":"b","v":"Drop"},{"v":"x"}]}')
        self.assertEqual(out, {"a": "keep", "b": "drop"})

    def test_parse_verdicts_bad_json(self):
        self.assertEqual(tc.parse_verdicts("not json"), {})

    def test_keep_set_is_conservative(self):
        self.assertIn("maybe", tc.KEEP)
        self.assertIn("keep", tc.KEEP)
        self.assertNotIn("drop", tc.KEEP)

    def test_build_batch_messages_lists_traits(self):
        msgs = tc.build_batch_messages(
            {"must_have": [{"trait": "schedulers"}], "nice_to_have": [{"trait": "gpus"}]},
            [{"id": "a", "name": "A"}])
        self.assertIn("schedulers", msgs[-1]["content"])
        self.assertIn("gpus", msgs[-1]["content"])


class TestNormalizeVerdict(unittest.TestCase):
    def test_maps_eval_raw_to_consensus_schema(self):
        r = jc.normalize_verdict({"candidate_id": "p1", "jd_score": 0.7, "seniority_fit": "ideal", "verdict": "top_tier"})
        self.assertEqual(r["person_id"], "p1")
        self.assertEqual(r["score"], 0.7)
        self.assertTrue(r["in_band"])  # ideal is not gated

    def test_gated_fit_is_not_in_band(self):
        r = jc.normalize_verdict({"candidate_id": "p2", "jd_score": 0.2, "seniority_fit": "too_senior", "verdict": "out"})
        self.assertFalse(r["in_band"])

    def test_native_schema_passthrough(self):
        native = {"person_id": "p3", "score": 0.5, "in_band": True, "seniority_fit": "in_band", "verdict": "high_potential"}
        self.assertEqual(jc.normalize_verdict(dict(native)), native)


class TestCodexJudgeExtract(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(cj_judge.extract_json('{"seniority_fit":"ideal"}'), {"seniority_fit": "ideal"})

    def test_fenced_json(self):
        self.assertEqual(cj_judge.extract_json('```json\n{"a":1}\n```'), {"a": 1})

    def test_json_with_prose_around(self):
        self.assertEqual(cj_judge.extract_json('Here is the result:\n{"verdict":"out"}\nDone.'), {"verdict": "out"})

    def test_empty_and_garbage(self):
        self.assertEqual(cj_judge.extract_json(""), {})
        self.assertEqual(cj_judge.extract_json("no json here"), {})


class TestConsensusScoreThreshold(unittest.TestCase):
    def _judges(self):
        # one judge file; in_band derived from seniority_fit
        return {"j": [
            {"person_id": "a", "seniority_fit": "ideal", "verdict": "out", "score": 0.45, "in_band": True},
            {"person_id": "b", "seniority_fit": "ideal", "verdict": "out", "score": 0.30, "in_band": True},
            {"person_id": "c", "seniority_fit": "too_senior", "verdict": "out", "score": 0.9, "in_band": False},
        ]}

    def test_threshold_keeps_inband_above_cutoff(self):
        _, strong = jc.build_consensus(self._judges(), {}, min_inband_votes=1, min_notout_votes=1, score_threshold=0.40)
        self.assertEqual([r["person_id"] for r in strong], ["a"])  # a (0.45) in; b (0.30) below; c gated

    def test_threshold_gates_out_of_band_even_if_high_score(self):
        _, strong = jc.build_consensus(self._judges(), {}, min_inband_votes=1, min_notout_votes=1, score_threshold=0.10)
        self.assertNotIn("c", [r["person_id"] for r in strong])  # too_senior never kept

    def test_no_threshold_uses_notout_gate(self):
        # without threshold, all verdicts are "out" -> nobody passes the not-out gate
        _, strong = jc.build_consensus(self._judges(), {}, min_inband_votes=1, min_notout_votes=1)
        self.assertEqual(strong, [])


if __name__ == "__main__":
    unittest.main()
