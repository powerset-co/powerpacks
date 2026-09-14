from __future__ import annotations

import importlib.util
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from packs.indexing.lib.artifacts import build_summary_records
from packs.indexing.lib.people import build_roles, build_unified_profiles, flatten_people
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.search.primitives.local.local_duckdb_store import LocalDuckDBSearchStore
from packs.search.primitives.execute_role_search import execute_role_search
from packs.shared.csv_io import CsvIO


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dossier_search_shim", ROOT / "scripts/build-local-duckdb-shim.py")
shim = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = shim
spec.loader.exec_module(shim)


class DossierLocalSearchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        csv = self.root / "people.csv"
        CsvIO.write_dict_rows(csv, PEOPLE_SCHEMA_COLUMNS, [{
            "id": "candidate:email:jordan@example.com", "full_name": "Jordan Bravo",
            "summary": "Astronomy club friend; organized telescope nights in 2022.",
            "primary_email": "jordan@example.com", "source_channels": '["gmail_msgvault"]',
        }])
        people = flatten_people(csv)
        roles = build_roles(people)
        self.assertEqual(len(people), 1)
        self.assertEqual(roles, [])
        self.person_id = people[0]["id"]
        summaries = build_summary_records(build_unified_profiles(people), "operator-test")["summaries"]
        self.assertEqual(len(summaries), 1)
        # Precomputed fixture vectors exercise local ranking without an embedding provider.
        summaries[0]["vector"] = [1.0, 0.0]
        for table, filename, rows in (
            ("local_people_positions", "people.records.parquet", roles),
            ("local_summaries", "summaries.records.parquet", summaries),
        ):
            shim.write_parquet_rows(self.root / "records" / filename, rows,
                float_array_fields=("vector",), schema=shim.ALL_LOCAL_TABLE_CONTRACT[table])
        db, counts, _ = shim.load_duckdb(self.root, "operator-test", person_profiles_csv=csv)
        self.assertEqual(counts["local_people_positions"], 0)
        self.assertEqual(counts["local_person_profiles"], 1)
        self.assertEqual(counts["local_summaries"], 1)
        self.store = LocalDuckDBSearchStore(str(db))
        self.db_path = str(db)

    def tearDown(self):
        self.store.conn.close()
        self.temp.cleanup()

    def search(self, payload, filters=None):
        return self.store.summary_search_rows(payload, filters, 10, ["person_id", "summary"])

    def test_keyword_and_precomputed_vector_find_contact_without_roles(self):
        for payload in ({"bm25_queries": ["astronomy"]}, {"query_embedding": [1.0, 0.0]}):
            with self.subTest(payload=payload):
                rows = self.search(payload)
                self.assertEqual([row["person_id"] for row in rows], [self.person_id])

    def test_person_filters_do_not_require_a_role(self):
        filters = ["And", [
            ["allowed_operator_ids", "ContainsAny", ["operator-test"]],
            ["full_name", "Eq", "Jordan Bravo"],
        ]]
        for payload in ({"bm25_queries": ["astronomy"]}, {"query_embedding": [1.0, 0.0]}):
            with self.subTest(payload=payload):
                rows = self.search(payload, filters)
                self.assertEqual([row["person_id"] for row in rows], [self.person_id])

    def test_operator_and_actual_role_requirements_still_exclude_contact(self):
        for filters in (
            ["allowed_operator_ids", "ContainsAny", ["another-operator"]],
            ["And", [["allowed_operator_ids", "ContainsAny", ["operator-test"]],
                     ["position_title", "Eq", "Founder"]]],
        ):
            with self.subTest(filters=filters):
                self.assertEqual(self.search({"bm25_queries": ["astronomy"]}, filters), [])

    def test_summary_age_filter_keeps_role_birth_year_precedence(self):
        store = LocalDuckDBSearchStore(":memory:", read_only=False)
        self.addCleanup(store.conn.close)
        store.conn.execute("CREATE TABLE local_person_profiles (person_id VARCHAR, inferred_birth_year INTEGER)")
        store.conn.execute("CREATE TABLE local_people_positions (id VARCHAR, person_id VARCHAR, inferred_birth_year INTEGER)")
        store.conn.execute("CREATE TABLE local_summaries (id VARCHAR, person_id VARCHAR, summary VARCHAR, vector DOUBLE[])")
        store.conn.execute("INSERT INTO local_summaries VALUES ('summary-jordan', 'jordan', 'Astronomy', [1.0, 0.0])")
        store.conn.execute("INSERT INTO local_person_profiles VALUES ('jordan', NULL)")
        store.conn.execute("INSERT INTO local_people_positions VALUES ('role-jordan', 'jordan', NULL)")
        filters = ["inferred_birth_year", "Gte", 1990]
        for role_year, profile_year, expected in ((1998, 1980, ["jordan"]), (1980, 1998, [])):
            with self.subTest(role_year=role_year, profile_year=profile_year):
                store.conn.execute("UPDATE local_people_positions SET inferred_birth_year=?", [role_year])
                store.conn.execute("UPDATE local_person_profiles SET inferred_birth_year=?", [profile_year])
                rows = store.summary_search_rows({"query_embedding": [1.0, 0.0]}, filters, 10, ["person_id"])
                self.assertEqual([row["person_id"] for row in rows], expected)

    def test_execute_search_returns_summary_candidate_with_person_id_scope(self):
        import local_search_backend

        local_search_backend.configure_local_backend(self.db_path)
        try:
            result = asyncio.run(execute_role_search.run(SimpleNamespace(
                state=None, env_file=None, top_k=10, limit=0, write_state=False, write_artifact=False,
                payload_json=json.dumps({"bm25_queries": ["astronomy"],
                    "allowed_operator_ids": ["operator-test"], "base_candidate_ids": [self.person_id]}),
            )))
            self.assertEqual(result["candidate_ids"], [self.person_id])
            self.assertEqual(result["verticals"]["role"]["row_count"], 0)
            self.assertEqual(result["verticals"]["summary"]["row_count"], 1)
        finally:
            local_search_backend._local_store_for_path(self.db_path).conn.close()
            local_search_backend._local_store_for_path.cache_clear()
            local_search_backend.configure_local_backend(None)


if __name__ == "__main__":
    unittest.main()
