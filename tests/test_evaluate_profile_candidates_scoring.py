"""Deterministic scoring tests for evaluate_profile_candidates.

The model returns judgments only; the final score and verdict ladder
(top_tier / high_potential / out) are computed in code. These tests pin that
contract.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALUATE_PY = ROOT / "packs/search/primitives/evaluate_profile_candidates/evaluate_profile_candidates.py"


def _load_module():
    name = "evaluate_profile_candidates_scoring_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, EVALUATE_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _traits(statuses: list[str]) -> list[dict]:
    return [{"trait": f"t{i}", "status": s, "evidence": "e"} for i, s in enumerate(statuses)]


def _excellence(trajectory: float, pedigree: float, impact: float) -> dict:
    return {
        "trajectory": {"score": trajectory, "evidence": "e"},
        "pedigree": {"score": pedigree, "evidence": "e", "companies": [], "schools": []},
        "impact": {"score": impact, "evidence": "e"},
    }


def _plan(n_must: int = 3, n_nice: int = 2) -> dict:
    return {
        "job_title": "Founding Engineer",
        "normalized_archetype": "founding product backend engineer",
        "usable_cutoff": "senior/staff IC",
        "hire_stage": "founding_early",
        "traits": {
            "must_have": [{"trait": f"t{i}"} for i in range(n_must)],
            "nice_to_have": [{"trait": f"t{i}"} for i in range(n_must, n_must + n_nice)],
        },
    }


class TestComputeTraitScore(unittest.TestCase):
    def test_all_doing_now_is_top(self) -> None:
        m = _load_module()
        # all must doing_now (quorum 0.95) + nice doing_now bonus -> capped 1.0
        score = m.compute_trait_score(_traits(["doing_now"] * 3), _traits(["doing_now"] * 2))
        self.assertAlmostEqual(score, 1.0)

    def test_ladder_values(self) -> None:
        m = _load_module()
        self.assertAlmostEqual(m.compute_trait_score(_traits(["experienced"] * 2), []), 0.80)
        self.assertAlmostEqual(m.compute_trait_score(_traits(["capable"] * 2), []), 0.70)
        self.assertAlmostEqual(m.compute_trait_score(_traits(["foundational"] * 2), []), 0.50)
        self.assertAlmostEqual(m.compute_trait_score(_traits(["thin"] * 2), []), 0.25)

    def test_musts_drive_score_nice_is_bonus(self) -> None:
        m = _load_module()
        # 3 must experienced (quorum 0.80), nice missing -> 0.80 (nice adds nothing)
        score = m.compute_trait_score(_traits(["experienced"] * 3), _traits(["missing"] * 2))
        self.assertAlmostEqual(score, 0.80)
        # Inverse: must missing (quorum 0), nice experienced -> only the bonus
        # 0 + 0.10 * 0.80 = 0.08. Nice-to-haves cannot rescue absent must-haves.
        score = m.compute_trait_score(_traits(["missing"] * 3), _traits(["experienced"] * 2))
        self.assertAlmostEqual(score, 0.08)

    def test_quorum_forgives_one_weak_must(self) -> None:
        m = _load_module()
        # 2 doing_now + 1 thin: linear mean would be (0.95+0.95+0.25)/3=0.72,
        # quorum discounts the weak one -> clears the top_tier trait gate (>=0.80).
        score = m.compute_trait_score(_traits(["doing_now", "doing_now", "thin"]), [])
        self.assertGreaterEqual(score, 0.80)

    def test_legacy_buckets_fold_onto_ladder(self) -> None:
        m = _load_module()
        # legacy strong -> 0.80 (experienced), partial -> 0.50 (foundational)
        self.assertAlmostEqual(m.compute_trait_score(_traits(["strong"] * 2), []), 0.80)
        self.assertAlmostEqual(m.compute_trait_score(_traits(["partial"] * 2), []), 0.50)

    def test_unknown_scores_zero(self) -> None:
        m = _load_module()
        score = m.compute_trait_score(_traits(["unknown"] * 2), [])
        self.assertAlmostEqual(score, 0.0)


class TestQuorumAggregate(unittest.TestCase):
    def test_anchors(self) -> None:
        m = _load_module()
        q = m.quorum_aggregate
        # n=3: forgive one. 3/3=1.0, 2/3~=0.87, 1/3~=0.43
        self.assertAlmostEqual(q([1.0, 1.0, 1.0]), 1.0)
        self.assertAlmostEqual(q([1.0, 1.0, 0.0]), 2.0 / 2.3, places=3)
        self.assertAlmostEqual(q([1.0, 0.0, 0.0]), 1.0 / 2.3, places=3)

    def test_uniform_values_unchanged(self) -> None:
        m = _load_module()
        for v in (0.0, 0.25, 0.5, 0.7, 0.95):
            self.assertAlmostEqual(m.quorum_aggregate([v, v, v]), v)

    def test_scales_with_n(self) -> None:
        m = _load_module()
        q = m.quorum_aggregate
        # n=7 forgives ~2; n=9 forgives ~3
        self.assertGreater(q([1.0] * 5 + [0.0] * 2), 0.85)   # 5/7
        self.assertGreaterEqual(q([1.0] * 5 + [0.0] * 4), 0.60)  # 5/9 ~ at the hp bar
        self.assertLess(q([1.0] * 5 + [0.0] * 4), 0.80)

    def test_never_discounts_all_single_value(self) -> None:
        m = _load_module()
        self.assertAlmostEqual(m.quorum_aggregate([0.7]), 0.7)
        self.assertAlmostEqual(m.quorum_aggregate([]), 0.0)


class TestVerdictLadder(unittest.TestCase):
    def test_top_tier_requires_all_thresholds(self) -> None:
        m = _load_module()
        must = _traits(["experienced"] * 3)  # trait_score 0.80
        verdict = m.decide_verdict("ideal", 0.80, 0.8, 0.8, must)
        self.assertEqual(verdict, "top_tier")

    def test_capable_across_musts_is_high_potential_not_top_tier(self) -> None:
        m = _load_module()
        # all capable: trait 0.70 < 0.80 top_tier gate, coverage 0.70 >= 0.60
        must = _traits(["capable"] * 3)
        self.assertEqual(m.decide_verdict("ideal", 0.70, 0.8, 0.6, must), "high_potential")

    def test_missing_must_have_blocks_top_tier(self) -> None:
        m = _load_module()
        must = _traits(["doing_now", "doing_now", "missing"])  # coverage 0.633
        verdict = m.decide_verdict("ideal", 0.9, 0.8, 0.9, must)
        self.assertEqual(verdict, "high_potential")

    def test_low_excellence_blocks_top_tier(self) -> None:
        m = _load_module()
        must = _traits(["experienced"] * 3)
        verdict = m.decide_verdict("ideal", 0.80, 0.5, 0.8, must)
        self.assertEqual(verdict, "high_potential")

    def test_solid_coverage_is_high_potential_without_trajectory(self) -> None:
        m = _load_module()
        # foundational+experienced+experienced: coverage (0.5+0.8+0.8)/3 = 0.70
        # moderate trajectory must NOT sink a solid-coverage candidate.
        must = _traits(["foundational", "experienced", "experienced"])
        self.assertEqual(m.decide_verdict("ideal", 0.7, 0.6, 0.55, must), "high_potential")

    def test_diamond_escape_rescues_light_coverage(self) -> None:
        m = _load_module()
        # coverage 0.5 (< 0.60) but steep trajectory >= 0.75 -> high_potential
        must = _traits(["foundational"] * 3)
        self.assertEqual(m.decide_verdict("ideal", 0.5, 0.6, 0.80, must), "high_potential")
        # same light coverage, flat trajectory -> out
        self.assertEqual(m.decide_verdict("ideal", 0.5, 0.6, 0.50, must), "out")

    def test_seniority_gate_forces_out(self) -> None:
        m = _load_module()
        must = _traits(["doing_now"] * 3)
        for fit in ("too_senior", "too_junior", "wrong_track"):
            self.assertEqual(m.decide_verdict(fit, 1.0, 1.0, 1.0, must), "out")

    def test_default_is_out(self) -> None:
        m = _load_module()
        self.assertEqual(m.decide_verdict("ideal", 0.4, 0.3, 0.3, _traits(["thin"] * 3)), "out")


class TestNormalizeEvaluation(unittest.TestCase):
    def test_score_is_computed_not_model_assigned(self) -> None:
        m = _load_module()
        parsed = {
            "jd_score": 0.99,  # model-provided score must be ignored
            "verdict": "top_tier",  # model-provided verdict must be ignored
            "seniority_fit": "ideal",
            "must_have": [{"trait": "t0", "status": "foundational", "evidence": "e"},
                          {"trait": "t1", "status": "foundational", "evidence": "e"},
                          {"trait": "t2", "status": "foundational", "evidence": "e"}],
            "nice_to_have": [{"trait": "t3", "status": "missing", "evidence": ""},
                             {"trait": "t4", "status": "missing", "evidence": ""}],
            "excellence": _excellence(0.5, 0.5, 0.5),
            "rationale": "r",
            "caveats": [],
        }
        out = m.normalize_evaluation(parsed, _plan())
        # quorum(foundational x3)=0.5; nice missing -> 0; trait=0.5;
        # excellence=0.5; final = 0.55*0.5 + 0.45*0.5 = 0.5
        self.assertAlmostEqual(out["jd_score"], 0.5, places=3)
        self.assertEqual(out["verdict"], "out")
        self.assertIn("score_breakdown", out)
        self.assertAlmostEqual(out["score_breakdown"]["trait_score"], 0.5, places=3)

    def test_material_caveats_penalize(self) -> None:
        m = _load_module()
        base = {
            "seniority_fit": "ideal",
            "must_have": [{"trait": f"t{i}", "status": "experienced", "evidence": "e"} for i in range(3)],
            "nice_to_have": [{"trait": f"t{i}", "status": "experienced", "evidence": "e"} for i in (3, 4)],
            "excellence": _excellence(0.9, 0.9, 0.9),
            "rationale": "r",
            "caveats": [],
        }
        clean = m.normalize_evaluation(dict(base), _plan())
        flagged = m.normalize_evaluation(
            {**base, "caveats": [{"text": "c1", "material": True}, {"text": "c2", "material": True}]},
            _plan(),
        )
        self.assertAlmostEqual(clean["jd_score"] - flagged["jd_score"], 0.10, places=3)
        # Non-material caveats do not penalize.
        informational = m.normalize_evaluation(
            {**base, "caveats": [{"text": "c1", "material": False}, "legacy string caveat"]},
            _plan(),
        )
        self.assertAlmostEqual(clean["jd_score"], informational["jd_score"], places=3)

    def test_caveat_penalty_capped(self) -> None:
        m = _load_module()
        caveats = [{"text": f"c{i}", "material": True} for i in range(8)]
        self.assertAlmostEqual(m.caveat_penalty(caveats), 0.20)

    def test_seniority_gate_caps_score(self) -> None:
        m = _load_module()
        parsed = {
            "seniority_fit": "too_senior",
            "must_have": [{"trait": f"t{i}", "status": "doing_now", "evidence": "e"} for i in range(3)],
            "nice_to_have": [{"trait": f"t{i}", "status": "doing_now", "evidence": "e"} for i in (3, 4)],
            "excellence": _excellence(1.0, 1.0, 1.0),
            "rationale": "r",
            "caveats": [],
        }
        out = m.normalize_evaluation(parsed, _plan())
        self.assertEqual(out["verdict"], "out")
        self.assertLessEqual(out["jd_score"], 0.3)

    def test_top_tier_end_to_end(self) -> None:
        m = _load_module()
        parsed = {
            "seniority_fit": "ideal",
            "must_have": [{"trait": f"t{i}", "status": "doing_now", "evidence": "e"} for i in range(3)],
            "nice_to_have": [{"trait": "t3", "status": "experienced", "evidence": "e"},
                             {"trait": "t4", "status": "foundational", "evidence": "e"}],
            "excellence": _excellence(0.9, 0.7, 0.85),
            "rationale": "r",
            "caveats": [{"text": "minor", "material": False}],
        }
        out = m.normalize_evaluation(parsed, _plan())
        self.assertEqual(out["verdict"], "top_tier")
        self.assertGreater(out["jd_score"], 0.85)

    def test_high_potential_diamond_in_the_rough(self) -> None:
        m = _load_module()
        # Light must-coverage (foundational) rescued by steep trajectory.
        parsed = {
            "seniority_fit": "ideal",
            "must_have": [{"trait": "t0", "status": "foundational", "evidence": "e"},
                          {"trait": "t1", "status": "foundational", "evidence": "e"},
                          {"trait": "t2", "status": "foundational", "evidence": "e"}],
            "nice_to_have": [{"trait": "t3", "status": "missing", "evidence": ""},
                             {"trait": "t4", "status": "missing", "evidence": ""}],
            "excellence": _excellence(0.85, 0.2, 0.6),
            "rationale": "r",
            "caveats": [],
        }
        out = m.normalize_evaluation(parsed, _plan())
        self.assertEqual(out["verdict"], "high_potential")

    def test_missing_traits_backfilled_as_unknown(self) -> None:
        m = _load_module()
        parsed = {
            "seniority_fit": "ideal",
            "must_have": [],
            "nice_to_have": [],
            "excellence": _excellence(0.5, 0.5, 0.5),
            "rationale": "r",
            "caveats": [],
        }
        out = m.normalize_evaluation(parsed, _plan())
        self.assertEqual(len(out["must_have"]), 3)
        self.assertTrue(all(t["status"] == "unknown" for t in out["must_have"]))
        self.assertEqual(out["verdict"], "out")


class TestVerdictOrdering(unittest.TestCase):
    def test_tier_sorts_before_score(self) -> None:
        m = _load_module()
        rows = [
            {"verdict": "out", "jd_score": 0.95},
            {"verdict": "high_potential", "jd_score": 0.55},
            {"verdict": "top_tier", "jd_score": 0.88},
            {"verdict": "high_potential", "jd_score": 0.70},
        ]
        ordered = sorted(rows, key=lambda r: (m.VERDICT_ORDER.get(r.get("verdict", "out"), 9), -r.get("jd_score", 0)))
        self.assertEqual(
            [(r["verdict"], r["jd_score"]) for r in ordered],
            [("top_tier", 0.88), ("high_potential", 0.70), ("high_potential", 0.55), ("out", 0.95)],
        )


if __name__ == "__main__":
    unittest.main()
