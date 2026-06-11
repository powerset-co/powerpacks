import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "packs/search/primitives/expand_search_request/parallel_extractors.py"


def load_module():
    spec = importlib.util.spec_from_file_location("parallel_extractors_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class ExpandSearchRequestTests(unittest.TestCase):
    def test_role_agent_prompt_uses_taxonomy_and_prod_shape(self):
        mod = load_module()

        prompt = mod.role_agent_system_prompt()

        self.assertIn("engineering:", prompt)
        self.assertIn("software_engineer", prompt)
        self.assertIn("full_stack_engineer", prompt)
        self.assertNotIn("frontend_engineer, fullstack_engineer", prompt)
        self.assertIn("Return JSON with exactly these keys: semantic_query, bm25_queries, role_ids, departments, seniority", prompt)
        self.assertEqual(mod.role_agent_user_content("engineering leaders"), 'Query: "engineering leaders"')

    def test_sf_in_phrase_adds_person_city(self):
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

        self.assertEqual(filters["cities"], ["San Francisco"])
        self.assertNotIn("company_cities", filters)

    def test_sf_company_phrase_adds_company_city(self):
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

        self.assertEqual(filters["company_cities"], ["San Francisco"])
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

    def test_role_id_title_injections_add_generic_bm25_aliases(self):
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

        self.assertIn("Member of Technical Staff", filters["bm25_queries"])

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

    def test_city_filters_expand_to_prod_alias_lists(self):
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

        self.assertEqual(
            filters["cities"],
            ["New York City", "New York City, New York", "New York"],
        )

        unmapped = mod._expand_city_filter_aliases(["Austin", "  ", "Austin"])
        self.assertEqual(unmapped, ["Austin"])

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
