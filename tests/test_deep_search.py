"""Unit tests for the $search deep-mode consensus + ground-truth-gap primitives (pure functions)."""
from __future__ import annotations

import argparse
import csv
import gzip
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIM = ROOT / "packs" / "search" / "primitives" / "deep_search"
if str(PRIM) not in sys.path:
    sys.path.insert(0, str(PRIM))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, PRIM / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


jc = _load("judge_consensus")
su = _load("subprocess_utils")
sg = _load("score_ground_truth_gaps")
dv = _load("diversify_probe_bm25")
dj = _load("decompose_jd")
ea = _load("expand_from_anchor")
bei = _load("build_eval_inputs")
tc = _load("triage_candidates")
cj_judge = _load("codex_judge")
rs = _load("robust_source")
rl = _load("deep_search_loop")
fj = _load("fetch_jd")


class TestSubprocessUtils(unittest.TestCase):
    def test_run_checked_raises_on_nonzero(self):
        with self.assertRaises(su.CommandError) as ctx:
            su.run_checked([sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(7)"], description="boom")
        self.assertEqual(ctx.exception.returncode, 7)
        self.assertIn("bad", ctx.exception.stderr_tail)

    def test_run_checked_raises_on_missing_expected_path(self):
        missing = Path(tempfile.mkdtemp()) / "missing.txt"
        with self.assertRaises(su.CommandError) as ctx:
            su.run_checked([sys.executable, "-c", "pass"], expected_paths=[missing], description="artifact")
        self.assertEqual(ctx.exception.missing, [missing])

    def test_run_wide_search_imports_sibling_subprocess_utils(self):
        self.assertTrue(hasattr(_load("run_wide_search"), "run_checked"))


class TestRunWideSearchPartialFailure(unittest.TestCase):
    """A single flaky probe must NOT abort the whole wide search (robustness: tolerate dead probes,
    fail only when none survive)."""

    def test_prepare_returns_none_on_probe_failure_instead_of_raising(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            # rsg.CommandError (not su.CommandError): run_wide_search's except binds its own import.
            boom = rsg.CommandError(["prepare"], returncode=1, description="prepare probe q00")
            with mock.patch.object(rsg, "run_checked", side_effect=boom):
                result = rsg._prepare({"key": "q00", "query": "flaky"}, probe_dir, ".env", True, "powerset", None)
        self.assertIsNone(result)  # tolerated (dropped by ok_seeds), not propagated

    def test_prepare_returns_payload_path_on_success(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            prep = probe_dir / "prep" / "sub"
            prep.mkdir(parents=True)
            (prep / "expand_search_request.json").write_text("{}")
            with mock.patch.object(rsg, "run_checked", return_value=None):
                dest = rsg._prepare({"key": "q00", "query": "ok"}, probe_dir, ".env", True, "powerset", None)
            self.assertEqual(dest, probe_dir / "payload.json")
            self.assertTrue((probe_dir / "payload.json").exists())

    def test_run_returns_false_on_probe_failure_instead_of_raising(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            probe_dir.mkdir(parents=True)
            (probe_dir / "payload.json").write_text(json.dumps({"role_search_filters": {}}))
            boom = rsg.CommandError(["run"], returncode=1, description="run probe q00")
            with mock.patch.object(rsg, "run_checked", side_effect=boom):
                ok = rsg._run({"key": "q00", "query": "flaky"}, probe_dir, "set-123", ".env", 200, 6000, "powerset", None)
        self.assertFalse(ok)  # tolerated (build_union skips the missing ledger), not propagated

    def test_run_returns_true_on_success(self):
        rsg = _load("run_wide_search")
        with tempfile.TemporaryDirectory() as td:
            probe_dir = Path(td) / "probes" / "q00"
            probe_dir.mkdir(parents=True)
            (probe_dir / "payload.json").write_text(json.dumps({"role_search_filters": {}}))
            with mock.patch.object(rsg, "run_checked", return_value=None):
                ok = rsg._run({"key": "q00", "query": "ok"}, probe_dir, None, ".env", 200, 6000, "powerset", None)
        self.assertTrue(ok)


class TestLocalBackendThreading(unittest.TestCase):
    """--backend/--db threading through the deep-search sourcing chain (post search-backend fold)."""

    class _HaltAfterParse(Exception):
        pass

    def test_backend_args_local_vs_powerset(self):
        rsg = _load("run_wide_search")
        self.assertEqual(rsg._backend_args("local", "x.duckdb"), ["--backend", "local", "--db", "x.duckdb"])
        self.assertEqual(rsg._backend_args("powerset", None), [])

    def _parse_with_real_parser(self, mod, argv: list[str]) -> argparse.Namespace:
        """Drive mod.main() only through its real argparse parse, then halt (no execution)."""
        captured: dict[str, argparse.Namespace] = {}
        real_parse_args = argparse.ArgumentParser.parse_args

        def spy(parser, *args, **kwargs):
            captured["args"] = real_parse_args(parser, *args, **kwargs)
            raise TestLocalBackendThreading._HaltAfterParse()

        old_argv = sys.argv
        sys.argv = argv
        try:
            with mock.patch.object(argparse.ArgumentParser, "parse_args", spy):
                with self.assertRaises(TestLocalBackendThreading._HaltAfterParse):
                    mod.main()
        finally:
            sys.argv = old_argv
        return captured["args"]

    def test_deep_search_loop_parser_accepts_local_backend(self):
        args = self._parse_with_real_parser(
            rl,
            ["loop", "--jd-file", "jd.txt", "--run-dir", "run", "--created-at", "t",
             "--backend", "local", "--db", "x.duckdb"],
        )
        self.assertEqual(args.backend, "local")
        self.assertEqual(args.db, "x.duckdb")

    def test_robust_source_parser_accepts_local_backend(self):
        args = self._parse_with_real_parser(
            rs,
            ["robust", "--jd-file", "jd.txt", "--run-dir", "run",
             "--backend", "local", "--db", "x.duckdb"],
        )
        self.assertEqual(args.backend, "local")
        self.assertEqual(args.db, "x.duckdb")


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


class TestCoreGate(unittest.TestCase):
    CORE = {"distributed systems"}  # normalized core-trait text

    def _judges(self):
        # GEM: lower score but DOES the core domain (doing_now). WRONG: higher score, strong+senior
        # but only `capable` on the core (adjacent/wrong-domain). GATED: does the core but too_senior.
        return {"j1": [
            {"person_id": "GEM", "name": "Gem", "in_band": True, "verdict": "high_potential",
             "score": 0.6, "seniority_fit": "in_band",
             "must_have": [{"trait": "Distributed Systems", "status": "doing_now"}]},
            {"person_id": "WRONG", "name": "Wrong", "in_band": True, "verdict": "top_tier",
             "score": 0.9, "seniority_fit": "in_band",
             "must_have": [{"trait": "distributed systems", "status": "capable"}]},
            {"person_id": "GATED", "name": "Gated", "in_band": False, "verdict": "out",
             "score": 0.8, "seniority_fit": "too_senior",
             "must_have": [{"trait": "distributed systems", "status": "doing_now"}]},
        ]}

    def test_core_traits_from_plan_reads_core_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "plan.json"
            p.write_text(json.dumps({"traits": {"must_have": [
                {"trait": "Distributed Systems", "tier": "core"},
                {"trait": "leadership", "tier": "table_stakes"}]}}))
            self.assertEqual(jc.core_traits_from_plan(p), {"distributed systems"})

    def test_meets_core_requires_experienced_not_capable(self):
        j = self._judges()["j1"]
        gem = {x["person_id"]: x for x in j}
        self.assertTrue(jc.candidate_meets_core({"j1": gem["GEM"]}, ["j1"], self.CORE))
        self.assertFalse(jc.candidate_meets_core({"j1": gem["WRONG"]}, ["j1"], self.CORE))

    def test_core_gate_excludes_wrong_domain_keeps_gem(self):
        _, strong = jc.build_consensus(self._judges(), {}, min_inband_votes=1, min_notout_votes=1,
                                       score_threshold=0.40, core_traits=self.CORE)
        ids = [r["person_id"] for r in strong]
        self.assertEqual(ids, ["GEM"])  # WRONG excluded (capable), GATED excluded (too_senior)

    def test_core_gate_passes_when_one_of_multiple_core_traits_met(self):
        judges = {"j1": [
            {"person_id": "ONE", "name": "One", "in_band": True, "verdict": "high_potential",
             "score": 0.7, "seniority_fit": "in_band",
             "must_have": [
                 {"trait": "distributed systems", "status": "experienced"},
                 {"trait": "gpu kernels", "status": "missing"},
             ]},
        ]}
        _, strong = jc.build_consensus(judges, {}, min_inband_votes=1, min_notout_votes=1,
                                       score_threshold=0.40, core_traits={"distributed systems", "gpu kernels"})
        self.assertEqual([r["person_id"] for r in strong], ["ONE"])

    def test_no_core_traits_falls_back_to_score_gate(self):
        _, strong = jc.build_consensus(self._judges(), {}, min_inband_votes=1, min_notout_votes=1,
                                       score_threshold=0.40, core_traits=set())
        # No core-gate: WRONG (0.9, in-band) qualifies on score alone; GATED still out (not in-band).
        self.assertIn("WRONG", [r["person_id"] for r in strong])


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

    def test_plan_target_level_valid_passes_through(self):
        plan = bei.plan_from_obj({"must_have": ["x"], "target_level": "VP"},
                                 set_name="s", set_id="i", source_url=None, created_at="t")
        self.assertEqual(plan["target_level"], "vp")  # normalized lowercase

    def test_plan_target_level_invalid_defaults_to_senior_ic(self):
        plan = bei.plan_from_obj({"must_have": ["x"], "target_level": "supreme_overlord"},
                                 set_name="s", set_id="i", source_url=None, created_at="t")
        self.assertEqual(plan["target_level"], "senior_ic")

    def test_plan_target_level_absent_defaults_to_senior_ic(self):
        plan = bei.plan_from_obj({"must_have": ["x"]}, set_name="s", set_id="i", source_url=None, created_at="t")
        self.assertEqual(plan["target_level"], "senior_ic")

    def test_build_plan_messages_carries_jd(self):
        msgs = bei.build_plan_messages("Design schedulers")
        self.assertIn("Design schedulers", msgs[-1]["content"])

    def test_must_trait_tagged_object_preserves_tier(self):
        self.assertEqual(bei._must_trait({"trait": "distributed systems", "tier": "core"}),
                         {"trait": "distributed systems", "tier": "core"})

    def test_must_trait_invalid_tier_defaults_table_stakes(self):
        # A mis-tagged/absent tier must NOT over-gate -> degrade to table_stakes (gate falls back).
        self.assertEqual(bei._must_trait({"trait": "x", "tier": "bogus"})["tier"], "table_stakes")
        self.assertEqual(bei._must_trait({"trait": "x"})["tier"], "table_stakes")

    def test_must_trait_bare_string_is_table_stakes(self):
        self.assertEqual(bei._must_trait("schedulers"), {"trait": "schedulers", "tier": "table_stakes"})
        self.assertIsNone(bei._must_trait("   "))

    def test_plan_from_obj_carries_core_tier(self):
        plan = bei.plan_from_obj(
            {"must_have": [{"trait": "fusion hardware", "tier": "core"},
                           {"trait": "leadership", "tier": "table_stakes"}]},
            set_name="s", set_id="i", source_url=None, created_at="t")
        tiers = {t["trait"]: t["tier"] for t in plan["traits"]["must_have"]}
        self.assertEqual(tiers, {"fusion hardware": "core", "leadership": "table_stakes"})

    def test_build_frontier_has_merge_compatible_fields(self):
        union_row = {
            "person_id": "p1", "name": "Ada", "linkedin_url": "https://linkedin.com/in/ada",
            "current_title": "Staff Engineer", "current_company": "Acme", "location": "SF",
            "found_by": ["q1", "q2"],
        }
        # Source/Channel carry the REAL import provenance from the profile source_map, not the method
        front = bei.build_frontier([union_row], {"p1": ("Jane Doe", "gmail")})
        row = front[0]
        self.assertEqual(row["candidate_id"], "p1")
        self.assertEqual(row["current_role"], "Staff Engineer")
        self.assertEqual(row["source_operator"], "Jane Doe")
        self.assertEqual(row["source_channel"], "gmail")
        self.assertEqual(row["duplicate_signal"]["matched_probe_count"], 2)
        self.assertEqual(row["duplicate_signal"]["matched_probe_ids"], ["q1", "q2"])
        # no provenance on file -> empty, never a hardcoded placeholder value
        bare = bei.build_frontier([union_row])[0]
        self.assertEqual((bare["source_operator"], bare["source_channel"]), ("", ""))

    def test_profile_source_map_reads_primary_provenance(self):
        d = Path(tempfile.mkdtemp())
        (d / "hydrate_people").mkdir(parents=True)
        with gzip.open(d / "hydrate_people" / "profiles.jsonl.gz", "wt") as fh:
            fh.write(json.dumps({"person_id": "p1", "primary_source_operator": "Jane Doe", "primary_source_channel": "gmail"}) + "\n")
            fh.write(json.dumps({"person_id": "p2", "source_operators": ["Bob"], "source_channels": ["linkedin"]}) + "\n")
        m = bei.profile_source_map([str(d)])
        self.assertEqual(m["p1"], ("Jane Doe", "gmail"))
        self.assertEqual(m["p2"], ("Bob", "linkedin"))  # falls back to first of the *_operators/_channels lists

    def test_write_frontier_artifacts_writes_same_full_ids(self):
        d = Path(tempfile.mkdtemp())
        frontier = bei.build_frontier([
            {"person_id": "a", "found_by": ["q0"], "current_title": "Eng"},
            {"person_id": "b", "found_by": ["q1"], "current_title": "MTS"},
        ])
        bei.write_frontier_artifacts(d, frontier)
        json_doc = json.loads((d / "candidate_frontier.json").read_text())
        jsonl_rows = [json.loads(l) for l in (d / "candidate_frontier.jsonl").read_text().splitlines()]
        self.assertEqual(json_doc["candidate_count"], 2)
        self.assertEqual({r["person_id"] for r in json_doc["candidates"]}, {"a", "b"})
        self.assertEqual({r["person_id"] for r in jsonl_rows}, {"a", "b"})
        self.assertIn("duplicate_signal", json_doc["candidates"][0])

    def test_main_reuses_plan_without_created_at_and_writes_json_and_jsonl(self):
        d = Path(tempfile.mkdtemp())
        (d / "union.jsonl").write_text(json.dumps({"person_id": "p1", "found_by": ["q0"], "current_title": "Eng"}) + "\n")
        plan = {"traits": {"must_have": [{"trait": "systems", "tier": "core"}], "nice_to_have": []},
                "created_at": "original", "target_level": "staff_ic"}
        plan_path = d / "approved.json"
        plan_path.write_text(json.dumps(plan))
        argv = sys.argv
        sys.argv = ["build", "--run-dir", str(d), "--plan", str(plan_path)]
        try:
            bei.main()
        finally:
            sys.argv = argv
        self.assertTrue((d / "candidate_frontier.json").exists())
        self.assertTrue((d / "candidate_frontier.jsonl").exists())
        self.assertEqual(json.loads((d / "plan.json").read_text())["created_at"], "original")

    def test_main_requires_created_at_for_new_plan_before_openai(self):
        d = Path(tempfile.mkdtemp())
        (d / "union.jsonl").write_text(json.dumps({"person_id": "p1", "found_by": ["q0"]}) + "\n")
        jd = d / "jd.txt"
        jd.write_text("Build distributed systems")
        argv = sys.argv
        sys.argv = ["build", "--run-dir", str(d), "--jd-file", str(jd)]
        try:
            with self.assertRaises(SystemExit):
                bei.main()
        finally:
            sys.argv = argv

    def test_emitted_frontier_works_with_capture_and_export_defaults(self):
        d = Path(tempfile.mkdtemp())
        frontier = bei.build_frontier([{
            "person_id": "p1", "name": "Ada", "linkedin_url": "https://linkedin.com/in/ada",
            "current_title": "Staff Engineer", "current_company": "Acme", "location": "SF",
            "found_by": ["q0", "q1"],
        }], {"p1": ("Jane Doe", "gmail")})
        bei.write_frontier_artifacts(d, frontier)
        (d / "plan.json").write_text(json.dumps({"traits": {"must_have": [{"trait": "systems"}], "nice_to_have": []}}))
        raw = {
            "candidate_id": "p1", "person_id": "p1", "rank": 1, "jd_score": 0.8,
            "verdict": "top_tier", "seniority_fit": "in_band", "rationale": "strong fit",
            "must_have": [{"trait": "systems", "status": "doing_now", "evidence": "built it"}],
            "nice_to_have": [], "duplicate_signal": frontier[0]["duplicate_signal"], "caveats": [],
        }
        (d / "candidate_evaluations.raw.jsonl").write_text(json.dumps(raw) + "\n")
        capture = ROOT / "packs/search/primitives/capture_jd_evaluations/capture_jd_evaluations.py"
        export = ROOT / "packs/search/primitives/export_candidate_shortlist/export_candidate_shortlist.py"
        cp = subprocess.run([sys.executable, str(capture), "--run-dir", str(d), "--evaluator-mode", "harness_single_agent", "--force"],
                            text=True, capture_output=True, check=False)
        self.assertEqual(cp.returncode, 0, cp.stderr + cp.stdout)
        cp = subprocess.run([sys.executable, str(export), "--run-dir", str(d)], text=True, capture_output=True, check=False)
        self.assertEqual(cp.returncode, 0, cp.stderr + cp.stdout)
        with (d / "shortlist.csv").open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["Current Role"], "Staff Engineer")
        self.assertEqual(rows[0]["Source"], "Jane Doe")
        self.assertEqual(rows[0]["Channel"], "gmail")


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

    def test_judge_one_passes_prompt_via_stdin_not_argv(self):
        long_prompt = "PRIVATE PROFILE " * 100
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["input"] = kwargs.get("input")
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_text('{"seniority_fit":"ideal"}')
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(cj_judge.subprocess, "run", side_effect=fake_run):
            parsed, err = cj_judge.judge_one(long_prompt, None, "low", 5)
        self.assertEqual(parsed, {"seniority_fit": "ideal"})
        self.assertIsNone(err)
        self.assertEqual(seen["input"], long_prompt)
        self.assertFalse(any(long_prompt in str(part) for part in seen["cmd"]))

    def test_judge_one_surfaces_nonzero_exit(self):
        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=42, stdout="", stderr="permission denied")

        with mock.patch.object(cj_judge.subprocess, "run", side_effect=fake_run):
            parsed, err = cj_judge.judge_one("prompt", None, "low", 5)
        self.assertEqual(parsed, {})
        self.assertIn("codex_exit_42", err)
        self.assertIn("permission denied", err)

    def test_main_fails_when_all_codex_subprocesses_error(self):
        d = Path(tempfile.mkdtemp())
        candidate = {"person_id": "p1", "candidate_id": "p1"}
        argv = sys.argv
        sys.argv = ["codex_judge", "--run-dir", str(d)]
        try:
            with mock.patch.object(cj_judge.EV, "read_json", return_value={"traits": {"must_have": []}}), \
                 mock.patch.object(cj_judge.EV, "load_frontier", return_value=[candidate]), \
                 mock.patch.object(cj_judge.EV, "collect_profiles", return_value={"p1": {"person_id": "p1"}}), \
                 mock.patch.object(cj_judge.EV, "build_user_prompt", return_value="profile prompt"), \
                 mock.patch.object(cj_judge, "judge_one", return_value=({}, "codex_exit_1: auth")):
                with self.assertRaises(SystemExit) as ctx:
                    cj_judge.main()
            self.assertEqual(ctx.exception.code, 1)
        finally:
            sys.argv = argv
        self.assertTrue((d / "candidate_evaluations.raw.jsonl").exists())


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


class TestRobustSourceMerge(unittest.TestCase):
    def _write(self, rows):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.close()
        return Path(f.name)

    def test_merge_dedups_and_counts_net_new(self):
        into = {}
        p0 = self._write([{"person_id": "a", "found_by": ["q0"]}, {"person_id": "b", "found_by": ["q1"]}])
        n0 = rs._merge(into, p0, "r0")
        self.assertEqual(n0, 2)
        self.assertEqual(set(into), {"a", "b"})
        self.assertEqual(into["a"]["found_by"], ["r0:q0"])  # provenance tagged with round
        # round 1: a is a repeat (no net-new), c is new
        p1 = self._write([{"person_id": "a", "found_by": ["q5"]}, {"person_id": "c", "found_by": ["q9"]}])
        n1 = rs._merge(into, p1, "r1")
        self.assertEqual(n1, 1)  # only c is net-new
        self.assertEqual(set(into), {"a", "b", "c"})
        self.assertEqual(into["a"]["found_by"], ["r0:q0", "r1:q5"])  # accumulated across rounds

    def test_merge_skips_idless_and_missing_file(self):
        into = {"x": {"person_id": "x", "found_by": []}}
        self.assertEqual(rs._merge(into, Path("/nonexistent/none.jsonl"), "r0"), 0)
        p = self._write([{"found_by": ["q"]}, {"person_id": "y", "found_by": ["q"]}])
        self.assertEqual(rs._merge(into, p, "r0"), 1)  # idless dropped, y added

    def test_emphases_are_distinct(self):
        self.assertEqual(len(set(rs.EMPHASES)), len(rs.EMPHASES))

    def test_main_fails_on_child_command_error(self):
        d = Path(tempfile.mkdtemp())
        jd = d / "jd.txt"
        jd.write_text("Build distributed systems")
        err = rs.CommandError(["fake"], returncode=9, stderr="boom", description="decompose round 0")
        argv = sys.argv
        sys.argv = ["robust", "--jd-file", str(jd), "--run-dir", str(d / "run"), "--max-rounds", "1"]
        try:
            with mock.patch.object(rs, "run_checked", side_effect=err):
                with self.assertRaises(SystemExit) as ctx:
                    rs.main()
            self.assertEqual(ctx.exception.code, 1)
        finally:
            sys.argv = argv


class TestRecruitLoopAnchors(unittest.TestCase):
    def test_diverse_anchors_dedups_by_company_and_ranks_by_score(self):
        strong = [
            {"person_id": "a", "current_company": "SpaceX", "mean_score": 0.9},
            {"person_id": "b", "current_company": "SpaceX", "mean_score": 0.8},  # same co -> skipped
            {"person_id": "c", "current_company": "Meta", "mean_score": 0.85},
            {"person_id": "d", "current_company": "NVIDIA", "mean_score": 0.7},
        ]
        union = {"a": {"person_id": "a", "tech_skills": ["x"]}}
        out = rl.diverse_anchors(strong, union, k=3)
        self.assertEqual([r["person_id"] for r in out], ["a", "c", "d"])  # b dropped (dup co), ranked by score
        self.assertEqual(out[0]["tech_skills"], ["x"])  # enriched from union profile

    def test_diverse_anchors_empty_when_no_strong(self):
        self.assertEqual(rl.diverse_anchors([], {}, k=5), [])

    def test_diverse_anchors_respects_k(self):
        strong = [{"person_id": str(i), "current_company": f"co{i}", "mean_score": 1.0 - i / 10} for i in range(10)]
        self.assertEqual(len(rl.diverse_anchors(strong, {}, k=4)), 4)

    def test_stage_judge_input_does_not_mutate_canonical_frontier(self):
        d = Path(tempfile.mkdtemp())
        (d / "plan.json").write_text(json.dumps({"traits": {"must_have": []}}))
        (d / "probe_summaries.json").write_text("[]")
        full = [
            {"person_id": "a", "candidate_id": "a"},
            {"person_id": "b", "candidate_id": "b"},
        ]
        original = "".join(json.dumps(r, sort_keys=True) + "\n" for r in full)
        (d / "candidate_frontier.jsonl").write_text(original)
        (d / "candidate_frontier.json").write_text(json.dumps({"candidates": full}))

        jdir = rl.stage_judge_input(d, [full[0]])
        self.assertEqual((d / "candidate_frontier.jsonl").read_text(), original)
        staged = [json.loads(l) for l in (jdir / "candidate_frontier.jsonl").read_text().splitlines()]
        self.assertEqual([r["person_id"] for r in staged], ["a"])
        self.assertTrue((jdir / "plan.json").exists())
        self.assertTrue((jdir / "probe_summaries.json").exists())

    def test_judge_consensus_help_says_at_least_one_core(self):
        cp = subprocess.run([sys.executable, str(PRIM / "judge_consensus.py"), "--help"], text=True, capture_output=True, check=False)
        self.assertEqual(cp.returncode, 0)
        self.assertIn("at least one core", cp.stdout)
        self.assertNotIn("every core must-have", cp.stdout)

    def test_main_stops_at_gate_without_approval_and_does_not_judge(self):
        d = Path(tempfile.mkdtemp())
        jd = d / "jd.txt"
        jd.write_text("Build systems")
        run_dir = d / "run"

        def fake_run(cmd, *, expected_paths=None, description=None):
            if description == "epoch0 robust_source":
                (run_dir / "epoch0" / "union.jsonl").write_text(json.dumps({"person_id": "p1", "found_by": ["q0"]}) + "\n")
            elif description == "epoch0 build_eval_inputs":
                e0 = run_dir / "epoch0"
                (e0 / "plan.json").write_text(json.dumps({"traits": {"must_have": []}, "created_at": "t"}))
                rows = [{"person_id": "p1", "candidate_id": "p1"}]
                (e0 / "candidate_frontier.jsonl").write_text(json.dumps(rows[0]) + "\n")
                (e0 / "candidate_frontier.json").write_text(json.dumps({"candidates": rows}))
                (e0 / "probe_summaries.json").write_text("[]")
            else:
                self.fail(f"unexpected run before gate: {description}")

        argv = sys.argv
        sys.argv = ["loop", "--jd-file", str(jd), "--run-dir", str(run_dir), "--created-at", "t", "--max-epochs", "1"]
        try:
            with mock.patch.object(rl, "run", side_effect=fake_run), mock.patch.object(rl, "judge", side_effect=AssertionError("judge called")):
                rl.main()
        finally:
            sys.argv = argv
        hist = json.loads((run_dir / "loop.json").read_text())
        self.assertEqual(hist[0]["status"], "awaiting_plan_approval")
        self.assertTrue((run_dir / "epoch0" / "plan.json").exists())

    def test_plan_approved_resume_preserves_existing_plan_and_skips_build(self):
        d = Path(tempfile.mkdtemp())
        jd = d / "jd.txt"
        jd.write_text("Build systems")
        run_dir = d / "run"
        e0 = run_dir / "epoch0"
        e0.mkdir(parents=True)
        plan_bytes = b'{"traits":{"must_have":[]},"created_at":"human-edited"}\n'
        (e0 / "plan.json").write_bytes(plan_bytes)
        (e0 / "union.jsonl").write_text(json.dumps({"person_id": "p1", "found_by": ["q0"]}) + "\n")
        rows = [{"person_id": "p1", "candidate_id": "p1"}]
        (e0 / "candidate_frontier.jsonl").write_text(json.dumps(rows[0]) + "\n")
        (e0 / "candidate_frontier.json").write_text(json.dumps({"candidates": rows}))
        (e0 / "probe_summaries.json").write_text("[]")
        calls = []

        def fake_run(cmd, *, expected_paths=None, description=None):
            calls.append(description)
            self.assertNotEqual(description, "epoch0 build_eval_inputs")
            if description == "epoch0 consensus":
                out = run_dir / "shortlist"
                out.mkdir(parents=True, exist_ok=True)
                (out / "consensus.json").write_text("[]")
                (out / "ground_truth_ranked.json").write_text("[]")

        def fake_judge(edir, candidates, judge_kind, effort, concurrency):
            (edir / "candidate_evaluations.raw.jsonl").write_text(json.dumps({"candidate_id": "p1", "jd_score": 0.1}) + "\n")

        argv = sys.argv
        sys.argv = ["loop", "--jd-file", str(jd), "--run-dir", str(run_dir), "--created-at", "t", "--max-epochs", "1", "--plan-approved"]
        try:
            with mock.patch.object(rl, "run", side_effect=fake_run), mock.patch.object(rl, "judge", side_effect=fake_judge):
                rl.main()
        finally:
            sys.argv = argv
        self.assertEqual((e0 / "plan.json").read_bytes(), plan_bytes)
        self.assertEqual(calls, ["epoch0 consensus"])

    def test_unapproved_rerun_with_existing_plan_does_not_clobber_or_build(self):
        d = Path(tempfile.mkdtemp())
        jd = d / "jd.txt"
        jd.write_text("Build systems")
        run_dir = d / "run"
        e0 = run_dir / "epoch0"
        e0.mkdir(parents=True)
        plan_bytes = b'{"traits":{"must_have":[{"trait":"edited"}]},"created_at":"human"}\n'
        (e0 / "plan.json").write_bytes(plan_bytes)

        argv = sys.argv
        sys.argv = ["loop", "--jd-file", str(jd), "--run-dir", str(run_dir), "--created-at", "t", "--max-epochs", "1"]
        try:
            with mock.patch.object(rl, "run", side_effect=AssertionError("should not run child commands")), \
                 mock.patch.object(rl, "judge", side_effect=AssertionError("judge called")):
                rl.main()
        finally:
            sys.argv = argv
        self.assertEqual((e0 / "plan.json").read_bytes(), plan_bytes)
        hist = json.loads((run_dir / "loop.json").read_text())
        self.assertTrue(hist[0]["existing_plan"])
        self.assertEqual(hist[0]["status"], "awaiting_plan_approval")

    def test_main_writes_failure_status_on_command_error(self):
        d = Path(tempfile.mkdtemp())
        jd = d / "jd.txt"
        jd.write_text("Build systems")
        run_dir = d / "run"
        err = rl.CommandError(["fake"], returncode=2, stderr="bad", description="epoch0 robust_source")
        argv = sys.argv
        sys.argv = ["loop", "--jd-file", str(jd), "--run-dir", str(run_dir), "--created-at", "t", "--max-epochs", "1"]
        try:
            with mock.patch.object(rl, "run", side_effect=err):
                with self.assertRaises(SystemExit) as ctx:
                    rl.main()
            self.assertEqual(ctx.exception.code, 1)
        finally:
            sys.argv = argv
        hist = json.loads((run_dir / "loop.json").read_text())
        self.assertEqual(hist[-1]["status"], "failed")


class TestFetchJd(unittest.TestCase):
    """URL->JD front-end that lets $search deep mode accept a job-posting URL."""

    def test_extract_drops_chrome_keeps_content_and_title(self):
        html = (
            "<html><head><title> Senior Backend Engineer - Acme </title><style>.x{}</style></head>"
            "<body><nav>Home About</nav><h1>Senior Backend Engineer</h1>"
            "<p>Build production APIs.</p><ul><li>5+ years Python</li><li>Postgres</li></ul>"
            "<script>var x=1;</script><footer>copyright 2026</footer></body></html>"
        )
        text, title = fj.extract(html)
        self.assertEqual(title, "Senior Backend Engineer - Acme")
        self.assertIn("Senior Backend Engineer", text)
        self.assertIn("5+ years Python", text)
        self.assertIn("Postgres", text)
        # script/style/nav/footer chrome is dropped
        for junk in ("var x=1", "copyright", "Home About"):
            self.assertNotIn(junk, text)

    def test_extract_separates_block_elements(self):
        text, _ = fj.extract("<p>one</p><p>two</p><li>three</li>")
        # block boundaries prevent words running together
        self.assertNotIn("onetwo", text)
        self.assertEqual([ln for ln in text.splitlines() if ln], ["one", "two", "three"])

    def test_main_writes_jd_and_source_json(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "jd.txt"
            html = "<html><head><title>Role X</title></head><body><p>" + ("do the work " * 100) + "</p></body></html>"
            argv = sys.argv
            sys.argv = ["fetch_jd", "--url", "https://example.test/job", "--out", str(out)]
            try:
                with mock.patch.object(fj, "fetch", return_value=(html, "https://example.test/job")):
                    fj.main()  # status ok -> no SystemExit
            finally:
                sys.argv = argv
            self.assertTrue(out.exists())
            self.assertIn("do the work", out.read_text())
            src = json.loads((Path(d) / "source.json").read_text())
            self.assertEqual(src["source_url"], "https://example.test/job")
            self.assertEqual(src["source_title"], "Role X")
            self.assertIn("fetched_at", src)

    def test_main_thin_content_still_writes_and_warns(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "jd.txt"
            argv = sys.argv
            sys.argv = ["fetch_jd", "--url", "https://example.test/js", "--out", str(out)]
            try:
                with mock.patch.object(fj, "fetch", return_value=("<html><body>App</body></html>", "https://example.test/js")):
                    fj.main()  # thin is not a failure -> no SystemExit
            finally:
                sys.argv = argv
            self.assertTrue(out.exists())  # thin content is still written

    def test_deep_search_loop_requires_exactly_one_jd_input(self):
        with tempfile.TemporaryDirectory() as d:
            argv = sys.argv
            # neither jd-file nor jd-url
            sys.argv = ["loop", "--run-dir", str(Path(d) / "r"), "--created-at", "t"]
            try:
                with self.assertRaises(SystemExit) as ctx:
                    rl.main()
                self.assertEqual(ctx.exception.code, 2)
                # both jd-file and jd-url
                sys.argv = ["loop", "--jd-file", "x.txt", "--jd-url", "http://y", "--run-dir", str(Path(d) / "r2"), "--created-at", "t"]
                with self.assertRaises(SystemExit) as ctx2:
                    rl.main()
                self.assertEqual(ctx2.exception.code, 2)
            finally:
                sys.argv = argv

    def test_deep_search_loop_jd_url_fetches_before_loop(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d) / "run"
            (run_dir / "epoch0").mkdir(parents=True)
            (run_dir / "epoch0" / "plan.json").write_text('{"traits":[]}')  # forces zero-spend awaiting_plan_approval
            argv = sys.argv
            sys.argv = ["loop", "--jd-url", "https://example.test/job", "--run-dir", str(run_dir), "--created-at", "t"]
            try:
                # stub the fetch_jd subprocess: write a realistic (non-thin) jd.txt like the real primitive would
                def fake_run(cmd, **kw):
                    (run_dir / "jd.txt").write_text(
                        "Senior Backend Engineer\n\n" + ("Build and operate high-throughput APIs. " * 20))
                with mock.patch.object(rl, "run", side_effect=fake_run):
                    rl.main()  # returns at awaiting_plan_approval (no SystemExit)
            finally:
                sys.argv = argv
            self.assertTrue((run_dir / "jd.txt").exists())  # URL was fetched to jd.txt before the loop

    def test_deep_search_loop_rejects_thin_fetched_jd(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d) / "run"
            argv = sys.argv
            sys.argv = ["loop", "--jd-url", "https://example.test/js-job", "--run-dir", str(run_dir), "--created-at", "t"]
            try:
                # a JS-rendered page fetches to near-empty text: the loop must stop, not build a garbage plan
                def fake_run(cmd, **kw):
                    (run_dir / "jd.txt").write_text("Apply now\n")
                with mock.patch.object(rl, "run", side_effect=fake_run):
                    with self.assertRaises(SystemExit) as ctx:
                        rl.main()
                self.assertEqual(ctx.exception.code, 1)  # thin JD -> hard fail before sourcing
            finally:
                sys.argv = argv


if __name__ == "__main__":
    unittest.main()
