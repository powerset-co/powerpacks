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
BUILD_EVAL_PY = ROOT / "packs/search/primitives/deep_search/build_eval_inputs.py"
CONSENSUS_PY = ROOT / "packs/search/primitives/deep_search/judge_consensus.py"


def _load_module():
    name = "evaluate_profile_candidates_scoring_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, EVALUATE_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_build_eval_module():
    name = "build_eval_inputs_scoring_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, BUILD_EVAL_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_consensus_module():
    name = "judge_consensus_scoring_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, CONSENSUS_PY)
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


class TestComputeExcellence(unittest.TestCase):
    def test_missing_pedigree_is_floor_neutral(self) -> None:
        m = _load_module()
        without = m.compute_excellence(_excellence(0.8, 0.0, 0.8))
        with_pedigree = m.compute_excellence(_excellence(0.8, 1.0, 0.8))
        self.assertAlmostEqual(without, 0.8)
        self.assertAlmostEqual(with_pedigree, 1.0)

    def test_plan_can_explicitly_ignore_pedigree(self) -> None:
        m = _load_module()
        score = m.compute_excellence(
            _excellence(0.7, 1.0, 0.9),
            {"trajectory": 0.4, "impact": 0.4, "pedigree": 0.2},
            "ignore",
        )
        self.assertAlmostEqual(score, 0.8)


