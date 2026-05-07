import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path


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
        self.assertIn(("city", "In", ["San Francisco"]), filters[1])
        self.assertIn(("state", "In", ["California"]), filters[1])
        self.assertFalse(any(clause[0] == "Or" and ("state", "In", ["California"]) in clause[1] for clause in filters[1]))
        self.assertIn(("role_track", "In", ["engineering"]), filters[1])
        self.assertIn(("is_current", "Eq", True), filters[1])
        self.assertIn(("total_years_experience", "Gte", 3), filters[1])

    def test_position_window_converts_to_overlap_filters(self) -> None:
        filters = turbopuffer_client.filters_from_role_payload(
            {
                "semantic_query": "Builds software systems in production with hands-on coding responsibilities.",
                "company_ids": ["urn:harmonic:company:box"],
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
        self.assertNotIn(("linkedin_followers", "Gte", 1000), filters[1])

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
            "is_current_company": True,
        })
        self.assertEqual(company_filters[0], "And")
        self.assertIn(("company_id", "In", ["urn:harmonic:company:meta"]), company_filters[1])
        self.assertIn(("is_current", "Eq", True), company_filters[1])

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
            return [{"id": "base-uuid-0", "base_id": "base-uuid", "position_title": "Engineer"}]

        turbopuffer_client.filter_only_rows = fake_filter_only_rows
        try:
            rows = asyncio.run(turbopuffer_client.hybrid_role_rows(
                {"company_ids": ["urn:harmonic:company:meta"]},
                ("company_id", "In", ["urn:harmonic:company:meta"]),
                top_k=10,
                include_attributes=["base_id", "position_title"],
            ))
        finally:
            turbopuffer_client.filter_only_rows = original

        self.assertEqual(rows[0]["retrieval_mode"], "filter_only")
        self.assertEqual(rows[0]["person_id"], "base-uuid")
        self.assertEqual(rows[0]["position_id"], "base-uuid-0")

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
