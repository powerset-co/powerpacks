import asyncio
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
local_filter_eval = load_module("local_filter_eval", LIB / "local_filter_eval.py")
apply_prefilters = load_module("apply_prefilters", ROOT / "packs/search/primitives/apply_prefilters" / "apply_prefilters.py")
resolve_education = load_module("resolve_education", ROOT / "packs/search/primitives/resolve_education" / "resolve_education.py")
execute_role_search = load_module("execute_role_search", ROOT / "packs/search/primitives/execute_role_search" / "execute_role_search.py")


LONG_FOUNDER_QUERY = (
    "Started and built a company from scratch as a founder or co-founder, hired early teams, "
    "owned company-building outcomes, raised capital, and led strategy for a startup."
)
LONG_BACKEND_QUERY = (
    "Builds production backend software systems, APIs, data infrastructure, distributed services, "
    "and scalable platform engineering with hands-on coding responsibilities."
)


class LocalDuckDBFixtureMixin:
    def setUp(self) -> None:
        self._old_env = {key: os.environ.get(key) for key in ["POWERPACKS_LOCAL_SEARCH_DB"]}
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "local-search.duckdb")
        self._create_fixture(self.db_path)
        os.environ["POWERPACKS_LOCAL_SEARCH_DB"] = self.db_path
        turbopuffer_client._local_store_for_path.cache_clear()

    def tearDown(self) -> None:
        turbopuffer_client._local_store_for_path.cache_clear()
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmpdir.cleanup()

    def _create_fixture(self, db_path: str) -> None:
        import duckdb

        con = duckdb.connect(db_path)
        try:
            con.execute(
                """
                create table local_people_positions (
                    id varchar,
                    position_id varchar,
                    person_id varchar,
                    base_id varchar,
                    position_title varchar,
                    company_id varchar,
                    company_name varchar,
                    city varchar,
                    state varchar,
                    country varchar,
                    macro_region varchar,
                    metro_areas varchar[],
                    role_track varchar,
                    seniority_band varchar,
                    role_ids varchar[],
                    is_current boolean,
                    total_years_experience integer,
                    allowed_operator_ids varchar[],
                    start_date_epoch integer,
                    end_date_epoch integer,
                    inferred_birth_year integer,
                    x_twitter_followers integer,
                    linkedin_followers integer,
                    linkedin_connections integer,
                    ig_followers integer,
                    phrase_tokens varchar[],
                    word_tokens varchar[],
                    vector double[]
                )
                """
            )
            people_rows = [
                (
                    "person-founder-0", "pos-founder-current", "person-founder", "person-founder",
                    "Founder and CEO", "company-startup", "Acme AI", "San Francisco", "California", "United States",
                    "North America", ["San Francisco Bay Area"], "founder", "c_suite", ["founder", "chief_executive_officer"], True,
                    12, ["op1", "op-founder"], 1577836800, 0, 1988, 1000, 5000, 3000, 100,
                    ["founder", "founder ceo", "co founder", "startup founder"],
                    ["founder", "ceo", "startup", "company", "builder", "product"], [1.0, 0.0, 0.0],
                ),
                (
                    "person-founder-1", "pos-founder-past-product", "person-founder", "person-founder",
                    "Product Manager", "company-product", "Box", "San Francisco", "California", "United States",
                    "North America", ["San Francisco Bay Area"], "product", "manager", ["product_manager"], False,
                    12, ["op1", "op-founder"], 1420070400, 1546300800, 1988, 1000, 5000, 3000, 100,
                    ["product manag", "product"], ["product", "manager", "roadmap", "experiments"], [0.4, 0.4, 0.0],
                ),
                (
                    "person-engineer-0", "pos-engineer-current", "person-engineer", "person-engineer",
                    "Backend Engineer", "company-infra", "InfraDB", "New York", "New York", "United States",
                    "North America", ["New York City Metropolitan Area"], "engineering", "senior", ["software_engineer", "backend_engineer"], True,
                    8, ["op1", "op-eng"], 1609459200, 0, 1992, 120, 1500, 1200, 40,
                    ["backend engin", "softwar engin", "distribut system"],
                    ["backend", "engineer", "python", "distributed", "systems", "api", "services"], [0.0, 1.0, 0.0],
                ),
            ]
            con.executemany("insert into local_people_positions values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", people_rows)

            con.execute(
                """
                create table local_summaries (
                    id varchar,
                    person_id varchar,
                    base_id varchar,
                    summary varchar,
                    tech_skills varchar[],
                    allowed_operator_ids varchar[],
                    phrase_tokens varchar[],
                    word_tokens varchar[],
                    vector double[]
                )
                """
            )
            con.executemany(
                "insert into local_summaries values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("person-founder", "person-founder", "person-founder", "Founder with product leadership background", ["Product", "Go"], ["op1"], ["founder product"], ["founder", "product", "startup"], [1.0, 0.0, 0.0]),
                    ("person-engineer", "person-engineer", "person-engineer", "Backend engineer building Python services", ["Python", "DuckDB", "Kubernetes"], ["op1"], ["backend engin"], ["backend", "engineer", "python", "duckdb"], [0.0, 1.0, 0.0]),
                ],
            )

            con.execute(
                """
                create table local_people_education (
                    id varchar,
                    person_id varchar,
                    base_id varchar,
                    canonical_education_id varchar,
                    school_name varchar,
                    degree_normalized varchar,
                    field_of_study varchar,
                    graduation_year integer,
                    allowed_operator_ids varchar[]
                )
                """
            )
            con.executemany(
                "insert into local_people_education values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("edu-founder-stanford", "person-founder", "person-founder", "school-stanford", "Stanford University", "Bachelors", "Computer Science", 2010, ["op1"]),
                    ("edu-engineer-mit", "person-engineer", "person-engineer", "school-mit", "Massachusetts Institute of Technology", "Masters", "Electrical Engineering and Computer Science", 2014, ["op1"]),
                ],
            )

            con.execute(
                """
                create table local_education (
                    id varchar,
                    canonical_education_id varchar,
                    school_name_tokens varchar[],
                    school_name varchar,
                    display_value varchar,
                    person_count integer
                )
                """
            )
            con.executemany(
                "insert into local_education values (?, ?, ?, ?, ?, ?)",
                [
                    ("school-stanford", "school-stanford", ["stanford", "university"], "Stanford University", "Stanford University", 200000),
                    ("school-mit", "school-mit", ["massachusetts", "institute", "of", "technology", "mit"], "Massachusetts Institute of Technology", "MIT", 150000),
                ],
            )
        finally:
            con.close()


