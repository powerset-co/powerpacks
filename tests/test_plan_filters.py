from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.primitives.deep_search import build_eval_inputs
from packs.search.primitives.deep_search import deep_search_loop
from packs.search.primitives.deep_search.plan_filters import (
    bind_plan_filters,
    compile_plan_filters,
    enforce_payload_retrieval_filters,
    normalize_plan_filters,
    validate_plan_filter_contract,
)
from packs.search.primitives.validate_artifact.validate_artifact import validate_file

_TRAITS = {"traits": [
    {"trait": "builds distributed schedulers", "kind": "capability",
     "evidence_quote": "Build distributed schedulers."},
    {"trait": "owns a service from design to rollout", "kind": "capability",
     "evidence_quote": "Own a service from design to rollout."},
    {"trait": "shipped production software at a startup", "kind": "background",
     "evidence_quote": "Prior experience shipping production software at a startup."},
]}


def _response(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class TestPlanFilters(unittest.TestCase):
    def test_plan_response_is_checkpointed_before_json_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jd = root / "jd.txt"
            raw = root / "plan.raw.json"
            jd.write_text("Synthetic job description", encoding="utf-8")
            client = mock.Mock()
            client.chat.completions.create.return_value = _response("{malformed")

            with mock.patch.object(build_eval_inputs, "make_openai_client",
                                   return_value=client), \
                 self.assertRaises(json.JSONDecodeError):
                build_eval_inputs.extract_plan(
                    jd_file=jd, set_name="team", set_id="set-1", source_url=None,
                    created_at="t", model="test", api_key="test",
                    raw_response_path=raw,
                )

            self.assertEqual(raw.read_text(encoding="utf-8"), "{malformed")
            self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_traits_response_is_checkpointed_before_json_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jd = root / "jd.txt"
            raw = root / "traits.raw.json"
            jd.write_text("Synthetic job description", encoding="utf-8")
            client = mock.Mock()
            client.chat.completions.create.return_value = _response("{malformed")

            with mock.patch.object(build_eval_inputs, "make_openai_client",
                                   return_value=client), \
                 self.assertRaises(json.JSONDecodeError):
                build_eval_inputs.extract_traits(
                    jd_file=jd,
                    brief={
                        "job_title": "Synthetic Role",
                        "normalized_archetype": "synthetic role",
                        "target_level": "senior_ic",
                        "pond_prompt_family": "general",
                    },
                    pond_traits=[{
                        "value": "Synthetic Role", "temporal": "current", "meaning": "role",
                    }],
                    model="test", api_key="test", raw_response_path=raw,
                )

            self.assertEqual(raw.read_text(encoding="utf-8"), "{malformed")

    def test_plan_prompt_stands_alone_without_trait_buckets(self):
        prompt = build_eval_inputs.PLAN_SYSTEM
        self.assertNotIn("Given a search query", prompt)
        for bucket in ("must_have", "nice_to_have", "core_groups", '"traits"', "tier"):
            self.assertNotIn(bucket, prompt)
        for hint_kind in ("ranking-boost", "tool-culture", "comp-band-anchor"):
            self.assertNotIn(hint_kind, prompt)
        self.assertIn("pond_prompt_family", prompt)
        self.assertIn("candidate_populations", prompt)
        self.assertIn("Never an industry", prompt)

    def test_plan_freezes_supported_pond_prompt_family(self):
        plan = build_eval_inputs.plan_from_obj(
            {"pond_prompt_family": "operations-finance-people"}, _TRAITS,
            set_name="team", set_id="set-1", source_url=None, created_at="t",
        )
        self.assertEqual(plan["pond_prompt_family"], "operations-finance-people")

        fallback = build_eval_inputs.plan_from_obj(
            {"pond_prompt_family": "unknown"}, _TRAITS,
            set_name="team", set_id="set-1", source_url=None, created_at="t",
        )
        self.assertEqual(fallback["pond_prompt_family"], "general")

    def test_plan_messages_include_source_department_as_a_hint(self):
        messages = build_eval_inputs.build_plan_messages(
            "Build production software.",
            source_metadata={"department": "Implementation"},
        )
        self.assertIn("Source department hint: Implementation", messages[1]["content"])
        self.assertIn("Build production software", messages[1]["content"])

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

    def test_generated_plan_keeps_english_filters_and_compiles_yoe(self):
        plan = build_eval_inputs.plan_from_obj(
            {
                "job_title": "Staff Backend Engineer",
                "location": "San Francisco",
                "location_filters": {"metro_areas": ["San Francisco Bay Area"]},
                "filters": [
                    "7+ years of professional software engineering experience",
                    "Based in San Francisco Bay Area",
                ],
            },
            _TRAITS,
            set_name="team",
            set_id="set-1",
            source_url=None,
            created_at="2026-08-17T00:00:00Z",
        )
        self.assertEqual(plan["traits"], _TRAITS["traits"])
        self.assertEqual(
            plan["filters"],
            [
                {"filter": "7+ years of professional software engineering experience",
                 "source": "jd"},
                {"filter": "Based in San Francisco Bay Area", "source": "jd"},
            ],
        )
        self.assertEqual(plan["retrieval_filters"], {"years_experience_min": 7})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            self.assertEqual(validate_file("search-network-jd-plan", path), plan)
            self.assertEqual(deep_search_loop.validate_approved_plan(path), plan)

    def test_plan_schema_accepts_optional_trait_selection_reason(self):
        traits = {"traits": [{
            **_TRAITS["traits"][0],
            "selection_reason": "This independently changes candidate ranking.",
        }]}
        plan = build_eval_inputs.plan_from_obj(
            {}, traits, set_name="team", set_id="set-1", source_url=None, created_at="t",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            self.assertEqual(validate_file("search-network-jd-plan", path), plan)

    def test_approved_plan_rejects_stale_compiled_projection(self):
        plan = build_eval_inputs.plan_from_obj(
            {"filters": ["7+ YOE"]},
            _TRAITS,
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
