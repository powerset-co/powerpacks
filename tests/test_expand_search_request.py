import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "packs/search/primitives/expand_search_request/parallel_extractors.py"


def load_module():
    spec = importlib.util.spec_from_file_location("parallel_extractors_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class ExpandSearchRequestTests(unittest.TestCase):
    def test_parallel_expansion_preserves_standard_trait_and_domain_contract(self):
        mod = load_module()

        async def fake_extract(client, name, system_prompt, query, model=None, reasoning_effort=None):
            if name == "trait_generation":
                return {
                    "traits": [{
                        "value": "Software engineer",
                        "temporal": "current",
                        "meaning": "role",
                    }],
                    "has_domain_intent": False,
                }
            if name == "role":
                return {"semantic_query": "Software engineers building production systems.",
                        "bm25_queries": ["software engineer"]}
            return {}

        with mock.patch.object(mod, "make_async_openai_client", return_value=object()), \
             mock.patch.object(mod, "_extract", side_effect=fake_extract):
            result = asyncio.run(mod.expand_query_parallel(
                "software engineers", api_key="test", model_override="gpt-5.6-luna",
                reasoning_effort="medium"))

        self.assertEqual(result["traits"], [{
            "value": "Software engineer",
            "temporal": "current",
            "meaning": "role",
        }])
        self.assertIs(result["has_domain_intent"], False)
        self.assertIs(result["role_search_filters"]["has_domain_intent"], False)

    def test_complete_prompt_bundle_can_be_edited_as_files(self):
        mod = load_module()
        shipped = mod.load_prompt_bundle()
        self.assertEqual(set(shipped), set(mod.PROMPT_NAMES))
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            for name, body in shipped.items():
                (directory / f"{name}.txt").write_text(
                    ("edited role prompt" if name == "role" else body), encoding="utf-8")
            edited = mod.load_prompt_bundle(directory)
        self.assertEqual(edited["role"], "edited role prompt")
        self.assertEqual(edited["company"], shipped["company"])

    def test_standalone_expand_keeps_existing_neutral_defaults(self):
        spec = importlib.util.spec_from_file_location(
            "expand_search_request_defaults_test",
            ROOT / "packs/search/primitives/expand_search_request/expand_search_request.py",
        )
        expand = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(expand)  # type: ignore[union-attr]
        self.assertEqual(expand.DEFAULT_MODEL, "gpt-4o-mini")
        self.assertIsNone(expand.DEFAULT_REASONING_EFFORT)

    def test_role_agent_prompt_uses_taxonomy_and_prod_shape(self):
        mod = load_module()

        prompt = mod.role_agent_system_prompt()

        self.assertIn("engineering:", prompt)
        self.assertIn("software_engineer", prompt)
        self.assertIn("full_stack_engineer", prompt)
        self.assertNotIn("frontend_engineer, fullstack_engineer", prompt)
        self.assertIn("Parse the candidate population separately", prompt)
        self.assertIn("never add it to role_ids, bm25_queries, departments, or seniority", prompt)
        self.assertIn("Return JSON with exactly these keys: semantic_query, bm25_queries, role_ids, departments, seniority", prompt)
        self.assertEqual(mod.role_agent_user_content("engineering leaders"), 'Query: "engineering leaders"')

    def test_sf_in_phrase_prefers_person_metro(self):
        mod = load_module()
        filters = mod._merge(
            {"semantic_query": "software engineering work", "bm25_queries": ["software engineer"]},
            {},
            {},
            {},
            {},
            {},
            {},
            "swe in sf",
        )

        self.assertEqual(filters["metro_areas"], ["San Francisco Bay Area"])
        self.assertNotIn("cities", filters)
        self.assertNotIn("company_cities", filters)

    def test_sf_company_phrase_prefers_company_metro(self):
        mod = load_module()
        filters = mod._merge(
            {"semantic_query": "software engineering work", "bm25_queries": ["software engineer"]},
            {},
            {},
            {},
            {},
            {},
            {},
            "software engineers at sf companies",
        )

        self.assertEqual(filters["company_metro_areas"], ["San Francisco Bay Area"])
        self.assertNotIn("company_cities", filters)
        self.assertNotIn("cities", filters)

    def test_founder_role_expansion_matches_prod_shortcut_shape(self):
        mod = load_module()
        filters = mod._merge(
            {"semantic_query": "founders", "bm25_queries": ["founder"]},
            {},
            {},
            {},
            {},
            {"seniority_bands": ["c_suite"]},
            {},
            "founders at devtools companies",
        )

        self.assertIn("founder", filters["role_ids"])
        self.assertIn("co-founder", filters["bm25_queries"])
        self.assertIn("founding", filters["bm25_queries"])
        self.assertIn("CEO", filters["bm25_queries"])
        self.assertIn("Chief Executive Officer", filters["bm25_queries"])
        self.assertEqual(filters["role_function"], "founder")
        self.assertNotIn("seniority_bands", filters)
        self.assertIn("role_core_patterns", filters)
        self.assertIn("founder", filters["role_core_patterns"][0]["examples"])

    def test_founder_csuite_query_keeps_founder_role_id_filter_precise(self):
        mod = load_module()
        filters = mod._merge(
            {
                "semantic_query": "founder executives",
                "bm25_queries": ["founder CEO"],
                "role_ids": ["founder", "chief_executive_officer"],
            },
            {},
            {},
            {},
            {},
            {},
            {},
            "founder CEOs at devtools companies",
        )

        self.assertEqual(filters["role_ids"], ["founder"])
        self.assertIn("CEO", filters["bm25_queries"])
        self.assertIn("Chief Executive Officer", filters["bm25_queries"])
        self.assertEqual(filters["role_function"], "founder")

    def test_csuite_role_expansion_adds_canonical_ids_and_aliases(self):
        mod = load_module()
        filters = mod._merge(
            {"semantic_query": "technology executives", "bm25_queries": ["technology executive"]},
            {},
            {},
            {},
            {},
            {},
            {},
            "CTOs at AI startups",
        )

        self.assertIn("chief_technology_officer", filters["role_ids"])
        self.assertIn("CTO", filters["bm25_queries"])
        self.assertIn("Chief Technology Officer", filters["bm25_queries"])
        self.assertEqual(filters["seniority_bands"], ["c-suite"])
        self.assertEqual(filters["role_function"], "leader")
        self.assertIn("role_core_patterns", filters)

    def test_csuite_detection_handles_ciso_singular_and_plural(self):
        mod = load_module()

        for query in ("CISO at security companies", "CISOs at security companies"):
            filters = mod._merge(
                {"semantic_query": "security executives", "bm25_queries": ["security executive"]},
                {},
                {},
                {},
                {},
                {},
                {},
                query,
            )
            self.assertIn("chief_information_security_officer", filters["role_ids"])
            self.assertIn("CISO", filters["bm25_queries"])
            self.assertIn("Chief Information Security Officer", filters["bm25_queries"])

    def test_relationship_target_does_not_become_candidate_role_or_seniority(self):
        mod = load_module()
        filters = mod._merge(
            {
                "semantic_query": "Executive administrative support.",
                "bm25_queries": ["executive assistant", "assistant to the CEO", "CEO"],
                "role_ids": ["admin_assistant", "chief_executive_officer"],
                "seniority": ["c_suite"],
            },
            {},
            {"cities": ["Stockholm"]},
            {},
            {},
            {"seniority_bands": []},
            {},
            "Executive Assistant to the CEO in Stockholm",
        )

        self.assertEqual(filters["role_ids"], ["admin_assistant"])
        self.assertIn("executive assistant", filters["bm25_queries"])
        self.assertIn("assistant to the CEO", filters["bm25_queries"])
        self.assertNotIn("CEO", filters["bm25_queries"])
        self.assertNotIn("seniority_bands", filters)

    def test_role_agent_seniority_and_departments_are_consumed(self):
        mod = load_module()
        filters = mod._merge(
            {
                "semantic_query": "engineering leadership across software teams",
                "bm25_queries": ["engineering leader"],
                "role_ids": ["engineering_manager", "chief_technology_officer"],
                "departments": ["engineering"],
                "seniority": ["director", "vice-president"],
            },
            {},
            {},
            {},
            {},
            {},
            {},
            "engineering leadership",
        )

        self.assertEqual(filters["role_departments"], ["engineering"])
        self.assertEqual(filters["seniority_bands"], ["director", "vice-president"])

    def test_seniority_extractor_overrides_role_agent_seniority_when_present(self):
        mod = load_module()
        filters = mod._merge(
            {
                "semantic_query": "software engineering work",
                "bm25_queries": ["software engineer"],
                "role_ids": ["software_engineer"],
                "seniority": ["director"],
            },
            {},
            {},
            {},
            {},
            {"seniority_bands": ["vice-president"]},
            {},
            "vp software engineers",
        )

        self.assertEqual(filters["seniority_bands"], ["vice-president"])

    def test_role_id_title_injections_do_not_widen_software_pond_to_mts(self):
        mod = load_module()
        filters = mod._merge(
            {
                "semantic_query": "software engineers building production systems",
                "bm25_queries": ["software engineer"],
                "role_ids": ["software_engineer"],
            },
            {},
            {},
            {},
            {},
            {},
            {},
            "software engineers",
        )

        self.assertNotIn("Member of Technical Staff", filters["bm25_queries"])

    def test_hillclimbed_prompts_preserve_explicit_filter_boundaries(self):
        mod = load_module()
        prompts = mod.load_prompt_bundle()

        self.assertIn("GRAMMATICAL EMPLOYER BOUNDARY", prompts["company"])
        self.assertIn("EDUCATION RELATIONSHIP REQUIRED", prompts["education"])
        self.assertIn("return an empty list", prompts["seniority"])
        self.assertIn("explicitly states a numeric duration", prompts["temporal"])
        self.assertIn("Never emit a location trait", prompts["trait_generation"])
        self.assertIn("3-8 observable titles or true title aliases", prompts["role"])

    def test_temporal_current_flag_maps_to_local_current_filters(self):
        mod = load_module()
        filters = mod._merge(
            {"semantic_query": "technology executives", "bm25_queries": ["technology executive"]},
            {},
            {},
            {},
            {"is_current": True},
            {},
            {},
            "currently CTOs",
        )

        self.assertTrue(filters["is_current_role"])
        self.assertTrue(filters["is_current_company"])

    def test_temporal_past_flag_maps_to_local_current_filters(self):
        mod = load_module()
        filters = mod._merge(
            {"semantic_query": "software engineering work", "bm25_queries": ["software engineer"]},
            {"company_names": ["Stripe"]},
            {},
            {},
            {"is_current": False},
            {},
            {},
            "ex-Stripe engineers",
        )

        self.assertFalse(filters["is_current_role"])
        self.assertFalse(filters["is_current_company"])

    def test_city_filters_prefer_unambiguous_indexed_metro(self):
        mod = load_module()
        filters = mod._merge(
            {"semantic_query": "blockchain engineering work", "bm25_queries": ["blockchain engineer"]},
            {},
            {"cities": ["New York City"]},
            {},
            {},
            {},
            {},
            "blockchain engineers in new york",
        )

        self.assertEqual(filters["metro_areas"], ["New York Metropolitan Area"])
        self.assertNotIn("cities", filters)

        unmapped = mod._expand_city_filter_aliases(["Austin", "  ", "Austin"])
        self.assertEqual(unmapped, ["Austin"])

    def test_city_filters_fall_back_exact_when_any_city_is_not_unambiguous(self):
        mod = load_module()
        filters = mod._merge(
            {"semantic_query": "engineering work", "bm25_queries": ["engineer"]},
            {},
            {"cities": ["San Francisco", "Raleigh"]},
            {},
            {},
            {},
            {},
            "engineers in San Francisco or Raleigh",
        )

        self.assertEqual(filters["cities"], ["San Francisco", "Raleigh"])
        self.assertNotIn("metro_areas", filters)

    def test_seniority_bands_normalize_to_canonical_index_values(self):
        mod = load_module()

        self.assertEqual(
            mod._normalize_seniority_bands(
                ["c_suite", "C-Suite", "vice_president", "Vice President", "senior", "Senior_IC"]
            ),
            ["c-suite", "vice-president", "senior", "senior_ic"],
        )
        self.assertEqual(mod._normalize_seniority_bands([]), [])
        self.assertEqual(mod._normalize_seniority_bands([None, ""]), [])

    def test_entity_and_sector_vocab_stay_in_sync_with_indexing_enum(self):
        # The extraction prompt and validator must only offer entity/sector tags
        # that exist in the index. enrich_companies_checkpointed owns the enums.
        spec = importlib.util.spec_from_file_location(
            "enrich_companies_checkpointed_test",
            ROOT / "packs/indexing/primitives/enrich_companies_checkpointed/enrich_companies_checkpointed.py",
        )
        enrich = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(enrich)  # type: ignore[union-attr]

        expand_spec = importlib.util.spec_from_file_location(
            "expand_search_request_vocab_test",
            ROOT / "packs/search/primitives/expand_search_request/expand_search_request.py",
        )
        expand = importlib.util.module_from_spec(expand_spec)
        expand_spec.loader.exec_module(expand)  # type: ignore[union-attr]

        warnings = expand.validate_output(
            {"role_search_filters": {
                "entity_types": sorted(enrich.OBSERVED_ENTITY_TYPES),
                "sector_types": sorted(enrich.OBSERVED_SECTOR_TYPES),
            }}
        )
        self.assertEqual(
            [w for w in warnings if "entity" in w or "sector" in w], []
        )
        self.assertEqual(
            expand.validate_output({"role_search_filters": {"entity_types": ["public_company"]}}),
            ["invalid entity_type: public_company"],
        )
        self.assertEqual(
            expand.validate_output({"role_search_filters": {"sector_types": ["mobility_av"]}}),
            ["invalid sector_type: mobility_av"],
        )

        prompt = (
            ROOT / "packs/search/primitives/expand_search_request/prompts/company.txt"
        ).read_text(encoding="utf-8")
        for tag in sorted(enrich.OBSERVED_ENTITY_TYPES | enrich.OBSERVED_SECTOR_TYPES):
            self.assertIn(tag, prompt, f"prompt missing canonical tag: {tag}")
        for stale in ("public_company", "private_company", "non_profit,", "mobility_av", "food_ag_tech"):
            self.assertNotIn(stale, prompt, f"prompt still offers nonexistent tag: {stale}")

    def test_seniority_values_stay_in_sync_with_indexing_enum(self):
        # The extraction layer must only emit seniority_band values that exist in
        # the index. enrich_roles_checkpointed owns the canonical enum.
        spec = importlib.util.spec_from_file_location(
            "enrich_roles_checkpointed_test",
            ROOT / "packs/indexing/primitives/enrich_roles_checkpointed/enrich_roles_checkpointed.py",
        )
        enrich = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(enrich)  # type: ignore[union-attr]
        canonical = set(enrich.VALID_SENIORITY_BANDS)

        mod = load_module()
        self.assertLessEqual(set(mod._SENIORITY_CANONICAL.values()), canonical)
        normalized = set(mod._normalize_seniority_bands(sorted(canonical)))
        self.assertEqual(normalized, canonical)

        expand_spec = importlib.util.spec_from_file_location(
            "expand_search_request_test",
            ROOT / "packs/search/primitives/expand_search_request/expand_search_request.py",
        )
        expand = importlib.util.module_from_spec(expand_spec)
        expand_spec.loader.exec_module(expand)  # type: ignore[union-attr]
        warnings = expand.validate_output(
            {"role_search_filters": {"seniority_bands": sorted(canonical)}}
        )
        self.assertEqual([w for w in warnings if "seniority" in w], [])


if __name__ == "__main__":
    unittest.main()