class LocalDuckDBBackendTests(LocalDuckDBFixtureMixin, unittest.TestCase):
    def test_hard_filters_distinguish_founder_and_engineer(self) -> None:
        founder_payload = {
            "cities": ["San Francisco"],
            "states": ["California"],
            "role_ids": ["founder"],
            "company_ids": ["company-startup"],
            "is_current_company": True,
            "operator_ids": ["op-founder"],
        }
        filters = turbopuffer_client.filters_from_role_payload(founder_payload)
        rows = asyncio.run(turbopuffer_client.filter_only_rows_for_namespace("people", filters, ["base_id", "position_title", "role_track"]))
        self.assertEqual([row["base_id"] for row in rows], ["person-founder"])
        self.assertEqual(rows[0]["position_title"], "Founder and CEO")

        engineer_filters = turbopuffer_client.filters_from_role_payload({
            "cities": ["New York"],
            "role_tracks": ["engineering"],
            "is_current_role": True,
            "operator_ids": ["op-eng"],
        })
        engineer_rows = asyncio.run(turbopuffer_client.filter_only_rows_for_namespace("people", engineer_filters, ["base_id", "position_title"]))
        self.assertEqual([row["base_id"] for row in engineer_rows], ["person-engineer"])

    def test_education_prefilter_feeds_role_search(self) -> None:
        payload = {"education_ids": ["school-stanford"], "degree_levels": ["bachelors"], "fields_of_study": ["computer science"]}
        base_ids, meta = asyncio.run(apply_prefilters.education_base_ids(payload, page_size=1000, max_ids=10))
        self.assertEqual(base_ids, ["person-founder"])
        self.assertEqual(meta["stage"], "education")

        role_payload = {"base_candidate_ids": base_ids, "role_ids": ["founder"], "is_current_role": True}
        rows = asyncio.run(turbopuffer_client.hybrid_role_rows(
            role_payload,
            turbopuffer_client.filters_from_role_payload(role_payload),
            top_k=10,
            include_attributes=["base_id", "position_title"],
        ))
        self.assertEqual([row["person_id"] for row in rows], ["person-founder"])
        self.assertEqual(rows[0]["retrieval_mode"], "filter_only")

    def test_tech_skill_prefilter(self) -> None:
        ids, meta = asyncio.run(apply_prefilters.tech_skill_base_ids({"tech_skills": ["DuckDB"]}, page_size=1000, max_ids=10))
        self.assertEqual(ids, ["person-engineer"])
        self.assertEqual(meta["matched"], 1)

    def test_school_resolver_and_namespace_prefix_tokens(self) -> None:
        response = turbopuffer_client.namespace("schools").query(
            filters=("school_name", "ContainsAllTokens", "Stan", {"last_as_prefix": True}),
            top_k=10,
            include_attributes=["school_name", "person_count"],
        )
        self.assertEqual(response.rows[0].id, "school-stanford")
        self.assertEqual(response.rows[0].school_name, "Stanford University")

        out = asyncio.run(resolve_education.run(SimpleNamespace(
            state=None,
            payload_json=json.dumps({"education_names": ["Stan"]}),
            env_file=None,
            max_rows_per_name=10,
        )))
        self.assertEqual(out["education_ids"], ["school-stanford"])

    def test_namespace_rank_by_includes_score(self) -> None:
        response = turbopuffer_client.namespace("people").query(
            rank_by=("vector", "kNN", [1.0, 0.0, 0.0]),
            filters=("is_current", "Eq", True),
            top_k=1,
            include_attributes=["base_id"],
        )
        self.assertEqual(response.rows[0].id, "person-founder-0")
        self.assertGreater(response.rows[0].score, 0.0)

    def test_light_semantic_bm25_role_search_with_deterministic_embedding(self) -> None:
        original_embedding = turbopuffer_client.embedding

        async def fake_embedding(text: str):
            return [0.0, 1.0, 0.0] if "backend" in text.lower() else [1.0, 0.0, 0.0]

        turbopuffer_client.embedding = fake_embedding
        try:
            founder_rows = asyncio.run(turbopuffer_client.hybrid_role_rows(
                {"semantic_query": LONG_FOUNDER_QUERY, "bm25_queries": ["founder CEO"], "is_current_role": True},
                turbopuffer_client.filters_from_role_payload({"is_current_role": True}),
                top_k=5,
                include_attributes=["base_id", "position_title"],
            ))
            engineer_rows = asyncio.run(turbopuffer_client.hybrid_role_rows(
                {"semantic_query": LONG_BACKEND_QUERY, "bm25_queries": ["backend engin"], "is_current_role": True},
                turbopuffer_client.filters_from_role_payload({"is_current_role": True}),
                top_k=5,
                include_attributes=["base_id", "position_title"],
            ))
        finally:
            turbopuffer_client.embedding = original_embedding

        self.assertEqual(founder_rows[0]["person_id"], "person-founder")
        self.assertEqual(founder_rows[0]["position_title"], "Founder and CEO")
        self.assertEqual(engineer_rows[0]["person_id"], "person-engineer")
        self.assertEqual(engineer_rows[0]["position_title"], "Backend Engineer")

    def test_explicit_query_embedding_does_not_force_filter_only_for_short_query(self) -> None:
        rows = asyncio.run(turbopuffer_client._hybrid_role_rows_single(
            {"semantic_query": "backend", "bm25_queries": ["backend engin"]},
            ("is_current", "Eq", True),
            top_k=5,
            include_attributes=["base_id", "position_title"],
            query_embedding=[0.0, 1.0, 0.0],
        ))
        self.assertEqual(rows[0]["person_id"], "person-engineer")
        self.assertEqual(rows[0]["retrieval_mode"], "hybrid")

    def test_bm25_adjacency_path(self) -> None:
        rows = asyncio.run(turbopuffer_client.bm25_adjacency_rows(
            ["backend engin"],
            ("is_current", "Eq", True),
            top_k=5,
            include_attributes=["base_id", "position_title"],
        ))
        self.assertEqual(rows[0]["person_id"], "person-engineer")
        self.assertEqual(rows[0]["retrieval_mode"], "company_adjacency_bm25")
        self.assertEqual(rows[0]["adjacency_query_indexes"], [0])

    def test_execute_role_search_local_payload_candidate_shape(self) -> None:
        original_embedding = execute_role_search.hybrid_role_rows

        async def fake_hybrid_role_rows(payload, filters, *, top_k, include_attributes):
            local_payload = dict(payload)
            local_payload["query_embedding"] = [0.0, 1.0, 0.0]
            return await turbopuffer_client.local_store().hybrid_role_rows(local_payload, filters, top_k, include_attributes)

        execute_role_search.hybrid_role_rows = fake_hybrid_role_rows
        try:
            out = asyncio.run(execute_role_search.run(SimpleNamespace(
                state=None,
                payload_json=json.dumps({"semantic_query": LONG_BACKEND_QUERY, "bm25_queries": ["backend engin"], "is_current_role": True}),
                env_file=None,
                top_k=5,
                limit=0,
                write_state=False,
                write_artifact=False,
            )))
        finally:
            execute_role_search.hybrid_role_rows = original_embedding

        self.assertEqual(out["candidate_ids"][0], "person-engineer")
        candidate = out["candidates"][0]
        self.assertEqual(candidate["person_id"], "person-engineer")
        self.assertEqual(candidate["position_id"], "pos-engineer-current")
        self.assertIn("hybrid", candidate["vertical_sources"])
        self.assertIn("matched_position_ids", candidate)

    def test_local_store_lazy_import_and_clear_namespace_errors(self) -> None:
        import local_duckdb_store

        missing_path = str(Path(self.tmpdir.name) / "missing-table.duckdb")
        import duckdb
        con = duckdb.connect(missing_path)
        con.close()
        with self.assertRaisesRegex(local_duckdb_store.LocalDuckDBError, "local_people_positions"):
            local_duckdb_store.LocalDuckDBSearchStore(missing_path).namespace("people")
        with self.assertRaisesRegex(local_duckdb_store.LocalDuckDBError, "unknown local DuckDB namespace"):
            turbopuffer_client.local_store().namespace("unsupported")

    def test_filter_only_respects_max_results_and_projects_id(self) -> None:
        rows = asyncio.run(turbopuffer_client.filter_only_rows_for_namespace(
            "people",
            None,
            ["base_id"],
            page_size=1,
            max_results=2,
        ))
        self.assertEqual([row["id"] for row in rows], ["person-engineer-0", "person-founder-0"])
        self.assertEqual([row["base_id"] for row in rows], ["person-engineer", "person-founder"])

    def test_default_mode_safety(self) -> None:
        os.environ.pop("POWERPACKS_LOCAL_SEARCH_DB", None)
        turbopuffer_client._local_store_for_path.cache_clear()
        self.assertFalse(turbopuffer_client.is_local_backend())
        self.assertIsInstance(turbopuffer_client.namespace_name("people"), str)
        with self.assertRaises(RuntimeError):
            turbopuffer_client.local_store()


