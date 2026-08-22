from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.search.primitives.deep_search import build_eval_inputs
from packs.search.primitives.deep_search import deep_search_loop
from packs.search.primitives.deep_search import plan_critic
from packs.search.primitives.deep_search import run_wide_search
from packs.search.primitives.deep_search.plan_filters import (
    bind_plan_filters,
    compile_plan_filters,
    compile_core_groups,
    enforce_payload_retrieval_filters,
    is_filter_criterion,
    normalize_plan_filters,
    validate_plan_filter_contract,
)
from packs.search.primitives.validate_artifact.validate_artifact import validate_file


class TestPlanFilters(unittest.TestCase):
    def test_plan_prompt_composes_exact_production_trait_lineage(self):
        production = build_eval_inputs.TRAIT_GENERATION_PROMPT_PATH.read_text(
            encoding="utf-8",
        ).rstrip()
        self.assertEqual(build_eval_inputs.load_trait_generation_prompt(), production)
        self.assertEqual(
            build_eval_inputs.PLAN_SYSTEM,
            f"{production}\n\n{build_eval_inputs.DEEP_PLAN_ADAPTER_PROMPT}",
        )
        self.assertIn(
            '"must_have":[{"trait":"...","tier":"core"}]',
            build_eval_inputs.DEEP_PLAN_ADAPTER_PROMPT,
        )
        self.assertNotIn('"tier":"core|table_stakes"', build_eval_inputs.PLAN_SYSTEM)
        self.assertNotIn("core_groups", build_eval_inputs.PLAN_SYSTEM)

    def test_hidden_core_policy_compiles_ordered_two_thirds_paths(self):
        self.assertEqual(
            compile_core_groups(["A", "B", "C"]),
            [
                {"name": "core path 1", "all_of": ["A", "B"], "source": "default"},
                {"name": "core path 2", "all_of": ["A", "C"], "source": "default"},
                {"name": "core path 3", "all_of": ["B", "C"], "source": "default"},
            ],
        )
        groups = compile_core_groups(["A", "B", "C", "D"], source="user")
        self.assertEqual(len(groups), 4)
        self.assertTrue(all(len(group["all_of"]) == 3 for group in groups))
        self.assertTrue(all(group["source"] == "user" for group in groups))
        with self.assertRaisesRegex(ValueError, "at most 4"):
            compile_core_groups(["A", "B", "C", "D", "E"])
        with self.assertRaisesRegex(ValueError, "unique"):
            compile_core_groups(["A", "a"])

    def test_normalizes_editable_english_filters_with_source(self):
        self.assertEqual(
            normalize_plan_filters([
                "7+ YOE",
                {"filter": "Experience at a FAANG-scale company", "source": "user"},
                "  7+ YOE  ",
            ]),
            [
                {"filter": "7+ YOE", "source": "jd"},
                {"filter": "Experience at a FAANG-scale company", "source": "user"},
            ],
        )

    def test_compiles_only_clear_overall_yoe_and_preserves_unsupported_filters(self):
        filters = normalize_plan_filters([
            "7+ years of professional software engineering experience",
            "Experience at a FAANG-scale company",
            "Worked at Series A",
        ])
        self.assertEqual(compile_plan_filters(filters), {"years_experience_min": 7})
        self.assertEqual(len(filters), 3)
        self.assertEqual(
            compile_plan_filters(["5+ years of experience building distributed schedulers"]),
            {},
        )

    def test_legacy_constraint_classifier_covers_seniority_and_pedigree(self):
        for value in (
            "Currently Staff or Principal engineer",
            "Director or VP-level scope",
            "C-suite or Head of Engineering",
            "Stanford, MIT, or Ivy League pedigree",
            "Elite top-tier university",
        ):
            with self.subTest(value=value):
                self.assertTrue(is_filter_criterion(value))
        self.assertFalse(is_filter_criterion("Technical leadership and mentoring"))

    def test_compiles_range_and_most_restrictive_bounds(self):
        self.assertEqual(
            compile_plan_filters(["3-8 YOE", "At least 5 years of work experience"]),
            {"years_experience_min": 5, "years_experience_max": 8},
        )
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            compile_plan_filters(["10+ YOE", "up to 8 YOE"])

    def test_bind_replaces_stale_compiled_projection(self):
        bound = bind_plan_filters({
            "filters": [{"filter": "7+ YOE", "source": "user"}],
            "retrieval_filters": {"years_experience_min": 2},
        })
        self.assertEqual(bound["retrieval_filters"], {"years_experience_min": 7})
        self.assertEqual(validate_plan_filter_contract(bound), {"years_experience_min": 7})

    def test_payload_enforcement_overwrites_probe_generated_yoe(self):
        nested = {"role_search_filters": {
            "semantic_query": "distributed systems",
            "years_experience_min": 2,
            "years_experience_max": 20,
        }}
        enforce_payload_retrieval_filters(nested, {"years_experience_min": 7})
        self.assertEqual(nested["role_search_filters"]["years_experience_min"], 7)
        self.assertNotIn("years_experience_max", nested["role_search_filters"])

        top_level = {"semantic_query": "distributed systems", "years_experience_min": 3}
        enforce_payload_retrieval_filters(top_level, {})
        self.assertNotIn("years_experience_min", top_level)

    def test_wide_search_prepare_applies_reviewed_compiled_yoe(self):
        with tempfile.TemporaryDirectory() as directory:
            probe_dir = Path(directory) / "probes" / "q00"
            prepared = probe_dir / "prep" / "generated"
            prepared.mkdir(parents=True)
            (prepared / "expand_search_request.json").write_text(json.dumps({
                "role_search_filters": {
                    "years_experience_min": 2,
                    "years_experience_max": 20,
                },
            }), encoding="utf-8")
            seed = {
                "key": "q00",
                "query": "distributed systems",
                "required_location": "",
                "location_filters": {},
            }
            with mock.patch.object(run_wide_search, "run_checked", return_value=None):
                payload_path = run_wide_search._prepare(
                    seed,
                    probe_dir,
                    ".env",
                    True,
                    "powerset",
                    None,
                    {"years_experience_min": 7},
                )
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["role_search_filters"]["years_experience_min"],
                7,
            )
            self.assertNotIn("years_experience_max", payload["role_search_filters"])

    def test_new_generation_routes_non_core_requirements_to_nice_or_filters(self):
        plan = build_eval_inputs.plan_from_obj(
            {
                "job_title": "Staff Backend Engineer",
                "must_have": [
                    {"trait": "Built distributed schedulers at scale", "tier": "core"},
                    {"trait": "Technical leadership and mentoring", "tier": "table_stakes"},
                    {
                        "trait": "7+ years of professional software engineering experience",
                        "tier": "table_stakes",
                    },
                ],
                "nice_to_have": ["Caching systems"],
                "filters": [],
            },
            set_name="team",
            set_id="set-1",
            source_url=None,
            created_at="2026-08-17T00:00:00Z",
        )
        self.assertEqual(
            [item["trait"] for item in plan["traits"]["must_have"]],
            ["Built distributed schedulers at scale"],
        )
        self.assertEqual(
            [item["trait"] for item in plan["traits"]["nice_to_have"]],
            ["Caching systems", "Technical leadership and mentoring"],
        )
        self.assertEqual(
            plan["filters"],
            [{
                "filter": "7+ years of professional software engineering experience",
                "source": "jd",
            }],
        )
        self.assertEqual(plan["retrieval_filters"], {"years_experience_min": 7})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            self.assertEqual(validate_file("search-network-jd-plan", path), plan)
            self.assertEqual(deep_search_loop.validate_approved_plan(path), plan)

    def test_new_generation_caps_core_and_ignores_model_qualification_paths(self):
        core = [f"Core capability {index}" for index in range(1, 6)]
        plan = build_eval_inputs.plan_from_obj(
            {
                "must_have": [{"trait": trait, "tier": "core"} for trait in core],
                "nice_to_have": [],
                "filters": [],
                "core_groups": [{
                    "name": "model invented gate",
                    "all_of": [core[0]],
                    "source": "jd",
                }],
            },
            set_name="team",
            set_id="set-1",
            source_url=None,
            created_at="t",
        )
        self.assertEqual(
            [item["trait"] for item in plan["traits"]["must_have"]],
            core[:4],
        )
        self.assertIn(core[4], [item["trait"] for item in plan["traits"]["nice_to_have"]])
        self.assertEqual(plan["core_groups"], compile_core_groups(core[:4]))
        self.assertFalse(
            any("conjunctions sharply reduce recall" in issue
                for issue in plan_critic.deterministic_checks(plan))
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            self.assertEqual(deep_search_loop.validate_approved_plan(path), plan)

    def test_legacy_plan_shape_remains_valid(self):
        plan = build_eval_inputs.plan_from_obj(
            {
                "must_have": [
                    {"trait": "Distributed systems", "tier": "core"},
                    {"trait": "Leadership", "tier": "table_stakes"},
                ],
            },
            set_name="team",
            set_id="set-1",
            source_url=None,
            created_at="t",
        )
        self.assertNotIn("filters", plan)
        self.assertNotIn("retrieval_filters", plan)
        self.assertEqual(len(plan["traits"]["must_have"]), 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            self.assertEqual(validate_file("search-network-jd-plan", path), plan)

    def test_approved_plan_rejects_stale_compiled_projection(self):
        plan = build_eval_inputs.plan_from_obj(
            {
                "must_have": [{"trait": "Distributed systems", "tier": "core"}],
                "filters": ["7+ YOE"],
            },
            set_name="team",
            set_id="set-1",
            source_url=None,
            created_at="t",
        )
        plan["retrieval_filters"] = {"years_experience_min": 5}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "deterministic compilation"):
                deep_search_loop.validate_approved_plan(path)


if __name__ == "__main__":
    unittest.main()
