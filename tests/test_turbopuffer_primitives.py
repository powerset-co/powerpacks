import asyncio
import gzip
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "packs/search/primitives/lib"
sys.path.insert(0, str(LIB))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


turbopuffer_client = load_module("turbopuffer_client", LIB / "turbopuffer_client.py")
resolve_companies = load_module("resolve_companies", ROOT / "packs/search/primitives/resolve_companies" / "resolve_companies.py")
apply_prefilters = load_module("apply_prefilters", ROOT / "packs/search/primitives/apply_prefilters" / "apply_prefilters.py")
hydrate_people = load_module("hydrate_people", ROOT / "packs/search/primitives/hydrate_people" / "hydrate_people.py")
results_io = load_module("results_io", ROOT / "packs/search/primitives/persist_search_results" / "results_io.py")


class TurbopufferPrimitiveTests(unittest.TestCase):
    def setUp(self) -> None:
        # Full test discovery imports message primitives that load repo .env.
        # Unit tests for low-level search filters should exercise explicit
        # payload behavior only, not project-local default operator filtering.
        os.environ.pop("POWERPACKS_DEFAULT_SET_ID", None)
        os.environ.pop("POWERPACKS_DEFAULT_OPERATOR_ID", None)
        os.environ.pop("POWERPACKS_DEFAULT_OPERATOR_IDS", None)

    def test_filters_from_role_payload_uses_contract_fields(self) -> None:
        filters = turbopuffer_client.filters_from_role_payload(
            {
                "semantic_query": "Builds software systems in production with hands-on coding responsibilities.",
                "cities": ["San Francisco"],
                "states": ["California"],
                "role_tracks": ["engineering"],
                "is_current_role": True,
                "years_experience_min": 3,
            }
        )

        self.assertEqual(filters[0], "And")
        location = next(clause for clause in filters[1] if clause[0] == "Or")
        self.assertIn(("city", "In", ["San Francisco"]), location[1])
        self.assertIn(("state", "In", ["California"]), location[1])
        self.assertIn(("role_track", "In", ["engineering"]), filters[1])
        self.assertIn(("is_current", "Eq", True), filters[1])
        self.assertIn(("total_years_experience", "Gte", 3), filters[1])

    def test_position_window_converts_to_overlap_filters(self) -> None:
        filters = turbopuffer_client.filters_from_role_payload(
            {
                "semantic_query": "Builds software systems in production with hands-on coding responsibilities.",
                "company_ids": ["urn:harmonic:company:box"],
                "role_ids": ["founder"],
                "position_after_date": "2019",
                "position_before_date": "2022",
            }
        )

        self.assertEqual(filters[0], "And")
        self.assertIn(("company_id", "In", ["urn:harmonic:company:box"]), filters[1])
        self.assertTrue(any(clause[0] == "start_date_epoch" and clause[1] == "Lte" for clause in filters[1]))
        self.assertTrue(any(clause[0] == "Or" for clause in filters[1]))

    def test_prefilter_base_ids_become_people_filter(self) -> None:
        filters = turbopuffer_client.filters_from_role_payload(
            {
                "semantic_query": "Builds software systems in production with hands-on coding responsibilities.",
                "role_tracks": ["engineering"],
                "base_candidate_ids": ["p1", "p2"],
                "li_followers_min": 1000,
            }
        )

        self.assertEqual(filters[0], "And")
        self.assertIn(("base_id", "In", ["p1", "p2"]), filters[1])
        self.assertIn(("linkedin_followers", "Gte", 1000), filters[1])

    def test_currentness_uses_split_fields_only(self) -> None:
        legacy_filters = turbopuffer_client.filters_from_role_payload({
            "semantic_query": "Builds software systems in production with hands-on coding responsibilities across backend, frontend, platform, infrastructure, or application engineering teams.",
            "is_current": True,
        })
        self.assertIsNone(legacy_filters)

        role_filters = turbopuffer_client.filters_from_role_payload({
            "semantic_query": "Builds software systems in production with hands-on coding responsibilities across backend, frontend, platform, infrastructure, or application engineering teams.",
            "is_current_role": True,
            "is_current_company": False,
        })
        self.assertEqual(role_filters, ("is_current", "Eq", True))

        company_filters = turbopuffer_client.filters_from_role_payload({
            "company_ids": ["urn:harmonic:company:meta"],
            "role_ids": ["founder"],
            "is_current_company": True,
        })
        self.assertEqual(company_filters[0], "And")
        self.assertIn(("company_id", "In", ["urn:harmonic:company:meta"]), company_filters[1])
        self.assertIn(("is_current", "Eq", True), company_filters[1])

    def test_role_payload_from_state_derives_currentness_from_traits(self) -> None:
        state = {
            "steps": [{
                "id": "expand_search_request",
                "output": {
                    "traits": [
                        {"meaning": "role", "temporal": "current", "value": "Software engineer"},
                        {"meaning": "company", "temporal": "past", "value": "Worked at Google"},
                    ],
                    "role_search_filters": {
                        "semantic_query": "Builds software systems in production with hands-on coding responsibilities.",
                        "is_current_role": False,
                        "is_current_company": True,
                    },
                },
            }],
        }

        payload = turbopuffer_client.role_payload_from_state(state)

        self.assertIs(payload["is_current_role"], True)
        self.assertIs(payload["is_current_company"], False)

    def test_founder_shortcut_adds_role_id_and_preserves_intersection(self) -> None:
        state = {
            "query": "founders at fintech startups",
            "steps": [{
                "id": "expand_search_request",
                "output": {"role_search_filters": {"company_ids": ["c1"], "bm25_queries": ["founders"]}},
            }],
        }

        payload = turbopuffer_client.role_payload_from_state(state)
        filters = turbopuffer_client.filters_from_role_payload(payload)

        self.assertEqual(payload["role_ids"], ["founder"])
        self.assertNotIn("seniority_bands", payload)
        self.assertEqual(payload["search_mode"], "COMPANY_INTERSECTION")
        self.assertIn(("role_ids", "ContainsAny", ["founder"]), filters[1])
        self.assertIn(("company_id", "In", ["c1"]), filters[1])

    def test_founders_fund_investor_query_does_not_trigger_founder_shortcut(self) -> None:
        payload = turbopuffer_client.apply_role_shortcuts({"investor_names": ["Founders Fund"]}, "people backed by Founders Fund")

        self.assertNotIn("role_ids", payload)

    def test_csuite_shortcut_adds_canonical_role_id(self) -> None:
        payload = turbopuffer_client.apply_role_shortcuts({"company_ids": ["c1"]}, "CTOs at AI companies")
        filters = turbopuffer_client.filters_from_role_payload(payload)

        self.assertIn("chief_technology_officer", payload["role_ids"])
        self.assertEqual(payload["seniority_bands"], ["c_suite"])
        self.assertIn(("role_ids", "ContainsAny", ["chief_technology_officer"]), filters[1])

    def test_search_mode_matches_company_domain_parity(self) -> None:
        self.assertEqual(turbopuffer_client.search_mode_for_payload({"role_tracks": ["engineering"]}), "SEARCH_ONLY")
        self.assertEqual(
            turbopuffer_client.search_mode_for_payload({
                "role_ids": ["founder"],
                "company_ids": ["urn:harmonic:company:fintech"],
                "has_domain_intent": True,
            }),
            "COMPANY_INTERSECTION",
        )
        self.assertEqual(
            turbopuffer_client.search_mode_for_payload({
                "role_tracks": ["engineering"],
                "company_semantic_queries": ["fintech companies"],
                "sector_types": ["financial_services"],
            }),
            "COMPANY_UNION",
        )
        self.assertEqual(
            turbopuffer_client.search_mode_for_payload({"company_ids": ["urn:harmonic:company:meta"]}),
            "COMPANY_UNION",
        )
        self.assertEqual(
            turbopuffer_client.search_mode_for_payload({
                "role_tracks": ["engineering"],
                "company_ids": ["urn:harmonic:company:meta"],
            }),
            "COMPANY_INTERSECTION",
        )

    def test_company_union_does_not_filter_role_search_by_company(self) -> None:
        payload = {
            "role_tracks": ["engineering"],
            "company_ids": ["urn:harmonic:company:fintech"],
            "company_semantic_queries": ["fintech companies"],
            "sector_types": ["financial_services"],
        }

        filters = turbopuffer_client.filters_from_role_payload(payload)

        self.assertEqual(filters, ("role_track", "In", ["engineering"]))

    def test_summarize_filter_truncates_large_id_lists(self) -> None:
        filters = ("base_id", "In", [f"p{i}" for i in range(25)])
        summary = turbopuffer_client.summarize_filter(filters, max_list_values=3)

        self.assertEqual(summary[0], "base_id")
        self.assertEqual(summary[2]["count"], 25)
        self.assertEqual(summary[2]["sample"], ["p0", "p1", "p2"])
        self.assertTrue(summary[2]["truncated"])

    def test_role_payload_from_state_uses_resolved_ids(self) -> None:
        state = {
            "steps": [
                {
                    "id": "expand_search_request",
                    "output": {
                        "role_search_filters": {
                            "semantic_query": "Builds software systems in production with hands-on coding responsibilities.",
                            "education_names": ["Stanford"],
                        }
                    },
                },
                {"id": "resolve_education", "output": {"education_ids": ["urn:harmonic:school:stanford"]}},
                {"id": "resolve_companies", "output": {"company_ids": ["urn:harmonic:company:meta"]}},
                {"id": "resolve_investors", "output": {"investor_urns": ["urn:harmonic:person:8352"]}},
                {"id": "apply_prefilters", "output": {"base_candidate_ids": ["p1", "p2"]}},
            ]
        }

        payload = turbopuffer_client.role_payload_from_state(state)
        self.assertEqual(payload["education_ids"], ["urn:harmonic:school:stanford"])
        self.assertEqual(payload["company_ids"], ["urn:harmonic:company:meta"])
        self.assertEqual(payload["investors"], ["urn:harmonic:person:8352"])
        self.assertEqual(payload["base_candidate_ids"], ["p1", "p2"])

    def test_company_sector_filters_are_configurable(self) -> None:
        payload = {
            "company_semantic_queries": ["database infrastructure companies"],
            "sector_types": ["data"],
            "entity_types": ["venture_backed_startup"],
        }

        hard = resolve_companies.company_attribute_filters(payload, include_soft=False)
        soft = resolve_companies.company_attribute_filters(payload, only_soft=True)
        combined = resolve_companies.combine_filters(hard, soft)

        self.assertEqual(hard[0], "entity_types")
        self.assertEqual(soft, ("sector_types", "ContainsAny", ["data"]))
        self.assertEqual(combined[0], "And")
        self.assertIn(("entity_types", "ContainsAny", ["venture_backed_startup"]), combined[1])
        self.assertIn(("sector_types", "ContainsAny", ["data"]), combined[1])
        self.assertEqual(resolve_companies.sector_strategy({"company_sector_strategy": "staged"}, "soft_union"), "staged")

    def test_company_location_and_numeric_filters_match_remote_shape(self) -> None:
        filters = resolve_companies.company_attribute_filters({
            "company_cities": ["San Francisco"],
            "company_metro_areas": ["New York City Metropolitan Area"],
            "headcount_max": 50,
            "funding_amount_max": 10_000_000,
            "valuation_max": 50_000_000,
            "founded_year_max": 2022,
            "funding_stage_max": "series_a",
        })

        self.assertEqual(filters[0], "And")
        location = next(clause for clause in filters[1] if clause[0] == "Or")
        self.assertIn(("city", "In", ["San Francisco"]), location[1])
        self.assertIn(("metro_area", "In", ["New York City Metropolitan Area"]), location[1])
        self.assertIn(("headcount", "Gt", 0), filters[1])
        self.assertIn(("headcount", "Lte", 50), filters[1])
        self.assertIn(("funding_total", "Gt", 0), filters[1])
        self.assertIn(("funding_total", "Lte", 10_000_000), filters[1])
        self.assertIn(("valuation", "Gt", 0), filters[1])
        self.assertIn(("valuation", "Lte", 50_000_000), filters[1])
        self.assertIn(("founded_year", "Gt", 0), filters[1])
        self.assertIn(("founded_year", "Lte", 2022), filters[1])
        self.assertIn(("funding_stage", "Gt", 0), filters[1])
        self.assertIn(("funding_stage", "Lte", 3), filters[1])

    def test_frontier_ids_read_execute_role_search(self) -> None:
        state = {
            "steps": [
                {
                    "id": "execute_role_search",
                    "output": {"candidate_ids": ["p1", "p2", "p1"]},
                }
            ]
        }

        self.assertEqual(hydrate_people.frontier_ids(state), ["p1", "p2"])
        self.assertEqual(results_io.frontier_ids(state), ["p1", "p2"])

    def test_dedupe_people_limit_zero_keeps_full_frontier(self) -> None:
        rows = [
            {"id": "p1-0", "base_id": "p1", "score": 1.0},
            {"id": "p2-0", "base_id": "p2", "score": 0.9},
            {"id": "p3-0", "base_id": "p3", "score": 0.8},
        ]
        self.assertEqual(len(turbopuffer_client.dedupe_people(rows, limit=0)), 3)
        self.assertEqual(len(turbopuffer_client.dedupe_people(rows, limit=2)), 2)

    def test_compressed_hydration_jsonl_round_trips_for_results(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "profiles.jsonl.gz"
            rows = [{"person_id": "p1", "name": "A"}, {"person_id": "p2", "name": "B"}]
            hydrate_people.write_jsonl(path, rows)
            with gzip.open(path, "rt") as handle:
                self.assertEqual(json.loads(handle.readline())["person_id"], "p1")
            self.assertEqual(results_io.read_jsonl(path), rows)

    def test_result_rows_use_execute_role_search_order(self) -> None:
        state = {
            "task_id": "task",
            "query": "software engineers in sf",
            "steps": [
                {"id": "execute_role_search", "output": {"candidate_ids": ["p2", "p1"]}},
                {
                    "id": "hydrate_people",
                    "output": {
                        "profiles": [
                            {"person_id": "p1", "name": "One", "positions": []},
                            {"person_id": "p2", "name": "Two", "positions": []},
                        ]
                    },
                },
            ],
        }

        rows = results_io.result_rows(state)
        self.assertEqual([row["person_id"] for row in rows], ["p2", "p1"])
        self.assertEqual([row["name"] for row in rows], ["Two", "One"])

    def test_social_and_interaction_prefilters_are_postgres_backed(self) -> None:
        original_social = apply_prefilters.fetch_social_filter_person_ids
        original_interaction = apply_prefilters.fetch_interaction_filter_person_ids
        try:
            apply_prefilters.fetch_social_filter_person_ids = lambda payload, env_file=None: ["p1", "p2"]
            apply_prefilters.fetch_interaction_filter_person_ids = lambda payload, env_file=None: ["p2", "p3"]
            social = apply_prefilters.social_base_ids({"li_followers_min": 1000}, env_file=None, max_ids=10)
            interaction = apply_prefilters.interaction_base_ids({"set_interaction_min": 5}, env_file=None, max_ids=10)
        finally:
            apply_prefilters.fetch_social_filter_person_ids = original_social
            apply_prefilters.fetch_interaction_filter_person_ids = original_interaction

        self.assertEqual(social[0], ["p1", "p2"])
        self.assertEqual(social[1]["stage"], "social")
        self.assertEqual(interaction[0], ["p2", "p3"])
        self.assertEqual(interaction[1]["stage"], "interaction")

    def test_large_base_candidate_ids_are_batched_for_hybrid_search(self) -> None:
        original_batch_size = turbopuffer_client.BASE_ID_BATCH_SIZE
        original_batch_min = turbopuffer_client.BASE_ID_BATCH_MIN
        original_embedding = turbopuffer_client.embedding
        original_single = turbopuffer_client._hybrid_role_rows_single
        seen_filters = []

        async def fake_embedding(text):
            return [0.1]

        async def fake_single(payload, filters, *, top_k, include_attributes, query_embedding=None):
            seen_filters.append(filters)
            batch = []
            for clause in filters[1]:
                if clause[0] == "base_id":
                    batch = clause[2]
            return [{"id": f"{batch[0]}-0", "base_id": batch[0], "score": 1.0}]

        turbopuffer_client.BASE_ID_BATCH_SIZE = 2
        turbopuffer_client.BASE_ID_BATCH_MIN = 3
        turbopuffer_client.embedding = fake_embedding
        turbopuffer_client._hybrid_role_rows_single = fake_single
        try:
            rows = asyncio.run(turbopuffer_client.hybrid_role_rows(
                {
                    "semantic_query": "Builds software systems in production with hands-on coding responsibilities across backend, frontend, platform, infrastructure, or application engineering teams.",
                    "base_candidate_ids": ["p1", "p2", "p3", "p4", "p5"],
                    "role_tracks": ["engineering"],
                },
                ("And", [("base_id", "In", ["p1", "p2", "p3", "p4", "p5"]), ("role_track", "In", ["engineering"])]),
                top_k=10,
                include_attributes=["base_id"],
            ))
        finally:
            turbopuffer_client.BASE_ID_BATCH_SIZE = original_batch_size
            turbopuffer_client.BASE_ID_BATCH_MIN = original_batch_min
            turbopuffer_client.embedding = original_embedding
            turbopuffer_client._hybrid_role_rows_single = original_single

        self.assertEqual(len(seen_filters), 3)
        self.assertTrue(all(len([c for c in f[1] if c[0] == "base_id"][0][2]) <= 2 for f in seen_filters))
        self.assertEqual(rows[0]["base_id_batch_count"], 3)
        self.assertTrue(all(row["retrieval_batched_base_ids"] for row in rows))

    def test_filter_only_payload_uses_filter_only_rows(self) -> None:
        original = turbopuffer_client.filter_only_rows

        async def fake_filter_only_rows(filters, include_attributes, *, page_size=10000, max_results=0):
            self.assertEqual(filters, ("company_id", "In", ["urn:harmonic:company:meta"]))
            self.assertEqual(max_results, 10)
            return [{"id": "base-uuid-0", "base_id": "base-uuid", "position_title": "Engineer"}]

        turbopuffer_client.filter_only_rows = fake_filter_only_rows
        try:
            rows = asyncio.run(turbopuffer_client.hybrid_role_rows(
                {"company_ids": ["urn:harmonic:company:meta"], "search_mode": "COMPANY_INTERSECTION"},
                ("company_id", "In", ["urn:harmonic:company:meta"]),
                top_k=10,
                include_attributes=["base_id", "position_title"],
            ))
        finally:
            turbopuffer_client.filter_only_rows = original

        self.assertEqual(rows[0]["retrieval_mode"], "filter_only")
        self.assertEqual(rows[0]["person_id"], "base-uuid")
        self.assertEqual(rows[0]["position_id"], "base-uuid-0")

    def test_company_union_candidates_append_after_role_candidates(self) -> None:
        candidates = [
            {"person_id": "p1", "score": 1.0, "vertical_sources": ["hybrid"]},
        ]
        union = [
            {"person_id": "p1", "position_id": "p1-0"},
            {"person_id": "p2", "position_id": "p2-0", "position_title": "Engineer", "company_id": "c1"},
        ]

        merged = turbopuffer_client.merge_company_union_candidates(candidates, union, limit=0)

        self.assertEqual([row["person_id"] for row in merged], ["p1", "p2"])
        self.assertEqual(merged[0]["vertical_sources"], ["hybrid", "company_filter"])
        self.assertEqual(merged[0]["matched_position_ids"], ["p1-0"])
        self.assertEqual(merged[1]["position_id"], "p2-0")
        self.assertEqual(merged[1]["vertical_sources"], ["company_filter"])

    def test_company_union_prefilter_records_union_without_role_base_ids(self) -> None:
        original = apply_prefilters.bm25_adjacency_rows

        async def fake_bm25_adjacency_rows(queries, filters, *, top_k, include_attributes):
            self.assertIn("CTO", queries)
            self.assertIn(("company_id", "In", ["c1"]), filters[1])
            self.assertIn(("seniority_band", "NotIn", ["entry", "trainee"]), filters[1])
            return [{"id": "p1-0", "base_id": "p1", "company_id": "c1", "position_title": "Engineering Manager", "score": 0.5}]

        apply_prefilters.bm25_adjacency_rows = fake_bm25_adjacency_rows
        try:
            out = asyncio.run(apply_prefilters.run(SimpleNamespace(
                state=None,
                payload_json=json.dumps({
                    "company_ids": ["c1"],
                    "company_semantic_queries": ["fintech companies"],
                    "sector_types": ["financial_services"],
                    "role_tracks": ["engineering"],
                }),
                env_file=None,
                page_size=10000,
                max_ids=100,
                company_prefilter_threshold=3000,
                company_id_batch_size=500,
                company_id_batch_concurrency=1,
            )))
        finally:
            apply_prefilters.bm25_adjacency_rows = original

        self.assertEqual(out["search_mode"], "COMPANY_UNION")
        self.assertFalse(out["role_prefilter_ran"])
        self.assertEqual(out["base_candidate_ids"], [])
        self.assertEqual(out["company_union_candidate_ids"], ["p1"])
        self.assertEqual(out["company_union_candidates"][0]["position_id"], "p1-0")
        self.assertEqual(out["company_union_candidates"][0]["vertical_sources"], ["company_filter"])
        self.assertEqual(out["stages"][-1]["adjacency_method"], "bm25")

    def test_company_union_role_id_adjacency_uses_adjacent_filters(self) -> None:
        original = apply_prefilters.filter_only_rows_for_namespace

        async def fake_filter_only_rows_for_namespace(logical_name, filters, include_attributes, *, page_size=10000, max_results=0):
            self.assertEqual(logical_name, "people")
            self.assertIn(("company_id", "In", ["c1"]), filters[1])
            self.assertIn(("role_ids", "ContainsAny", ["engineering_manager"]), filters[1])
            self.assertIn(("role_track", "In", ["engineering"]), filters[1])
            self.assertIn(("seniority_band", "NotIn", ["entry", "trainee"]), filters[1])
            return [{"id": "p2-1", "base_id": "p2", "company_id": "c1", "position_title": "Engineering Manager"}]

        apply_prefilters.filter_only_rows_for_namespace = fake_filter_only_rows_for_namespace
        try:
            out = asyncio.run(apply_prefilters.run(SimpleNamespace(
                state=None,
                payload_json=json.dumps({
                    "company_ids": ["c1"],
                    "company_semantic_queries": ["fintech companies"],
                    "sector_types": ["financial_services"],
                    "role_tracks": ["engineering"],
                    "adjacent_role_ids": ["engineering_manager"],
                    "adjacent_departments": ["engineering"],
                }),
                env_file=None,
                page_size=10000,
                max_ids=100,
                company_prefilter_threshold=3000,
                company_id_batch_size=500,
                company_id_batch_concurrency=1,
            )))
        finally:
            apply_prefilters.filter_only_rows_for_namespace = original

        self.assertEqual(out["company_union_candidate_ids"], ["p2"])
        self.assertEqual(out["stages"][-1]["adjacency_method"], "role_id")

    def test_company_union_derives_static_adjacent_role_ids_when_role_ids_present(self) -> None:
        original_rows = apply_prefilters.filter_only_rows_for_namespace
        original_effective = apply_prefilters.effective_adjacent_role_ids
        apply_prefilters.effective_adjacent_role_ids = lambda payload: ["engineering_manager", "technical_lead"]

        async def fake_filter_only_rows_for_namespace(logical_name, filters, include_attributes, *, page_size=10000, max_results=0):
            self.assertIn(("role_ids", "ContainsAny", ["engineering_manager", "technical_lead"]), filters[1])
            return [{"id": "p3-0", "base_id": "p3", "company_id": "c1", "position_title": "Technical Lead"}]

        apply_prefilters.filter_only_rows_for_namespace = fake_filter_only_rows_for_namespace
        try:
            out = asyncio.run(apply_prefilters.run(SimpleNamespace(
                state=None,
                payload_json=json.dumps({
                    "company_ids": ["c1"],
                    "company_semantic_queries": ["developer tools companies"],
                    "sector_types": ["developer_tools"],
                    "role_ids": ["software_engineer"],
                }),
                env_file=None,
                page_size=10000,
                max_ids=100,
                company_prefilter_threshold=3000,
                company_id_batch_size=500,
                company_id_batch_concurrency=1,
            )))
        finally:
            apply_prefilters.filter_only_rows_for_namespace = original_rows
            apply_prefilters.effective_adjacent_role_ids = original_effective

        self.assertEqual(out["company_union_candidate_ids"], ["p3"])
        self.assertEqual(out["stages"][-1]["adjacency_method"], "role_id")

    def test_company_adjacency_filters_non_operational_titles(self) -> None:
        self.assertTrue(turbopuffer_client.is_non_operational_title("Board Member"))
        self.assertTrue(turbopuffer_client.is_non_operational_title("Investor and Advisor"))
        self.assertFalse(turbopuffer_client.is_non_operational_title("Board Member and CTO"))
        self.assertFalse(turbopuffer_client.is_non_operational_title("Engineering Manager"))

    def test_broad_company_semantic_union_batches_company_ids(self) -> None:
        original = apply_prefilters.bm25_adjacency_rows
        seen_batch_sizes = []

        async def fake_bm25_adjacency_rows(queries, filters, *, top_k, include_attributes):
            for clause in filters[1]:
                if clause[0] == "company_id":
                    seen_batch_sizes.append(len(clause[2]))
                    return [{"id": f"p{len(seen_batch_sizes)}-0", "base_id": f"p{len(seen_batch_sizes)}", "position_title": "CTO"}]
            self.fail("company_id filter missing")

        apply_prefilters.bm25_adjacency_rows = fake_bm25_adjacency_rows
        try:
            out = asyncio.run(apply_prefilters.run(SimpleNamespace(
                state=None,
                payload_json=json.dumps({
                    "company_ids": [f"c{i}" for i in range(1201)],
                    "company_semantic_queries": ["fintech infrastructure companies"],
                    "sector_types": ["financial_services"],
                    "role_tracks": ["engineering"],
                }),
                env_file=None,
                page_size=10000,
                max_ids=10000,
                company_prefilter_threshold=3000,
                company_id_batch_size=500,
                company_id_batch_concurrency=1,
            )))
        finally:
            apply_prefilters.bm25_adjacency_rows = original

        self.assertEqual(seen_batch_sizes, [500, 500, 201])
        self.assertEqual(out["stages"][-1]["company_id_batches"], 3)
        self.assertEqual(out["company_union_candidate_ids"], ["p1", "p2", "p3"])

    def test_hydration_applies_retrieval_vertical_sources_and_matches(self) -> None:
        state = {
            "steps": [{
                "id": "execute_role_search",
                "output": {"candidates": [{
                    "person_id": "p1",
                    "position_id": "pos-1",
                    "score": 0.42,
                    "vertical_sources": ["hybrid", "company_filter"],
                }]},
            }]
        }
        meta = hydrate_people.candidate_metadata(state)
        profile = {
            "person_id": "p1",
            "positions": [{"id": "pos-0", "title": "Advisor"}, {"id": "pos-1", "title": "CTO"}],
            "vertical_sources": [],
            "matched_position_indexes": [],
        }
        enriched = hydrate_people.apply_candidate_metadata(profile, meta["p1"])
        self.assertEqual(enriched["base_score"], 0.42)
        self.assertEqual(enriched["matched_position_indexes"], [1])
        self.assertEqual(enriched["vertical_sources"], ["hybrid", "company_filter"])

    def test_scripts_do_not_import_aleph_mvp(self) -> None:
        for path in [
            LIB / "turbopuffer_client.py",
            ROOT / "packs/search/primitives/count_candidates" / "count_candidates.py",
            ROOT / "packs/search/primitives/execute_role_search" / "execute_role_search.py",
            ROOT / "packs/search/primitives/execute_search_slice" / "execute_search_slice.py",
            ROOT / "packs/search/primitives/resolve_education" / "resolve_education.py",
            ROOT / "packs/search/primitives/resolve_investors" / "resolve_investors.py",
            ROOT / "packs/search/primitives/resolve_companies" / "resolve_companies.py",
            ROOT / "packs/search/primitives/apply_prefilters" / "apply_prefilters.py",
        ]:
            text = path.read_text()
            self.assertNotIn("aleph-mvp", text)
            self.assertNotIn("api_v2", text)
            self.assertNotIn("shared.env_config", text)


if __name__ == "__main__":
    unittest.main()