class TestRecruiterPrompt(unittest.TestCase):
    def test_management_titles_require_responsibility_evidence(self) -> None:
        prompt = _load_module().SYSTEM_PROMPT

        self.assertIn("A manager/director/VP/Head title alone is not enough", prompt)
        self.assertIn("mark ambiguous cases `unknown` with a caveat", prompt)

    def test_default_singletons_keep_all_must_haves_in_scoring(self) -> None:
        m = _load_module()
        plan = _plan(n_must=0, n_nice=0)
        plan["traits"]["must_have"] = [
            {"trait": trait, "tier": "core"} for trait in ("path a", "path b", "path c")
        ]
        plan["core_groups"] = [
            {"name": trait, "all_of": [trait], "source": "default"}
            for trait in ("path a", "path b", "path c")
        ]
        parsed = {
            "seniority_fit": "ideal",
            "must_have": [
                {"trait": "path a", "status": "experienced", "evidence": "direct"},
                {"trait": "path b", "status": "missing", "evidence": ""},
                {"trait": "path c", "status": "missing", "evidence": ""},
            ],
            "nice_to_have": [],
            "excellence": _excellence(0.8, 0.0, 0.8),
            "caveats": [],
        }

        out = m.normalize_evaluation(parsed, plan)

        self.assertEqual(out["verdict"], "high_potential")
        self.assertIsNone(out["score_breakdown"]["selected_core_group"])
        self.assertEqual(
            out["score_breakdown"]["scored_must_have"],
            ["path a", "path b", "path c"],
        )
        self.assertAlmostEqual(out["score_breakdown"]["trait_score"], 0.348, places=3)
        self.assertEqual(out["score_breakdown"]["qualifying_core_group"], "path a")
        self.assertEqual(out["score_breakdown"]["qualification_must_have"], ["path a"])
        self.assertAlmostEqual(out["score_breakdown"]["qualification_trait_score"], 0.8)
        self.assertFalse(
            m.uses_default_singleton_core_groups(plan["core_groups"][:-1], ["path a", "path b", "path c"])
        )

        consensus = _load_consensus_module()
        row = consensus.normalize_verdict({"candidate_id": "p1", **out})
        _, strong = consensus.build_consensus(
            {"judge": [row]},
            {},
            min_inband_votes=1,
            min_notout_votes=1,
            score_threshold=0.40,
            core_groups=[{"path a"}, {"path b"}, {"path c"}],
        )
        self.assertEqual([item["person_id"] for item in strong], ["p1"])

    def test_alternative_core_paths_are_or_not_one_flat_must_list(self) -> None:
        m = _load_module()
        plan = _plan(n_must=0, n_nice=0)
        plan["traits"]["must_have"] = [
            {"trait": "path a", "tier": "core"},
            {"trait": "path b1", "tier": "core"},
            {"trait": "path b2", "tier": "core"},
            {"trait": "path b3", "tier": "core"},
        ]
        plan["core_groups"] = [
            {"name": "a", "all_of": ["path a"], "source": "jd"},
            {"name": "b", "all_of": ["path b1", "path b2", "path b3"], "source": "jd"},
        ]
        parsed = {
            "seniority_fit": "ideal",
            "must_have": [
                {"trait": "path a", "status": "experienced", "evidence": "direct"},
                {"trait": "path b1", "status": "missing", "evidence": ""},
                {"trait": "path b2", "status": "missing", "evidence": ""},
                {"trait": "path b3", "status": "missing", "evidence": ""},
            ],
            "nice_to_have": [],
            "excellence": _excellence(0.8, 0.0, 0.8),
            "rationale": "strong path a",
            "caveats": [],
        }

        out = m.normalize_evaluation(parsed, plan)

        self.assertEqual(out["verdict"], "top_tier")
        self.assertEqual(out["score_breakdown"]["selected_core_group"], "a")
        self.assertEqual(out["score_breakdown"]["scored_must_have"], ["path a"])
        self.assertAlmostEqual(out["score_breakdown"]["trait_score"], 0.8)

    def test_user_groups_use_best_path_scoring(self) -> None:
        m = _load_module()
        plan = _plan(n_must=0, n_nice=0)
        plan["traits"]["must_have"] = [
            {"trait": "hardware", "tier": "core"},
            {"trait": "software", "tier": "core"},
        ]
        plan["core_groups"] = [
            {"name": "hardware path", "all_of": ["hardware"], "source": "user"},
            {"name": "software path", "all_of": ["software"], "source": "user"},
        ]
        must = [
            {"trait": "hardware", "status": "missing", "evidence": ""},
            {"trait": "software", "status": "experienced", "evidence": "direct"},
        ]

        scored, selected = m.effective_must_for_scoring(plan, must)

        self.assertEqual(selected, "software path")
        self.assertEqual([item["trait"] for item in scored], ["software"])

    def test_missing_seniority_is_marked_non_qualifying_upstream(self) -> None:
        m = _load_module()
        plan = _plan(n_must=1, n_nice=0)
        parsed = {
            "must_have": [{"trait": "t0", "status": "experienced", "evidence": "direct"}],
            "nice_to_have": [],
            "excellence": _excellence(0.9, 0.0, 0.9),
            "caveats": [],
        }

        missing = m.normalize_evaluation(parsed, plan)
        explicit = m.normalize_evaluation({**parsed, "seniority_fit": "unknown"}, plan)

        self.assertEqual(missing["seniority_fit"], "unknown")
        self.assertFalse(missing["_seniority_assessment_valid"])
        self.assertTrue(explicit["_seniority_assessment_valid"])

    def test_founder_policy_override_cannot_be_overruled_by_base_title_gate(self) -> None:
        m = _load_module()
        plan = _plan()
        plan["target_level"] = "senior_ic"
        plan["recruiter_policy"] = m.recruiter_policy.resolve_recruiter_preferences(
            user_preferences={"current_founder_c_suite_for_non_exec_ic": "eligible"}
        )
        parsed = {
            "seniority_fit": "too_senior",
            "must_have": [{"trait": f"t{i}", "status": "experienced", "evidence": "e"} for i in range(3)],
            "nice_to_have": [{"trait": f"t{i}", "status": "missing", "evidence": ""} for i in (3, 4)],
            "excellence": _excellence(0.8, 0.0, 0.8),
            "rationale": "current founder",
            "caveats": [],
        }

        out = m.normalize_evaluation(parsed, plan, {"current_title": "Co-Founder and CTO"})

        self.assertEqual(out["seniority_fit"], "unknown")
        self.assertEqual(
            out["seniority_policy_adjustment"],
            "founder_c_suite_eligible_title_gate_removed",
        )
        self.assertNotEqual(out["verdict"], "out")

    def test_founder_default_out_is_enforced_in_code(self) -> None:
        m = _load_module()
        plan = _plan()
        plan["target_level"] = "senior_ic"
        plan["recruiter_policy"] = m.recruiter_policy.resolve_recruiter_preferences()
        parsed = {
            "seniority_fit": "ideal",
            "must_have": [
                {"trait": f"t{i}", "status": "experienced", "evidence": "e"}
                for i in range(3)
            ],
            "nice_to_have": [],
            "excellence": _excellence(0.9, 0.9, 0.9),
            "caveats": [],
        }

        out = m.normalize_evaluation(parsed, plan, {"current_title": "Founder and CEO"})

        self.assertEqual(out["seniority_fit"], "too_senior")
        self.assertEqual(out["seniority_policy_adjustment"], "founder_c_suite_default_out")
        self.assertEqual(out["verdict"], "out")
        self.assertLessEqual(out["jd_score"], 0.3)

    def test_founder_review_policy_stays_unknown(self) -> None:
        m = _load_module()
        plan = _plan()
        plan["target_level"] = "senior_ic"
        preferences = m.recruiter_policy.resolve_recruiter_preferences(
            user_preferences={"current_founder_c_suite_for_non_exec_ic": "review"}
        )["preferences"]

        fit, adjustment = m.apply_founder_policy(
            "ideal", plan, {"current_title": "CTO"}, preferences
        )

        self.assertEqual((fit, adjustment), ("unknown", "founder_c_suite_review"))

    def test_founder_default_does_not_gate_management_target(self) -> None:
        m = _load_module()
        plan = _plan()
        plan["target_level"] = "vp"
        preferences = m.recruiter_policy.resolve_recruiter_preferences()["preferences"]

        fit, adjustment = m.apply_founder_policy(
            "ideal", plan, {"current_title": "Founder and CEO"}, preferences
        )

        self.assertEqual((fit, adjustment), ("ideal", None))

    def test_founder_policy_ignores_sparse_undated_history(self) -> None:
        m = _load_module()

        self.assertFalse(
            m.profile_is_current_founder_c_suite(
                {"positions": [{"title": "Founder and CEO", "company_name": "OldCo"}]}
            )
        )
        self.assertFalse(
            m.profile_is_current_founder_c_suite(
                {
                    "positions": [
                        {"title": "Founder", "start_year": 2018, "end_year": 2020},
                        {"title": "Staff Engineer", "is_current": True},
                    ]
                }
            )
        )

    def test_founder_policy_accepts_defensible_current_position_evidence(self) -> None:
        m = _load_module()

        self.assertTrue(
            m.profile_is_current_founder_c_suite(
                {"positions": [{"position_title": "Co-Founder", "is_current": True}]}
            )
        )
        self.assertTrue(
            m.profile_is_current_founder_c_suite(
                {"work_experiences": [{"title": "CTO", "start_year": 2024}]}
            )
        )


