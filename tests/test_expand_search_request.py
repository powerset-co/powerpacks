import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.search.primitives.deep_search.location_scope import query_location_label

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "packs/search/primitives/expand_search_request/parallel_extractors.py"


def load_module():
    spec = importlib.util.spec_from_file_location("parallel_extractors_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class ExpandSearchRequestTests(unittest.TestCase):
    def _expand_with_model_answers(self, answers, *, query="fintech engineers", **kwargs):
        mod = load_module()
        prompts = mod.load_prompt_bundle()
        names_by_prompt = {body: name for name, body in prompts.items()}
        started = set()
        ready = asyncio.Event()

        async def complete(**request):
            name = names_by_prompt[request["messages"][0]["content"]]
            started.add(name)
            if len(started) == len(prompts):
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=1)
            answer = answers.get(name, {})
            if isinstance(answer, Exception):
                raise answer
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(answer)))])

        create = mock.AsyncMock(side_effect=complete)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.object(mod, "make_async_openai_client", return_value=client):
            result = asyncio.run(mod.expand_query_parallel(query, api_key="test", **kwargs))
        return result, create

    def test_person_location_model_answers_preserve_city_granularity(self):
        cases = [
            ("marketing managers in London", {"cities": ["London"]}, {"cities": ["London"]}),
            ("engineers in San Francisco", {"cities": ["San Francisco"]},
             {"cities": ["San Francisco"], "metro_areas": ["San Francisco Bay Area"]}),
            ("engineers in the Bay Area", {"metro_areas": ["San Francisco Bay Area"]},
             {"metro_areas": ["San Francisco Bay Area"]}),
            ("engineers in Greater Boston", {"metro_areas": ["Boston Metropolitan Area"]},
             {"metro_areas": ["Boston Metropolitan Area"]}),
            ("marketing managers in London", {"cities": ["London"], "states": ["England"],
             "countries": ["United Kingdom"], "macro_regions": ["Western Europe"]},
             {"cities": ["London"]}),
            ("engineers in Germany", {"countries": ["Germany"]}, {"countries": ["Germany"]}),
            ("engineers in California", {"states": ["California"], "countries": ["United States"]},
             {"states": ["California"], "countries": ["United States"]}),
            ("engineers in Europe", {"macro_regions": ["Western Europe", "Eurasia"]},
             {"macro_regions": ["Western Europe", "Eurasia"]}),
            ("engineers in London or San Francisco", {"cities": ["London", "San Francisco"]},
             {"cities": ["London", "San Francisco"], "metro_areas": ["San Francisco Bay Area"]}),
            ("engineers at SF companies", {"company_cities": ["San Francisco"]},
             {"company_metro_areas": ["San Francisco Bay Area"]}),
        ]
        for query, location, expected in cases:
            with self.subTest(query=query, location=location):
                result, _ = self._expand_with_model_answers({"location": location}, query=query)
                self.assertEqual(result["role_search_filters"], {**expected, "has_domain_intent": False})

    def test_dallas_omits_unsupported_table_metro_and_labels_for_deep_search(self):
        result, _ = self._expand_with_model_answers(
            {"location": {"cities": ["Dallas"]}}, query="engineers in Dallas",
        )
        filters = dict(result["role_search_filters"])
        filters.pop("has_domain_intent")
        label = query_location_label(filters)
        self.assertEqual(filters, {"cities": ["Dallas"]})
        self.assertIn("Dallas", label)

    def test_adjacency_and_interaction_model_answers_merge_without_changing_other_filters(self):
        answers = {
            "role": {"semantic_query": "Engineers building payment systems.", "bm25_queries": ["engineer"]},
            "company": {"sector_types": ["fintech"]},
            "trait_generation": {"traits": [{"value": "Payments experience"}], "has_domain_intent": True},
        }
        baseline, _ = self._expand_with_model_answers(answers)
        additions = {
            "company_adjacency_queries": ["CTO", "VP of Engineering"],
            "operator_interaction_min": 10,
            "operator_interaction_max": 50,
            "set_interaction_min": 100,
            "set_interaction_max": 200,
        }
        result, create = self._expand_with_model_answers({
            **answers,
            "company_adjacency": {"has_sector_context": True, "adjacency_queries": additions["company_adjacency_queries"]},
            "interaction": {key: value for key, value in additions.items() if key != "company_adjacency_queries"},
        }, model_override="gpt-5.4", reasoning_effort="medium")

        self.assertEqual(result["role_search_filters"], {**baseline["role_search_filters"], **additions})
        self.assertEqual(result["traits"], baseline["traits"])
        self.assertEqual(create.await_count, 10)
        self.assertEqual(set(result["extractor_timings"]), {
            "role", "company", "location", "education", "temporal", "seniority", "social",
            "trait_generation", "company_adjacency", "interaction",
        })
        for call in create.call_args_list:
            self.assertEqual(call.kwargs["model"], "gpt-5.4")
            self.assertEqual(call.kwargs["reasoning_effort"], "medium")

    def test_company_technology_and_customers_do_not_become_filters(self):
        company = {
            "company_names": ["Example Robotics"],
            "sector_types": ["software"],
            "entity_types": ["startup"],
            "company_semantic_queries": ["B2B robotics companies"],
            "headcount_min": 10,
        }
        answers = {"company": company, "trait_generation": {
            "traits": [{"value": "B2B robotics experience"}], "has_domain_intent": True,
        }}
        query = "engineers at B2B robotics companies"
        baseline, _ = self._expand_with_model_answers(answers, query=query)
        result, _ = self._expand_with_model_answers({
            **answers,
            "company": {**company, "technology_types": ["robotics"],
                        "customer_types": ["Business (B2B)"]},
        }, query=query)
        self.assertNotIn("technology_types", result["role_search_filters"])
        self.assertNotIn("customer_types", result["role_search_filters"])
        self.assertEqual(result["role_search_filters"], baseline["role_search_filters"])
        self.assertEqual(result["traits"], baseline["traits"])
        self.assertEqual(result["normalized_query"], query)

    def test_person_name_selects_lookup_without_another_model_call(self):
        result, create = self._expand_with_model_answers({
            "role": {"bm25_queries": ["engineer"]},
            "company": {"company_names": ["Stripe"]},
            "trait_generation": {"person_name": "Jordan Bravo"},
        }, query="who is Jordan Bravo")
        self.assertEqual(result["intent_type"], "person_lookup")
        self.assertEqual(result["person_name"], "Jordan Bravo")
        self.assertEqual(result["role_search_filters"], {})
        self.assertEqual(result["traits"], [])
        self.assertEqual(create.await_count, 10)

    def test_null_person_name_does_not_change_search_output(self):
        answers = {"role": {"bm25_queries": ["engineer"]},
                   "trait_generation": {"traits": [], "has_domain_intent": False}}
        baseline, _ = self._expand_with_model_answers(answers)
        result, _ = self._expand_with_model_answers({
            **answers, "trait_generation": {**answers["trait_generation"], "person_name": None},
        })
        for output in (baseline, result):
            output.pop("extractor_timings")
            output.pop("total_ms")
        self.assertEqual(result, baseline)
        self.assertNotIn("person_name", result)

    def test_failed_new_extractor_preserves_other_model_answers(self):
        answers = {
            "company": {"company_names": ["Stripe"]},
            "company_adjacency": {"has_sector_context": True, "adjacency_queries": ["CTO"]},
            "interaction": {"operator_interaction_min": 10},
        }
        for failed, missing, surviving in (
            ("company_adjacency", "company_adjacency_queries", "operator_interaction_min"),
            ("interaction", "operator_interaction_min", "company_adjacency_queries"),
        ):
            with self.subTest(failed=failed):
                result, _ = self._expand_with_model_answers({**answers, failed: RuntimeError("model unavailable")})
                filters = result["role_search_filters"]
                self.assertEqual(filters["company_names"], ["Stripe"])
                self.assertNotIn(missing, filters)
                self.assertIn(surviving, filters)

    def test_absent_interaction_and_sector_signals_leave_keys_absent(self):
        result, _ = self._expand_with_model_answers({
            "company_adjacency": {"has_sector_context": False, "adjacency_queries": ["CTO"]},
            "interaction": {"operator_interaction_min": None, "operator_interaction_max": None,
                            "set_interaction_min": None, "set_interaction_max": None},
        })
        self.assertEqual(result["role_search_filters"], {"has_domain_intent": False})

    def test_interaction_numbers_are_integers_and_zero_is_preserved(self):
        result, _ = self._expand_with_model_answers({
            "interaction": {"operator_interaction_min": "10", "operator_interaction_max": None,
                            "set_interaction_max": 0},
        })
        self.assertEqual(result["role_search_filters"], {
            "has_domain_intent": False, "operator_interaction_min": 10, "set_interaction_max": 0,
        })

    def test_excluded_executive_words_do_not_override_extracted_occupation(self):
        mod = load_module()
        queries = [
            "Software engineers with AI or LLM experience in SF or NYC; targeting mid/senior; exclude current founders, co-founders, CEOs and C-suite.",
            "Software engineers, not founders or CEOs",
            "Software engineers who report to the CEO",
        ]
        for query in queries:
            with self.subTest(query=query):
                filters = mod._merge(
                    {"role_ids": ["software_engineer"], "bm25_queries": ["software engineer"],
                     "semantic_query": "Software engineers building AI products and LLM systems.",
                     "seniority": ["mid", "senior"]},
                    {}, {}, {}, {}, {}, {}, query,
                )
                # Cloud V2 keeps non-shortcut role ids internal.
                self.assertNotIn("role_ids", filters)
                self.assertEqual(filters["bm25_queries"], ["software engineer"])
                self.assertEqual(filters["seniority_bands"], ["mid", "senior"])
                self.assertNotIn("csuite_shortcut_role_id", filters)

    def test_role_ids_are_emitted_only_for_cloud_shortcuts(self):
        # Cloud's V2 extractor emits role_ids only for founder and C-suite shortcuts;
        # the role agent's other ids still inject titles but never become a filter.
        mod = load_module()
        plain = mod._merge(
            {"role_ids": ["product_manager"], "bm25_queries": ["product manager"],
             "semantic_query": "Product managers building consumer apps."},
            {}, {}, {}, {}, {}, {}, "product managers in NYC",
        )
        self.assertNotIn("role_ids", plain)
        self.assertIn("product manager", plain["bm25_queries"])
        # Kept outside the filter for title clustering; role_function drives adjacency.
        self.assertEqual(plain["role_agent_ids"], ["product_manager"])
        self.assertEqual(plain["role_function"], "product")

        cto = mod._merge(
            {"role_ids": ["chief_technology_officer", "engineering_manager"], "bm25_queries": ["CTO", "engineering leader"]},
            {}, {}, {}, {}, {}, {}, "CTOs at fintech startups",
        )
        self.assertEqual(cto["role_ids"], ["chief_technology_officer"])
        self.assertEqual(cto["csuite_shortcut_role_id"], "chief_technology_officer")
        self.assertEqual(cto["seniority_bands"], ["c-suite"])
        self.assertNotIn("engineering leader", cto["bm25_queries"])

        founder = mod._merge(
            {"role_ids": ["founder", "software_engineer"], "bm25_queries": ["founder"]},
            {}, {}, {}, {}, {}, {}, "technical founders",
        )
        self.assertEqual(founder["role_ids"], ["founder"])

    def test_positive_cto_role_survives_other_executive_exclusions(self):
        mod = load_module()
        filters = mod._merge(
            {"role_ids": ["chief_technology_officer"], "bm25_queries": ["CTO"]},
            {}, {}, {}, {}, {}, {}, "CTOs excluding current founders and CEOs",
        )
        self.assertEqual(filters["role_ids"], ["chief_technology_officer"])
        self.assertEqual(filters["csuite_shortcut_role_id"], "chief_technology_officer")
        self.assertNotIn("CEO", filters["bm25_queries"])

    def test_generate_pond_traits_uses_the_production_trait_extractor(self):
        mod = load_module()
        client = object()
        expected = {"traits": [{"value": "Software engineer"}]}

        with mock.patch.object(mod, "_extract", new=mock.AsyncMock(return_value=expected)) as extract:
            result = asyncio.run(mod.generate_pond_traits(
                client,
                "software engineers",
                system_prompt="production trait prompt",
                model="gpt-5.6-luna",
                reasoning_effort="medium",
            ))

        self.assertEqual(result, expected)
        extract.assert_awaited_once_with(
            client,
            "trait_generation",
            "production trait prompt",
            "software engineers",
            model="gpt-5.6-luna",
            reasoning_effort="medium",
        )

    def test_failed_trait_extractor_does_not_enable_company_union(self):
        mod = load_module()

        async def fake_extract(client, name, system_prompt, query, model=None, reasoning_effort=None):
            if name == "role":
                return {"semantic_query": "Software engineers.",
                        "bm25_queries": ["software engineer"]}
            if name == "company":
                return {"company_names": ["Stripe"]}
            return {}

        with mock.patch.object(mod, "make_async_openai_client", return_value=object()), \
             mock.patch.object(mod, "_extract", side_effect=fake_extract):
            result = asyncio.run(mod.expand_query_parallel(
                "software engineers at Stripe", api_key="test"))

        self.assertIs(result["has_domain_intent"], False)
        self.assertIs(result["role_search_filters"]["has_domain_intent"], False)

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

    def test_sf_in_phrase_keeps_person_city_and_adds_metro(self):
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
        self.assertEqual(filters["cities"], ["San Francisco"])
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

    def test_extracted_years_of_experience_is_not_a_filter(self):
        # Cloud disables the YOE filter (multi_index_strategy_v2), so local
        # expand must not emit it either.
        mod = load_module()
        filters = mod._merge(
            {"semantic_query": "software engineering work", "bm25_queries": ["software engineer"]},
            {},
            {},
            {},
            {"years_experience_min": 3, "years_experience_max": 5, "position_start_year": 2018},
            {},
            {},
            "software engineers with 3-5 years of experience since 2018",
        )

        self.assertNotIn("years_experience_min", filters)
        self.assertNotIn("years_experience_max", filters)
        self.assertEqual(filters["position_after_date"], "2018")

    def test_founder_role_expansion_matches_prod_shortcut_shape(self):
        mod = load_module()
        filters = mod._merge(
            {"semantic_query": "founders", "bm25_queries": ["founder"], "role_ids": ["founder"]},
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

    def test_founder_csuite_query_emits_founder_and_named_csuite_ids(self):
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

        # Cloud entity_mapper_v2: ["founder"] + the C-suite ids named.
        self.assertEqual(filters["role_ids"], ["founder", "chief_executive_officer"])
        self.assertIn("CEO", filters["bm25_queries"])
        self.assertIn("Chief Executive Officer", filters["bm25_queries"])
        self.assertEqual(filters["role_function"], "founder")

    def test_csuite_role_expansion_adds_canonical_ids_and_aliases(self):
        mod = load_module()
        filters = mod._merge(
            {"semantic_query": "technology executives", "bm25_queries": ["technology executive"],
             "role_ids": ["chief_technology_officer"]},
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
                {"semantic_query": "security executives", "bm25_queries": ["security executive"],
                 "role_ids": ["chief_information_security_officer"]},
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

        self.assertNotIn("role_ids", filters)
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

    def test_bare_role_defaults_to_general_ic_seniority(self):
        prompt = load_module().load_prompt_bundle()["seniority"]
        self.assertIn('general IC bands: ["junior", "mid", "senior", "staff"]', prompt)

    def test_seniority_prompt_maps_lead_and_head_explicitly(self):
        prompt = load_module().load_prompt_bundle()["seniority"]
        self.assertIn('"Lead" maps to ["senior"]', prompt)
        self.assertIn('"Head of" maps to ["director", "vice-president"]', prompt)

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

    def test_investor_merge_preserves_extracted_seniority_and_funding_contracts(self):
        mod = load_module()
        role = {
            "semantic_query": "Investors leading Series B rounds with $20M checks",
            "bm25_queries": ["investor", "general partner", "venture partner"],
            "role_ids": ["general_partner"],
            "departments": ["investing"],
            "seniority": ["partner"],
        }
        cases = [
            ("general partners at any seniority who can lead our Series B with a $20M check", {}),
            ("general partners at investment firms whose employers raised at least $20M",
             {"funding_amount_min": 20000000}),
        ]
        for query, funding in cases:
            with self.subTest(query=query), mock.patch("socket.socket", side_effect=AssertionError("offline test")):
                filters = mod._merge(
                    role, {"entity_types": ["vc_firm"], **funding}, {}, {}, {},
                    {"seniority_bands": []}, {}, query,
                )
                self.assertNotIn("seniority_bands", filters)
                self.assertNotIn("role_ids", filters)
                self.assertEqual(filters["entity_types"], ["vc_firm"])
                self.assertEqual(
                    {key: value for key, value in filters.items() if key.startswith("funding_")}, funding,
                )

    def test_hillclimbed_prompts_preserve_explicit_filter_boundaries(self):
        mod = load_module()
        prompts = mod.load_prompt_bundle()

        self.assertIn("INDUSTRY VS PERSON SKILL", prompts["company"])
        self.assertIn("EDUCATION RELATIONSHIP REQUIRED", prompts["education"])
        self.assertIn("return an empty list", prompts["seniority"])
        self.assertIn("explicitly states a numeric duration", prompts["temporal"])
        self.assertIn("Never emit a location trait", prompts["trait_generation"])
        self.assertIn("3-8 observable titles or true title aliases", prompts["role"])

    def test_chief_of_staff_keeps_chief_while_other_levels_are_stripped(self):
        mod = load_module()
        filters = mod._merge(
            {"bm25_queries": ["Chief of Staff", "Senior Software Engineer"]},
            {}, {}, {}, {}, {}, {}, "chief of staff and senior software engineers",
        )
        self.assertEqual(filters["role_core_patterns"], [{
            "regex": r"\b(chief\ of\ staff|software\ engineer)\b",
            "examples": ["Chief of Staff", "Software Engineer"],
        }])

    def test_expander_ignores_role_and_temporal_current_flags(self):
        mod = load_module()
        filters = mod._merge(
            {"semantic_query": "technology executives", "bm25_queries": ["technology executive"],
             "is_current_role": False, "is_current_company": False},
            {},
            {},
            {},
            {"is_current": True},
            {},
            {},
            "currently CTOs",
        )

        self.assertNotIn("is_current_role", filters)
        self.assertNotIn("is_current_company", filters)

    def test_expander_ignores_temporal_past_flag(self):
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

        self.assertNotIn("is_current_role", filters)
        self.assertNotIn("is_current_company", filters)

    def test_city_filters_keep_spelling_aliases_with_metro(self):
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
        self.assertEqual(filters["cities"], ["New York City", "New York City, New York", "New York"])

        unmapped = mod._expand_city_filter_aliases(["Austin", "  ", "Austin"])
        self.assertEqual(unmapped, ["Austin"])

    def test_city_filters_add_each_fixed_table_metro(self):
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
        self.assertEqual(filters["metro_areas"], ["San Francisco Bay Area", "Raleigh-Durham Metropolitan Area"])

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
