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
from unittest import mock

import jsonschema

from packs.search.pipeline.frontier import CandidateFrontier, CandidateRecord
from packs.search.pipeline.models import PowersetCorpus


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / "packs/search/primitives"
LIB = PRIMITIVES / "lib"
SHARED = PRIMITIVES / "shared"
LOCAL = PRIMITIVES / "local"
TURBOPUFFER = PRIMITIVES / "turbopuffer"
for _path in [LIB, SHARED, LOCAL, TURBOPUFFER]:
    sys.path.insert(0, str(_path))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


search_result_merge = load_module("search_result_merge", SHARED / "search_result_merge.py")
turbopuffer_client = load_module("turbopuffer_search_backend", TURBOPUFFER / "turbopuffer_search_backend.py")
resolve_companies = load_module("turbopuffer_resolve_companies", TURBOPUFFER / "turbopuffer_resolve_companies.py")
search_common = load_module("search_common_explicit_scope", SHARED / "search_common.py")


class TurbopufferPrimitiveTests(unittest.TestCase):
    def setUp(self) -> None:
        # Full test discovery imports message primitives that load repo .env.
        # Unit tests for low-level search filters should exercise explicit
        # payload behavior only, not project-local default operator filtering.
        os.environ.pop("POWERPACKS_DEFAULT_SET_ID", None)
        os.environ.pop("POWERPACKS_DEFAULT_OPERATOR_ID", None)
        os.environ.pop("POWERPACKS_DEFAULT_OPERATOR_IDS", None)

    def test_unfiltered_enumeration_paginates_without_an_initial_filter(self) -> None:
        first = [SimpleNamespace(id="a", model_extra={"value": 1})]
        namespace = SimpleNamespace(
            query=mock.Mock(
                side_effect=[SimpleNamespace(rows=first), SimpleNamespace(rows=[])]
            )
        )
        with mock.patch.object(turbopuffer_client, "namespace", return_value=namespace):
            result = asyncio.run(
                turbopuffer_client.enumerate_filter_only_rows_for_namespace(
                    "schools", None, ["value"], page_size=1
                )
            )
        self.assertTrue(result["completed"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["rows"], [{"id": "a", "value": 1}])
        self.assertNotIn("filters", namespace.query.call_args_list[0].kwargs)
        self.assertEqual(
            namespace.query.call_args_list[1].kwargs["filters"], ("id", "Gt", "a")
        )

    def test_operator_scope_uses_only_explicit_payload_values(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "POWERPACKS_DEFAULT_SET_ID": "ambient-set",
                "POWERSET_DEFAULT_SET_ID": "ambient-set-2",
                "POWERPACKS_DEFAULT_OPERATOR_ID": "ambient-operator",
            },
        ):
            self.assertEqual(search_common.allowed_operator_ids_from_payload({}), [])
            self.assertEqual(
                search_common.allowed_operator_ids_from_payload(
                    {"set_id": "explicit-set", "operator_ids": ["op-1", "op-1", "op-2"]}
                ),
                ["op-1", "op-2"],
            )

    def test_remote_hydration_restores_scoped_interactions_and_attribution(self) -> None:
        from packs.search.backends.turbopuffer import runner as runner_module

        runner = runner_module.TurboPufferSearchRunner(
            PowersetCorpus("set-1", ("op-1", "op-2"))
        )
        frontier = CandidateFrontier(
            (CandidateRecord("person-1", matched_position_ids=("position-1",)),),
            1,
            1,
            None,
            False,
        )
        person_rows = [{
            "id": "person-1",
            "hydrated_context": {
                "name": "Ada Backend",
                "positions": [{"position_id": "position-1", "title": "Engineer"}],
            },
        }]
        attribution = {
            "person-1": {
                "operators": ["Arthur"],
                "channels": ["gmail"],
                "primary_operator": "Arthur",
                "primary_channel": "gmail",
            }
        }
        with (
            mock.patch.object(runner_module.postgres_client, "fetch_person_rows", return_value=person_rows),
            mock.patch.object(
                runner_module.postgres_client,
                "fetch_interaction_counts",
                return_value={"person-1": 7},
            ) as counts,
            mock.patch.object(
                runner_module.postgres_client,
                "fetch_source_attribution",
                return_value=attribution,
            ) as sources,
        ):
            hydrated = runner.hydrate(frontier)

        counts.assert_called_once_with(
            ["person-1"], allowed_operator_ids=["op-1", "op-2"]
        )
        sources.assert_called_once_with(
            ["person-1"], allowed_operator_ids=["op-1", "op-2"]
        )
        candidate = hydrated.candidates[0]
        self.assertEqual(candidate.matched_position_indexes, (0,))
        self.assertEqual(candidate.hydrated_profile["total_interactions"], 7)
        self.assertEqual(candidate.hydrated_profile["source_operators"], ["Arthur"])
        self.assertEqual(candidate.hydrated_profile["source_channels"], ["gmail"])
        self.assertEqual(candidate.hydrated_profile["primary_source_operator"], "Arthur")
        self.assertEqual(candidate.hydrated_profile["primary_source_channel"], "gmail")

    def test_enumeration_can_include_every_live_attribute(self) -> None:
        row = SimpleNamespace(
            id="a",
            vector=[0.1, 0.2],
            model_extra={"contract_attribute": "value", "live_only_attribute": 7},
        )
        namespace = SimpleNamespace(
            query=mock.Mock(return_value=SimpleNamespace(rows=[row]))
        )
        with mock.patch.object(turbopuffer_client, "namespace", return_value=namespace):
            result = asyncio.run(
                turbopuffer_client.enumerate_filter_only_rows_for_namespace(
                    "people", None, True, page_size=10
                )
            )
        self.assertEqual(
            result["rows"],
            [{
                "id": "a",
                "contract_attribute": "value",
                "live_only_attribute": 7,
                "vector": [0.1, 0.2],
            }],
        )
        self.assertIs(namespace.query.call_args.kwargs["include_attributes"], True)

    def test_all_attribute_enumeration_paginates_to_exhaustion(self) -> None:
        pages = [
            [SimpleNamespace(id="a", vector=[0.1], model_extra={"live_only": "first"})],
            [SimpleNamespace(id="b", vector=[0.2], model_extra={"live_only": "second"})],
            [],
        ]
        namespace = SimpleNamespace(
            query=mock.Mock(side_effect=[SimpleNamespace(rows=page) for page in pages])
        )
        with mock.patch.object(turbopuffer_client, "namespace", return_value=namespace):
            result = asyncio.run(
                turbopuffer_client.enumerate_filter_only_rows_for_namespace(
                    "people", None, True, page_size=1
                )
            )

        self.assertEqual(
            result["rows"],
            [
                {"id": "a", "live_only": "first", "vector": [0.1]},
                {"id": "b", "live_only": "second", "vector": [0.2]},
            ],
        )
        self.assertEqual(result["batch_count"], 3)
        self.assertTrue(result["completed"])
        self.assertFalse(result["truncated"])
        calls = namespace.query.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call.kwargs["include_attributes"] is True for call in calls))
        self.assertNotIn("filters", calls[0].kwargs)
        self.assertEqual(calls[1].kwargs["filters"], ("id", "Gt", "a"))
        self.assertEqual(calls[2].kwargs["filters"], ("id", "Gt", "b"))

    def test_page_consumer_does_not_accumulate_rows(self) -> None:
        pages = [
            [SimpleNamespace(id="a", vector=[0.1], model_extra={"live_only": "first"})],
            [SimpleNamespace(id="b", vector=[0.2], model_extra={"live_only": "second"})],
            [],
        ]
        namespace = SimpleNamespace(
            query=mock.Mock(side_effect=[SimpleNamespace(rows=page) for page in pages])
        )
        consumed = []
        with mock.patch.object(turbopuffer_client, "namespace", return_value=namespace):
            result = asyncio.run(
                turbopuffer_client.consume_filter_only_pages_for_namespace(
                    "people", None, True, consumed.append, page_size=1
                )
            )

        self.assertNotIn("rows", result)
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(
            consumed,
            [
                [{"id": "a", "live_only": "first", "vector": [0.1]}],
                [{"id": "b", "live_only": "second", "vector": [0.2]}],
            ],
        )

    def test_page_consumer_rejects_non_increasing_ids(self) -> None:
        namespace = SimpleNamespace(
            query=mock.Mock(
                return_value=SimpleNamespace(
                    rows=[SimpleNamespace(id="b", model_extra={}), SimpleNamespace(id="a", model_extra={})]
                )
            )
        )
        with mock.patch.object(turbopuffer_client, "namespace", return_value=namespace):
            with self.assertRaisesRegex(RuntimeError, "non-increasing ids"):
                asyncio.run(
                    turbopuffer_client.consume_filter_only_pages_for_namespace(
                        "people", None, True, lambda page: None, page_size=10
                    )
                )

    def test_namespace_schema_serializes_sdk_models_and_mapping_fallbacks(self) -> None:
        sdk_config = mock.Mock()
        sdk_config.model_dump.return_value = {
            "type": "string",
            "filterable": True,
        }
        namespace = SimpleNamespace(
            schema=mock.Mock(
                return_value={
                    "sdk_attribute": sdk_config,
                    "mapping_attribute": {"type": "integer", "optional": True},
                }
            )
        )
        with mock.patch.object(turbopuffer_client, "namespace", return_value=namespace):
            result = turbopuffer_client.namespace_schema("people")

        self.assertEqual(
            result,
            {
                "sdk_attribute": {"type": "string", "filterable": True},
                "mapping_attribute": {"type": "integer", "optional": True},
            },
        )
        sdk_config.model_dump.assert_called_once_with(
            mode="json", by_alias=True, exclude_none=True
        )

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

    def test_summary_search_uses_summary_namespace_filters_and_person_grain(self) -> None:
        row = SimpleNamespace(
            id="person-1",
            model_extra={"person_id": "person-1", "summary": "Built distributed systems"},
        )
        namespace = SimpleNamespace(
            multi_query=mock.Mock(return_value=SimpleNamespace(results=[SimpleNamespace(rows=[row])]))
        )
        filters = (
            "And",
            [("allowed_operator_ids", "ContainsAny", ["operator"]), ("id", "In", ["person-1"])],
        )
        with (
            mock.patch.object(turbopuffer_client, "namespace", return_value=namespace),
            mock.patch.object(turbopuffer_client, "embedding", new=mock.AsyncMock(return_value=[0.1, 0.2])),
        ):
            rows = asyncio.run(
                turbopuffer_client.hybrid_summary_rows(
                    {"semantic_query": "distributed systems", "bm25_queries": ["distributed systems"]},
                    filters,
                    top_k=10,
                    include_attributes=["person_id", "summary"],
                )
            )

        self.assertEqual(rows[0]["person_id"], "person-1")
        self.assertNotIn("position_id", rows[0])
        queries = namespace.multi_query.call_args.kwargs["queries"]
        self.assertTrue(all(query["filters"] == filters for query in queries))
        self.assertEqual({query["rank_by"][0] for query in queries}, {"summary_tokens", "vector"})

    def test_company_signal_search_is_company_grain_and_operator_scoped(self) -> None:
        row = SimpleNamespace(id="company-1", model_extra={"signals_semantic_text": "API infrastructure"})
        namespace = SimpleNamespace(query=mock.Mock(return_value=SimpleNamespace(rows=[row])))
        filters = ("allowed_operator_ids", "ContainsAny", ["operator"])
        with (
            mock.patch.object(turbopuffer_client, "namespace", return_value=namespace),
            mock.patch.object(turbopuffer_client, "embedding", new=mock.AsyncMock(return_value=[0.1, 0.2])),
        ):
            rows = asyncio.run(
                turbopuffer_client.semantic_company_signal_rows(
                    "API infrastructure",
                    filters,
                    top_k=10,
                    include_attributes=["signals_semantic_text"],
                )
            )

        self.assertEqual(rows[0]["company_id"], "company-1")
        self.assertEqual(rows[0]["score"], 1.0)
        self.assertEqual(namespace.query.call_args.kwargs["filters"], filters)
        self.assertEqual(namespace.query.call_args.kwargs["rank_by"][:2], ("vector", "kNN"))

    def test_required_location_families_can_be_conjunctive(self) -> None:
        filters = turbopuffer_client.filters_from_role_payload({
            "cities": ["London"],
            "countries": ["United Kingdom"],
            "location_filter_mode": "all",
        })
        self.assertEqual(filters[0], "And")
        self.assertIn(("city", "In", ["London"]), filters[1])
        self.assertIn(("country", "In", ["United Kingdom"]), filters[1])

        schema = json.loads((ROOT / "packs/search/schemas/role-search-filters.schema.json").read_text())
        jsonschema.validate({
            "cities": ["London"],
            "countries": ["United Kingdom"],
            "location_filter_mode": "all",
        }, schema)

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
                "output": {"role_search_filters": {"company_ids": ["c1"], "bm25_queries": ["founders"], "role_ids": ["founder"]}},
            }],
        }

        payload = turbopuffer_client.role_payload_from_state(state)
        filters = turbopuffer_client.filters_from_role_payload(payload)

        self.assertEqual(payload["role_ids"], ["founder"])
        self.assertNotIn("seniority_bands", payload)
        self.assertEqual(payload["search_mode"], "COMPANY_INTERSECTION")
        self.assertIn(("role_ids", "ContainsAny", ["founder"]), filters[1])
        self.assertIn(("company_id", "In", ["c1"]), filters[1])

    def test_non_shortcut_role_ids_do_not_become_hard_filters(self) -> None:
        # Deployed network-search-api expand rarely emits role_ids, so the hard
        # role_ids filter is reserved for founder/c-suite shortcut roles.
        # Precise extraction role_ids must not gate retrieval.
        filters = turbopuffer_client.filters_from_role_payload({
            "semantic_query": "AI engineers building and deploying machine learning systems in production.",
            "role_ids": ["ai_engineer", "ml_engineer"],
            "seniority_bands": ["senior"],
        })
        clauses = filters[1] if filters and filters[0] == "And" else [filters]
        self.assertFalse(any(clause[0] == "role_ids" for clause in clauses))

        mixed = turbopuffer_client.filters_from_role_payload({
            "semantic_query": "Founders and AI engineers building machine learning products end to end.",
            "role_ids": ["founder", "ai_engineer"],
            "company_ids": ["c1"],
        })
        self.assertIn(("role_ids", "ContainsAny", ["founder"]), mixed[1])

        # Extraction-emitted c-suite ids without an explicit query mention
        # (e.g. "sales leaders" -> chief_revenue_officer) must NOT gate.
        leaders = turbopuffer_client.filters_from_role_payload({
            "semantic_query": "Sales leaders owning revenue strategy, pipeline, and go-to-market teams.",
            "role_ids": ["chief_revenue_officer"],
            "seniority_bands": ["c-suite", "vice-president", "director"],
        })
        clauses = leaders[1] if leaders and leaders[0] == "And" else [leaders]
        self.assertFalse(any(clause[0] == "role_ids" for clause in clauses))

        # When the query names the role, apply_role_shortcuts flags it and the
        # hard filter applies.
        payload = turbopuffer_client.apply_role_shortcuts(
            {"role_ids": ["chief_technology_officer"], "company_ids": ["c1"]},
            "CTOs at AI companies",
        )
        csuite = turbopuffer_client.filters_from_role_payload(payload)
        self.assertIn(("role_ids", "ContainsAny", ["chief_technology_officer"]), csuite[1])

    def test_local_title_cluster_keywords_do_not_trigger_role_shortcuts(self) -> None:
        # Regression: the local pipeline merges DuckDB title-cluster keywords
        # into bm25_queries before retrieval. A corpus title like
        # "Founder & CEO (...)" must not flip a software-engineer query into a
        # hard founder/c-suite role_ids filter (prod detects shortcuts from the
        # raw query before title clustering, so clustered titles never feed
        # shortcut detection there).
        payload = turbopuffer_client.apply_role_shortcuts(
            {
                "bm25_queries": [
                    "software engineer",
                    "backend engineer",
                    "Software Engineer",
                    "Founder & CEO (hiring AI & robotics engineers!)",
                ],
                "local_title_cluster_keywords": [
                    "Software Engineer",
                    "Founder & CEO (hiring AI & robotics engineers!)",
                ],
                "role_tracks": ["engineering"],
            },
            "software engineers in sf that went to stanford",
        )

        self.assertNotIn("role_ids", payload)
        self.assertNotIn("seniority_bands", payload)
        filters = turbopuffer_client.filters_from_role_payload(payload)
        self.assertNotIn("role_ids", json.dumps(filters))

    def test_strip_is_current_filter_removes_only_currentness_clause(self) -> None:
        filters = (
            "And",
            [
                ("metro_areas", "ContainsAny", ["San Francisco Bay Area"]),
                ("is_current", "Eq", True),
                ("role_track", "In", ["engineering"]),
            ],
        )
        stripped = turbopuffer_client._search_common.strip_is_current_filter(filters)
        self.assertEqual(
            stripped,
            (
                "And",
                [
                    ("metro_areas", "ContainsAny", ["San Francisco Bay Area"]),
                    ("role_track", "In", ["engineering"]),
                ],
            ),
        )
        self.assertIsNone(turbopuffer_client._search_common.strip_is_current_filter(("is_current", "Eq", True)))
        self.assertIsNone(turbopuffer_client._search_common.strip_is_current_filter(None))

    def test_founder_text_never_triggers_shortcut_without_extracted_role_ids(self) -> None:
        # Role intent is owned by query extraction (mirrors network-search-api,
        # where role_ids come only from LLM extraction). Founder-ish words in
        # bm25_queries or the raw query never flip the shortcut on their own.
        payload = turbopuffer_client.apply_role_shortcuts(
            {
                "bm25_queries": ["founder", "co-founder", "Software Engineer"],
                "local_title_cluster_keywords": ["Software Engineer"],
            },
            "startup founders",
        )
        self.assertNotIn("role_ids", payload)

        extracted = turbopuffer_client.apply_role_shortcuts(
            {"role_ids": ["founder"], "bm25_queries": ["founder"]},
            "startup founders",
        )
        self.assertIn("founder", extracted["role_ids"])

    def test_founders_fund_investor_query_does_not_trigger_founder_shortcut(self) -> None:
        payload = turbopuffer_client.apply_role_shortcuts({"investor_names": ["Founders Fund"]}, "people backed by Founders Fund")

        self.assertNotIn("role_ids", payload)

    def test_csuite_shortcut_adds_canonical_role_id(self) -> None:
        payload = turbopuffer_client.apply_role_shortcuts({"company_ids": ["c1"]}, "CTOs at AI companies")
        filters = turbopuffer_client.filters_from_role_payload(payload)

        self.assertIn("chief_technology_officer", payload["role_ids"])
        self.assertEqual(payload["seniority_bands"], ["c-suite"])
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
            ]
        }

        payload = turbopuffer_client.role_payload_from_state(state)
        self.assertEqual(payload["education_ids"], ["urn:harmonic:school:stanford"])
        self.assertEqual(payload["company_ids"], ["urn:harmonic:company:meta"])

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


    def test_dedupe_people_limit_zero_keeps_full_frontier(self) -> None:
        rows = [
            {"id": "p1-0", "base_id": "p1", "score": 1.0},
            {"id": "p2-0", "base_id": "p2", "score": 0.9},
            {"id": "p3-0", "base_id": "p3", "score": 0.8},
        ]
        self.assertEqual(len(search_result_merge.dedupe_people(rows, limit=0)), 3)
        self.assertEqual(len(search_result_merge.dedupe_people(rows, limit=2)), 2)






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

        merged = search_result_merge.merge_company_union_candidates(candidates, union, limit=0)

        self.assertEqual([row["person_id"] for row in merged], ["p1", "p2"])
        self.assertEqual(merged[0]["vertical_sources"], ["hybrid", "company_filter"])
        self.assertEqual(merged[0]["matched_position_ids"], ["p1-0"])
        self.assertEqual(merged[1]["position_id"], "p2-0")
        self.assertEqual(merged[1]["vertical_sources"], ["company_filter"])







    def test_scripts_do_not_import_aleph_mvp(self) -> None:
        for path in [
            TURBOPUFFER / "turbopuffer_search_backend.py",
            TURBOPUFFER / "turbopuffer_resolve_education.py",
            ROOT / "packs/search/backends/turbopuffer" / "resolution.py",
            TURBOPUFFER / "turbopuffer_resolve_companies.py",
        ]:
            text = path.read_text()
            self.assertNotIn("aleph-mvp", text)
            self.assertNotIn("api_v2", text)
            self.assertNotIn("shared.env_config", text)


if __name__ == "__main__":
    unittest.main()