class LocalFilterEvalTests(unittest.TestCase):
    def test_supported_operators_nested_and_none(self) -> None:
        row = {
            "name": "Alice Founder",
            "age": 35,
            "city": "San Francisco",
            "tags": ["founder", "python"],
            "school_name_tokens": ["stanford", "university"],
        }
        self.assertTrue(local_filter_eval.filter_matches(row, None))
        self.assertTrue(local_filter_eval.filter_matches(row, ("And", [("age", "Gt", 30), ("age", "Gte", 35), ("age", "Lt", 40), ("age", "Lte", 35)])))
        self.assertTrue(local_filter_eval.filter_matches(row, ("Or", [("city", "Eq", "New York"), ("city", "Eq", "San Francisco")])))
        self.assertTrue(local_filter_eval.filter_matches(row, ("city", "In", ["San Francisco"])))
        self.assertTrue(local_filter_eval.filter_matches(row, ("city", "NotIn", ["Boston"])))
        self.assertTrue(local_filter_eval.filter_matches(row, ("city", "NotEq", "Boston")))
        self.assertTrue(local_filter_eval.filter_matches(row, ("tags", "ContainsAny", ["python"])))
        self.assertTrue(local_filter_eval.filter_matches(row, ("school_name", "ContainsAllTokens", "Stanford Univ", {"last_as_prefix": True})))
        self.assertTrue(local_filter_eval.filter_matches(row, ("name", "IGlob", "alice*")))
        self.assertFalse(local_filter_eval.filter_matches(row, ("tags", "ContainsAny", ["rust"])))
        self.assertEqual(local_filter_eval.filter_rows([row, {"age": 20}], ("age", "Gte", 30)), [row])

    def test_malformed_filters_raise(self) -> None:
        with self.assertRaises(ValueError):
            local_filter_eval.filter_matches({}, [])
        with self.assertRaises(ValueError):
            local_filter_eval.filter_matches({}, ("And", "not-a-list"))
        with self.assertRaises(ValueError):
            local_filter_eval.filter_matches({}, ("field", "Unknown", 1))
        with self.assertRaises(ValueError):
            local_filter_eval.filter_matches({}, ("field", "Eq", 1, {}))
        with self.assertRaises(ValueError):
            local_filter_eval.filter_matches({}, ("field", "ContainsAllTokens", "x", "bad-options"))


if __name__ == "__main__":
    unittest.main()