class TestPlanCoreGroupProvenance(unittest.TestCase):
    def _must(self) -> list[dict]:
        return [
            {"trait": trait, "tier": "core", "source": "jd"}
            for trait in ("schedulers", "control planes", "inference")
        ]

    def test_generated_singletons_are_default_provenance(self) -> None:
        m = _load_build_eval_module()
        groups = m._core_groups(
            {
                "core_groups": [
                    {"name": trait, "all_of": [trait]}
                    for trait in ("schedulers", "control planes", "inference")
                ]
            },
            self._must(),
        )

        self.assertEqual({group["source"] for group in groups}, {"default"})

    def test_missing_groups_fall_back_to_default_singletons(self) -> None:
        m = _load_build_eval_module()

        groups = m._core_groups({}, self._must())

        self.assertEqual(
            [(group["all_of"], group["source"]) for group in groups],
            [
                (["schedulers"], "default"),
                (["control planes"], "default"),
                (["inference"], "default"),
            ],
        )

    def test_deliberate_alternative_paths_are_jd_provenance(self) -> None:
        m = _load_build_eval_module()
        groups = m._core_groups(
            {
                "core_groups": [
                    {"name": "scheduler path", "all_of": ["schedulers", "control planes"]},
                    {"name": "inference path", "all_of": ["inference"]},
                ]
            },
            self._must(),
        )

        self.assertEqual({group["source"] for group in groups}, {"jd"})

    def test_explicit_jd_singleton_alternatives_keep_jd_provenance(self) -> None:
        m = _load_build_eval_module()
        groups = m._core_groups(
            {
                "core_groups": [
                    {"name": trait, "all_of": [trait], "source": "jd"}
                    for trait in ("schedulers", "control planes", "inference")
                ]
            },
            self._must(),
        )

        self.assertEqual({group["source"] for group in groups}, {"jd"})

    def test_extractor_prompt_describes_complete_paths_not_each_trait_as_a_gate(self) -> None:
        prompt = _load_build_eval_module().PLAN_SYSTEM

        self.assertIn("lacks evidence for EVERY complete core path", prompt)
        self.assertNotIn("someone who lacks a core trait", prompt)


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
        # direct excellence=.5; pedigree contributes an upside-only .1 bonus;
        # final = 0.55*.5 + 0.45*.6 = .545
        self.assertAlmostEqual(out["jd_score"], 0.545, places=3)
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
