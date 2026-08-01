"""Focused tests for retained recruiting JD, plan, location, and Codex helpers."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PRIM = ROOT / "packs" / "search" / "primitives" / "deep_search"
if str(PRIM) not in sys.path:
    sys.path.insert(0, str(PRIM))

from packs.search.primitives.deep_search import build_eval_inputs as bei
from packs.search.primitives.deep_search import codex_judge as cj_judge
from packs.search.primitives.deep_search import fetch_jd as fj
from packs.search.primitives.deep_search import location_scope as ls
from packs.search.primitives.deep_search import plan_critic as pc


class TestRequiredLocationScope(unittest.TestCase):
    def test_metro_match_is_strict_and_missing_fails_closed(self):
        required = {"metro_areas": ["San Francisco Bay Area"]}
        self.assertEqual(ls.location_fit(required, "San Francisco, California, United States"), "match")
        self.assertEqual(ls.location_fit(required, "Palo Alto, California, United States"), "match")
        self.assertEqual(ls.location_fit(required, "San Mateo, California, United States"), "match")
        self.assertEqual(ls.location_fit(required, "Santa Monica, California, United States"), "mismatch")
        self.assertEqual(ls.location_fit(required, None), "unknown")

    def test_null_scope_does_not_gate(self):
        self.assertEqual(ls.location_fit({}, None), "not_required")

    def test_approved_global_aliases_must_be_literal_null(self):
        self.assertIsNone(ls.required_location_from_plan({"search_scope": {"location": None, "filters": {}}}))
        for alias in ("", "global", "remote", "worldwide", "anywhere"):
            with self.subTest(alias=alias):
                with self.assertRaises(ValueError):
                    ls.required_location_from_plan({"search_scope": {"location": alias, "filters": {}}})

    def test_approved_scope_requires_filter_contract_even_when_global(self):
        with self.assertRaisesRegex(ValueError, "filters is required"):
            ls.location_scope_from_plan({"search_scope": {"location": None}})
        with self.assertRaisesRegex(ValueError, "must be an object"):
            ls.location_scope_from_plan({"search_scope": {"location": None, "filters": []}})

    def test_reviewed_structured_scopes_cover_regions_states_and_cities(self):
        europe = {"macro_regions": ["Western Europe", "Eurasia"]}
        self.assertEqual(ls.location_fit(europe, "Berlin, Germany"), "match")
        self.assertEqual(ls.location_fit(europe, "San Francisco, California, United States"), "mismatch")
        self.assertEqual(
            ls.location_fit({"states": ["Ontario"], "countries": ["Canada"]}, "Toronto, Ontario, Canada"),
            "match",
        )
        london = {"cities": ["London"], "countries": ["United Kingdom"]}
        self.assertEqual(ls.location_fit(london, "London, England, United Kingdom"), "match")
        self.assertEqual(ls.location_fit(london, "London, Ontario, Canada"), "mismatch")
        africa = ls.canonicalize_generated_location_filters("Africa", {"macro_regions": ["Africa"]})
        oceania = ls.canonicalize_generated_location_filters("Oceania", {"macro_regions": ["Oceania"]})
        self.assertEqual(ls.location_fit(africa, "Accra, Ghana"), "match")
        self.assertEqual(ls.location_fit(oceania, "Sydney, New South Wales, Australia"), "match")

    def test_reviewed_city_scope_requires_country_qualifier(self):
        with self.assertRaisesRegex(ValueError, "country qualifier"):
            ls.location_scope_from_plan(
                {
                    "search_scope": {
                        "location": "London, UK",
                        "filters": {"cities": ["London"]},
                    }
                }
            )

    def test_generated_scope_canonicalizes_aliases_before_review(self):
        self.assertEqual(
            ls.canonicalize_generated_location_filters(
                "London, UK",
                {"cities": ["London"], "countries": ["UK"]},
            ),
            {"cities": ["London"], "countries": ["United Kingdom"]},
        )
        self.assertEqual(
            ls.canonicalize_location_filters({"states": ["CA"], "countries": ["US"]}),
            {"states": ["California"], "countries": ["United States"]},
        )
        self.assertEqual(
            ls.canonicalize_location_filters({"metro_areas": ["New York City metropolitan area"]}),
            {"metro_areas": ["New York Metropolitan Area"]},
        )
        self.assertEqual(
            ls.canonicalize_location_filters({"metro_areas": ["London Metropolitan Area"]}),
            {"metro_areas": ["London Metropolitan Area"]},
        )
        self.assertEqual(
            ls.canonicalize_generated_location_filters(
                "New York City",
                {"cities": ["New York City"], "countries": ["US"]},
            ),
            {"cities": ["New York"], "countries": ["United States"]},
        )
        self.assertEqual(
            ls.canonicalize_generated_location_filters("Europe", {"macro_regions": ["Europe"]}),
            {"macro_regions": ["Western Europe", "Eurasia"]},
        )

    def test_every_local_canonical_metro_is_idempotent(self):
        mapping = json.loads(ls.LOCATION_MAPPING_FILE.read_text(encoding="utf-8"))
        metros = {
            value
            for values in mapping["city_to_metro"].values()
            for value in (values if isinstance(values, list) else [values])
        }
        for metro in sorted(metros):
            with self.subTest(metro=metro):
                self.assertEqual(
                    ls.canonicalize_location_filters({"metro_areas": [metro]}),
                    {"metro_areas": [metro]},
                )

    def test_city_canonicalization_is_field_aware_and_review_idempotent(self):
        filters = ls.canonicalize_generated_location_filters(
            "New York City",
            {"cities": ["New York City"], "countries": ["US"]},
        )
        self.assertEqual(filters, {"cities": ["New York"], "countries": ["United States"]})
        self.assertEqual(
            ls.location_scope_from_plan(
                {
                    "search_scope": {"location": "New York City", "filters": filters},
                }
            ),
            ("New York City", filters),
        )
        for city, country in (("Washington", "United States"), ("Victoria", "Canada")):
            with self.subTest(city=city):
                expected = {"cities": [city], "countries": [country]}
                self.assertEqual(
                    ls.location_scope_from_plan(
                        {
                            "search_scope": {
                                "location": ls.canonical_location_label(expected),
                                "filters": expected,
                            },
                        }
                    )[1],
                    expected,
                )

    def test_approved_scope_rejects_noncanonical_or_conflicting_filters(self):
        with self.assertRaisesRegex(ValueError, "canonical values"):
            ls.location_scope_from_plan(
                {"search_scope": {"location": "California", "filters": {"states": ["CA"], "countries": ["US"]}}}
            )
        with self.assertRaisesRegex(ValueError, "conflict"):
            ls.location_scope_from_plan(
                {"search_scope": {"location": "San Francisco Bay Area", "filters": {"countries": ["Germany"]}}}
            )
        for location, filters in (
            ("Americas", {"macro_regions": ["APAC"]}),
            ("Middle East", {"macro_regions": ["Western Europe"]}),
            ("United States", {"countries": ["United States", "Germany"]}),
            ("London, UK", {"cities": ["London"], "countries": ["United Kingdom", "Canada"]}),
        ):
            with self.subTest(location=location):
                with self.assertRaises(ValueError):
                    ls.location_scope_from_plan({"search_scope": {"location": location, "filters": filters}})

    def test_cross_country_multi_office_scope_must_use_metros(self):
        with self.assertRaisesRegex(ValueError, "exactly one country"):
            ls.location_scope_from_plan(
                {
                    "search_scope": {
                        "location": "Vancouver or Portland",
                        "filters": {
                            "cities": ["Vancouver", "Portland"],
                            "countries": ["Canada", "United States"],
                        },
                    }
                }
            )
        metros = {"metro_areas": ["Vancouver Metropolitan Area", "Portland Metropolitan Area"]}
        label = ls.canonical_location_label(metros)
        self.assertEqual(
            ls.location_scope_from_plan({"search_scope": {"location": label, "filters": metros}}),
            (label, metros),
        )
        with self.assertRaisesRegex(ValueError, "conflict|broaden"):
            ls.location_scope_from_plan(
                {
                    "search_scope": {"location": "Vancouver or Portland", "filters": metros},
                }
            )

    def test_reviewed_label_cannot_be_silently_broadened_or_contradicted(self):
        cases = (
            ("San Francisco Bay Area", {"countries": ["United States"]}),
            ("London, UK", {"countries": ["United Kingdom"]}),
            ("California", {"countries": ["United States"]}),
            ("San Francisco, CA", {"states": ["California"], "countries": ["United States"]}),
            ("San Francisco", {"countries": ["Germany"]}),
            ("San Francisco", {"metro_areas": ["San Francisco Bay Area"]}),
            ("Remote US", {"countries": ["Germany"]}),
        )
        for location, filters in cases:
            with self.subTest(location=location):
                with self.assertRaisesRegex(ValueError, "conflict|broaden"):
                    ls.location_scope_from_plan(
                        {
                            "search_scope": {"location": location, "filters": filters},
                        }
                    )

    def test_ambiguous_state_abbreviations_need_country_context(self):
        with self.assertRaisesRegex(ValueError, "country"):
            ls.canonicalize_generated_location_filters("Perth, WA", {"cities": ["Perth"]})
        self.assertEqual(
            ls.canonicalize_generated_location_filters(
                "Perth, WA",
                {"cities": ["Perth"], "countries": ["Australia"]},
            ),
            {"cities": ["Perth"], "countries": ["Australia"]},
        )
        self.assertEqual(
            ls.location_fit(
                {"cities": ["Perth"], "countries": ["Australia"]},
                "Perth, WA",
            ),
            "unknown",
        )

    def test_broad_continent_scopes_are_country_unions_for_both_backends(self):
        africa = ls.canonicalize_generated_location_filters("Africa", {"macro_regions": ["Africa"]})
        oceania = ls.canonicalize_generated_location_filters("Oceania", {"macro_regions": ["Oceania"]})
        latin_america = ls.canonicalize_generated_location_filters(
            "Latin America",
            {"macro_regions": ["Latin America"]},
        )
        self.assertIn("Ghana", africa["countries"])
        self.assertIn("Australia", oceania["countries"])
        self.assertIn("Brazil", latin_america["countries"])
        self.assertNotIn("United States", latin_america["countries"])
        mixed = ls.canonicalize_generated_location_filters(
            "Africa or Middle East",
            {"macro_regions": ["Africa", "Middle East"]},
        )
        self.assertIn("Ghana", mixed["countries"])
        self.assertIn("Israel", mixed["countries"])


class TestPlanCritic(unittest.TestCase):
    def test_conjunctive_core_group_is_flagged(self):
        # Measured on the audited benchmark: an all-of-3 group cut a validated 22-person
        # shortlist to 1. Every conjunction must surface at Review.
        plan = {
            "hire_stage": "founding_early",
            "traits": {"must_have": [{"trait": c, "tier": "core"} for c in "abcd"]},
            "core_groups": [{"name": "mega", "all_of": ["a", "b", "c", "d"]}],
        }
        for traits in (["a", "b"], ["a", "b", "c"], ["a", "b", "c", "d"]):
            with self.subTest(n=len(traits)):
                plan["core_groups"] = [{"name": "mega", "all_of": traits}]
                issues = pc.deterministic_checks(plan)
                self.assertTrue(any(f"ALL {len(traits)} traits" in i for i in issues), issues)
        # per-trait groups (the measured default) pass clean
        plan["core_groups"] = [{"name": f"g{c}", "all_of": [c]} for c in "abcd"]
        self.assertEqual([i for i in pc.deterministic_checks(plan) if "ALL" in i], [])

    def test_empty_powerset_set_id_surfaces_at_review(self):
        plan = {
            "hire_stage": "founding_early",
            "route": "deep",
            "set_scope": {"set_id": ""},
            "traits": {"must_have": [{"trait": "x", "tier": "core"}]},
            "core_groups": [{"name": "g", "all_of": ["x"]}],
        }
        issues = pc.deterministic_checks(plan, backend="powerset")
        self.assertTrue(any("set_scope.set_id is empty" in i for i in issues), issues)
        self.assertFalse(any("set_scope.set_id is empty" in i for i in pc.deterministic_checks(plan, backend="local")))

    def test_critic_omits_temperature_for_reasoning_model_families(self):
        self.assertFalse(pc.supports_custom_temperature("gpt-5.4"))
        self.assertFalse(pc.supports_custom_temperature("o4-mini"))
        self.assertTrue(pc.supports_custom_temperature("gpt-4o"))

    def test_deterministic_checks_flag_off_enum_stage_and_missing_core(self):
        issues = pc.deterministic_checks(
            {
                "hire_stage": "growth",
                "search_scope": {"location": None, "filters": {}},
                "traits": {"must_have": [{"trait": "x", "tier": "table_stakes"}]},
            }
        )
        self.assertEqual(len(issues), 2)
        self.assertIn("off-enum", issues[0])
        self.assertIn("core", issues[1])

    def test_deterministic_checks_pass_valid_plan(self):
        issues = pc.deterministic_checks(
            {
                "hire_stage": "founding_early",
                "search_scope": {"location": None, "filters": {}},
                "traits": {"must_have": [{"trait": "x", "tier": "core"}]},
                "core_groups": [{"name": "default", "all_of": ["x"]}],
            }
        )
        self.assertEqual(issues, [])


class TestBuildEvalInputs(unittest.TestCase):
    def test_plan_from_obj_shapes_traits_and_scope(self):
        plan = bei.plan_from_obj(
            {
                "job_title": "MTS",
                "normalized_archetype": "distsys engineer",
                "hire_stage": "scale",
                "usable_cutoff": "Senior IC in band.",
                "must_have": ["schedulers", "control plane", ""],
                "nice_to_have": ["gpus"],
            },
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="2026-01-01T00:00:00Z",
        )
        self.assertEqual([t["trait"] for t in plan["traits"]["must_have"]], ["schedulers", "control plane"])
        self.assertEqual(plan["traits"]["nice_to_have"], [{"trait": "gpus", "source": "jd"}])
        self.assertEqual(plan["set_scope"], {"name": "s", "set_id": "sid"})
        self.assertEqual(plan["normalized_archetype"], "distsys engineer")
        self.assertEqual(plan["hire_stage"], "scaling_late")
        self.assertEqual(plan["search_scope"], {"location": None, "filters": {}, "source": "jd"})
        self.assertFalse(plan["retrieval_ran"])

    def test_plan_from_obj_requires_reviewable_structured_location(self):
        base = {"must_have": [{"trait": "finance", "tier": "core"}]}
        inferred_europe = bei.plan_from_obj(
            {**base, "location": "Europe"},
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
        )
        self.assertEqual(
            inferred_europe["search_scope"]["filters"],
            {"macro_regions": ["Western Europe", "Eurasia"]},
        )
        plan = bei.plan_from_obj(
            {
                **base,
                "location": "Europe",
                "location_filters": {"macro_regions": ["Western Europe", "Eurasia"]},
            },
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
        )
        self.assertEqual(
            plan["search_scope"],
            {
                "location": "Europe",
                "filters": {"macro_regions": ["Western Europe", "Eurasia"]},
                "source": "jd",
            },
        )
        remote = bei.plan_from_obj(
            {**base, "location": "remote", "location_filters": {}},
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
        )
        self.assertEqual(remote["search_scope"], {"location": None, "filters": {}, "source": "jd"})

        remote_us = bei.plan_from_obj(
            {**base, "location": "remote", "location_filters": {"countries": ["US"]}},
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
        )
        self.assertEqual(
            remote_us["search_scope"],
            {"location": "United States", "filters": {"countries": ["United States"]}, "source": "jd"},
        )

        remote_multi = bei.plan_from_obj(
            {
                **base,
                "location": "remote",
                "location_filters": {"countries": ["US", "Canada"]},
            },
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
        )
        self.assertEqual(
            remote_multi["search_scope"],
            {
                "location": "United States or Canada",
                "filters": {"countries": ["United States", "Canada"]},
                "source": "jd",
            },
        )

        africa = bei.plan_from_obj(
            {**base, "location": "Africa", "location_filters": {"countries": ["Ghana"]}},
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
        )
        self.assertEqual(africa["search_scope"]["location"], "Africa")
        self.assertEqual(
            set(africa["search_scope"]["filters"]["countries"]),
            set(ls.CONTINENT_COUNTRIES["Africa"]),
        )
        remote_africa = bei.plan_from_obj(
            {**base, "location": "Remote Africa", "location_filters": {"macro_regions": ["Africa"]}},
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
        )
        self.assertEqual(remote_africa["search_scope"]["location"], "Africa")

        latin_america = bei.plan_from_obj(
            {
                **base,
                "location": "LATAM",
                "location_filters": {"macro_regions": ["Americas"]},
            },
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
        )
        self.assertEqual(latin_america["search_scope"]["location"], "Latin America")
        self.assertEqual(
            set(latin_america["search_scope"]["filters"]["countries"]),
            ls.LATIN_AMERICA_COUNTRIES,
        )

        nyc = bei.plan_from_obj(
            {
                **base,
                "location": "New York City",
                "location_filters": {"cities": ["New York City"], "countries": ["US"]},
            },
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
        )
        self.assertEqual(
            nyc["search_scope"],
            {
                "location": "New York, United States",
                "filters": {"cities": ["New York"], "countries": ["United States"]},
                "source": "jd",
            },
        )
        with self.assertRaisesRegex(ValueError, "conflict|broaden"):
            bei.plan_from_obj(
                {
                    **base,
                    "location": "San Francisco",
                    "location_filters": {"countries": ["Germany"]},
                },
                set_name="s",
                set_id="sid",
                source_url=None,
                created_at="t",
            )

    def test_generated_location_accepts_natural_exact_or_labels_and_metro_aliases(self):
        base = {"must_have": [{"trait": "finance", "tier": "core"}]}
        cases = (
            (
                "Vancouver or Portland",
                {"metro_areas": ["Vancouver Metropolitan Area", "Portland Metropolitan Area"]},
            ),
            (
                "New York and Boston",
                {"metro_areas": ["New York Metropolitan Area", "Boston Metropolitan Area"]},
            ),
            (
                "New York, Boston, or Chicago",
                {
                    "metro_areas": [
                        "New York Metropolitan Area",
                        "Boston Metropolitan Area",
                        "Chicago Metropolitan Area",
                    ]
                },
            ),
            (
                "Bay Area, New York, or Boston",
                {
                    "metro_areas": [
                        "San Francisco Bay Area",
                        "New York Metropolitan Area",
                        "Boston Metropolitan Area",
                    ]
                },
            ),
            (
                "San Francisco, CA or New York, NY",
                {"metro_areas": ["San Francisco Bay Area", "New York Metropolitan Area"]},
            ),
            (
                "New York, Boston, or Chicago",
                {
                    "cities": ["New York", "Boston", "Chicago"],
                    "countries": ["United States"],
                },
            ),
            ("US and Canada", {"countries": ["United States", "Canada"]}),
            (
                "California, Texas, or New York",
                {
                    "states": ["California", "Texas", "New York"],
                    "countries": ["United States"],
                },
            ),
            ("Silicon Valley", {"metro_areas": ["San Francisco Bay Area"]}),
            ("NYC metro", {"metro_areas": ["New York Metropolitan Area"]}),
            ("Tri-state area", {"metro_areas": ["New York Metropolitan Area"]}),
        )
        for location, filters in cases:
            with self.subTest(location=location):
                plan = bei.plan_from_obj(
                    {**base, "location": location, "location_filters": filters},
                    set_name="s",
                    set_id="sid",
                    source_url=None,
                    created_at="t",
                )
                self.assertEqual(plan["search_scope"]["filters"], filters)
                self.assertEqual(
                    plan["search_scope"]["location"],
                    ls.canonical_location_label(filters),
                )

    def test_generated_location_rejects_broader_or_wrong_alternatives(self):
        base = {"must_have": [{"trait": "finance", "tier": "core"}]}
        with self.assertRaisesRegex(ValueError, "conflict|broaden"):
            bei.plan_from_obj(
                {
                    **base,
                    "location": "San Francisco or New York",
                    "location_filters": {
                        "metro_areas": [
                            "San Francisco Bay Area",
                            "New York Metropolitan Area",
                            "Boston Metropolitan Area",
                        ]
                    },
                },
                set_name="s",
                set_id="sid",
                source_url=None,
                created_at="t",
            )

    def test_missing_archetype_falls_back_to_role_not_engineer(self):
        plan = bei.plan_from_obj(
            {"job_title": "Strategic Finance Lead", "must_have": [{"trait": "operating P&L", "tier": "core"}]},
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
        )
        self.assertEqual(plan["normalized_archetype"], "Strategic Finance Lead")

    def test_plan_from_obj_user_preferences_override_jd_and_record_provenance(self):
        plan = bei.plan_from_obj(
            {
                "hire_stage": "growth",
                "must_have": [{"trait": "systems", "tier": "core"}],
            },
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
            user_preferences={"hire_stage": "early", "pedigree_policy": "ignore"},
        )
        policy = plan["recruiter_policy"]
        self.assertEqual(plan["hire_stage"], "founding_early")
        self.assertEqual(policy["preferences"]["pedigree_policy"], "ignore")
        self.assertEqual(policy["provenance"]["hire_stage"]["source"], "user")
        self.assertEqual(policy["provenance"]["pedigree_policy"]["source"], "user")

    def test_plan_from_obj_extracts_only_explicit_jd_preferences_below_user(self):
        plan = bei.plan_from_obj(
            {
                "hire_stage": "growth",
                "must_have": [{"trait": "systems", "tier": "core"}],
                "recruiter_preferences": {
                    "pedigree_policy": "ignore",
                    "current_founder_c_suite_for_non_exec_ic": "review",
                },
            },
            set_name="s",
            set_id="sid",
            source_url=None,
            created_at="t",
            user_preferences={"current_founder_c_suite_for_non_exec_ic": "eligible"},
        )
        policy = plan["recruiter_policy"]
        self.assertEqual(policy["preferences"]["pedigree_policy"], "ignore")
        self.assertEqual(policy["provenance"]["pedigree_policy"]["source"], "jd")
        self.assertEqual(policy["preferences"]["current_founder_c_suite_for_non_exec_ic"], "eligible")
        self.assertEqual(
            policy["provenance"]["current_founder_c_suite_for_non_exec_ic"]["source"],
            "user",
        )

    def test_plan_from_obj_requires_must_have(self):
        with self.assertRaises(ValueError):
            bei.plan_from_obj({"must_have": []}, set_name="s", set_id="i", source_url=None, created_at="t")

    def test_plan_target_level_valid_passes_through(self):
        plan = bei.plan_from_obj(
            {"must_have": ["x"], "target_level": "VP"}, set_name="s", set_id="i", source_url=None, created_at="t"
        )
        self.assertEqual(plan["target_level"], "vp")

    def test_plan_target_level_invalid_defaults_to_senior_ic(self):
        plan = bei.plan_from_obj(
            {"must_have": ["x"], "target_level": "supreme_overlord"},
            set_name="s",
            set_id="i",
            source_url=None,
            created_at="t",
        )
        self.assertEqual(plan["target_level"], "senior_ic")

    def test_plan_target_level_absent_defaults_to_senior_ic(self):
        plan = bei.plan_from_obj({"must_have": ["x"]}, set_name="s", set_id="i", source_url=None, created_at="t")
        self.assertEqual(plan["target_level"], "senior_ic")

    def test_build_plan_messages_carries_jd(self):
        msgs = bei.build_plan_messages("Design schedulers")
        self.assertIn("Design schedulers", msgs[-1]["content"])

    def test_must_trait_tagged_object_preserves_tier(self):
        self.assertEqual(
            bei._must_trait({"trait": "distributed systems", "tier": "core"}),
            {"trait": "distributed systems", "tier": "core", "source": "jd"},
        )

    def test_must_trait_invalid_tier_defaults_table_stakes(self):
        # A mis-tagged/absent tier must NOT over-gate -> degrade to table_stakes (gate falls back).
        self.assertEqual(bei._must_trait({"trait": "x", "tier": "bogus"})["tier"], "table_stakes")
        self.assertEqual(bei._must_trait({"trait": "x"})["tier"], "table_stakes")

    def test_must_trait_bare_string_is_table_stakes(self):
        self.assertEqual(bei._must_trait("schedulers"), {"trait": "schedulers", "tier": "table_stakes", "source": "jd"})
        self.assertIsNone(bei._must_trait("   "))

    def test_plan_from_obj_carries_core_tier(self):
        plan = bei.plan_from_obj(
            {
                "must_have": [
                    {"trait": "fusion hardware", "tier": "core"},
                    {"trait": "leadership", "tier": "table_stakes"},
                ]
            },
            set_name="s",
            set_id="i",
            source_url=None,
            created_at="t",
        )
        tiers = {t["trait"]: t["tier"] for t in plan["traits"]["must_have"]}
        self.assertEqual(tiers, {"fusion hardware": "core", "leadership": "table_stakes"})

    def test_plan_core_groups_are_alternative_all_of_gates(self):
        plan = bei.plan_from_obj(
            {
                "must_have": [
                    {"trait": "distributed schedulers", "tier": "core"},
                    {"trait": "control planes", "tier": "core"},
                    {"trait": "inference serving", "tier": "core"},
                ],
                "core_groups": [
                    {"name": "scheduler", "all_of": ["distributed schedulers", "control planes"]},
                    {"name": "inference", "all_of": ["inference serving"]},
                ],
            },
            set_name="s",
            set_id="i",
            source_url=None,
            created_at="t",
        )
        self.assertEqual(plan["core_groups"][0]["all_of"], ["distributed schedulers", "control planes"])
        self.assertEqual(plan["core_groups"][1]["all_of"], ["inference serving"])

    def test_generated_plan_conforms_to_published_schema(self):
        plan = bei.plan_from_obj(
            {
                "job_title": "Staff Engineer",
                "normalized_archetype": "systems engineer",
                "hire_stage": "growth",
                "location": "San Francisco Bay Area",
                "location_filters": {"metro_areas": ["San Francisco Bay Area"]},
                "must_have": [{"trait": "distributed systems", "tier": "core"}],
                "nice_to_have": ["GPU infrastructure"],
            },
            set_name="team",
            set_id="set-1",
            source_url=None,
            created_at="2026-07-10T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plan.json"
            path.write_text(json.dumps(plan))
            cp = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "packs/search/primitives/validate_artifact/validate_artifact.py"),
                    "--schema",
                    "search-network-jd-plan",
                    "--file",
                    str(path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(cp.returncode, 0, cp.stderr + cp.stdout)


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
            with (
                mock.patch.object(cj_judge.EV, "read_json", return_value={"traits": {"must_have": []}}),
                mock.patch.object(cj_judge.EV, "load_frontier", return_value=[candidate]),
                mock.patch.object(cj_judge.EV, "collect_profiles", return_value={"p1": {"person_id": "p1"}}),
                mock.patch.object(cj_judge.EV, "build_user_prompt", return_value="profile prompt"),
                mock.patch.object(cj_judge, "judge_one", return_value=({}, "codex_exit_1: auth")),
            ):
                with self.assertRaises(SystemExit) as ctx:
                    cj_judge.main()
            self.assertEqual(ctx.exception.code, 1)
        finally:
            sys.argv = argv
        self.assertTrue((d / "candidate_evaluations.raw.jsonl").exists())


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
            self.assertEqual(src["requested_url"], "https://example.test/job")
            self.assertEqual(src["source_url"], "https://example.test/job")
            self.assertEqual(src["source_title"], "Role X")
            self.assertIn("fetched_at", src)

    def test_main_thin_content_still_writes_and_warns(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "jd.txt"
            argv = sys.argv
            sys.argv = ["fetch_jd", "--url", "https://example.test/js", "--out", str(out)]
            try:
                with mock.patch.object(
                    fj, "fetch", return_value=("<html><body>App</body></html>", "https://example.test/js")
                ):
                    fj.main()  # thin is not a failure -> no SystemExit
            finally:
                sys.argv = argv
            self.assertTrue(out.exists())


class TestFetchJDAshby(unittest.TestCase):
    """fetch_ashby early-outs (no network in either case)."""

    def test_non_ashby_host_returns_none(self):
        self.assertIsNone(fj.fetch_ashby("https://jobs.lever.co/acme/2e718684-4f75-4a99-8d6b-3b6bd44e4228"))

    def test_ashby_url_without_job_uuid_returns_none(self):
        self.assertIsNone(fj.fetch_ashby("https://jobs.ashbyhq.com/supabase"))


if __name__ == "__main__":
    unittest.main()
