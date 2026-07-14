"""Regression tests keeping TurboPuffer contracts in sync with record builders."""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from packs.indexing.lib.contracts import (
    attribute_names,
    contract_attribute_names,
    contract_duckdb_columns,
    dropped_fields,
    dropped_fields_for_records,
    load_search_contract,
    vector_metadata,
)
from packs.indexing.lib.people import CONTRACT_PERSON_COLUMNS, PEOPLE_NAMESPACE_COLUMNS

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_shim():
    return _load_module("build_local_duckdb_shim_schema_sync", ROOT / "scripts" / "build-local-duckdb-shim.py")


def _load_local_store():
    return _load_module("local_duckdb_store_schema_sync", ROOT / "packs/search/primitives/local/local_duckdb_store.py")


def _load_search_common():
    primitives = ROOT / "packs/search/primitives"
    for rel in ["lib", "shared", "local", "turbopuffer"]:
        path = str(primitives / rel)
        if path not in sys.path:
            sys.path.insert(0, path)
    return _load_module("search_common", primitives / "shared" / "search_common.py")


class ContractSchemaSyncTest(unittest.TestCase):
    def test_people_contract_declares_every_namespace_column(self):
        contract = load_search_contract("turbopuffer/people.namespace.json")
        declared = attribute_names(contract)
        missing = sorted(set(PEOPLE_NAMESPACE_COLUMNS) - declared)
        self.assertEqual(missing, [], f"people contract missing builder columns: {missing}")

    def test_people_contract_keeps_vector_metadata(self):
        contract = load_search_contract("turbopuffer/people.namespace.json")
        meta = vector_metadata(contract)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.get("dimension"), 1536)

    def test_people_builder_record_drops_nothing(self):
        contract = load_search_contract("turbopuffer/people.namespace.json")
        record = {column: "" for column in PEOPLE_NAMESPACE_COLUMNS}
        record["vector"] = [0.0] * 1536
        self.assertEqual(dropped_fields(record, contract), set())

    def test_dropped_fields_reports_unknown_names(self):
        contract = load_search_contract("turbopuffer/people.namespace.json")
        records = [
            {"id": "p1", "position_title": "Engineer"},
            {"id": "p2", "mystery_field": 1, "other_field": "x"},
        ]
        self.assertEqual(dropped_fields_for_records(records, contract), {"mystery_field", "other_field"})

    def test_people_namespace_columns_derive_from_contract(self):
        contract = load_search_contract("turbopuffer/people.namespace.json")
        self.assertEqual(PEOPLE_NAMESPACE_COLUMNS, contract_attribute_names(contract))
        self.assertEqual(len(PEOPLE_NAMESPACE_COLUMNS), 43)

    def test_contract_person_columns_derive_from_postgres_contract(self):
        contract = load_search_contract("postgres/persons.table.json")
        self.assertEqual(CONTRACT_PERSON_COLUMNS, contract_attribute_names(contract))
        self.assertEqual(len(CONTRACT_PERSON_COLUMNS), 20)


class LocalDuckdbShimContractSyncTest(unittest.TestCase):
    """The shim's LOCAL_TABLE_CONTRACT must be the contract-derived columns plus
    explicitly declared local-only bookkeeping columns."""

    def test_resolve_artifact_path_prefers_parquet_sibling(self):
        shim = _load_shim()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "records" / "people.records.jsonl"
            parquet = root / "records" / "people.records.parquet"
            jsonl.parent.mkdir()
            jsonl.write_text("{}\n", encoding="utf-8")
            self.assertEqual(shim.resolve_artifact_path(root, "records/people.records.jsonl"), jsonl)
            parquet.write_bytes(b"PAR1")
            self.assertEqual(shim.resolve_artifact_path(root, "records/people.records.jsonl"), parquet)

    def test_every_namespace_table_matches_contract_derived_columns(self):
        shim = _load_shim()
        self.assertEqual(
            sorted(shim.LOCAL_TABLE_CONTRACT_SOURCES),
            ["local_companies", "local_education", "local_people_education", "local_people_positions", "local_summaries"],
        )
        for table, contract_rel in shim.LOCAL_TABLE_CONTRACT_SOURCES.items():
            contract = load_search_contract(contract_rel)
            derived = contract_duckdb_columns(contract)
            local_only = shim.LOCAL_ONLY_TABLE_COLUMNS.get(table, {})
            self.assertEqual(
                shim.LOCAL_TABLE_CONTRACT[table],
                {**derived, **local_only},
                f"{table} columns drifted from {contract_rel}",
            )
            # Local-only columns must never shadow a contract attribute.
            self.assertEqual(set(local_only) & set(derived), set(), f"{table} local-only columns shadow contract columns")

    def test_local_only_tables_have_no_namespace_contract(self):
        shim = _load_shim()
        self.assertEqual(set(shim.LOCAL_ONLY_TABLE_CONTRACT), {"local_person_profiles", "local_company_signals"})
        self.assertEqual(set(shim.LOCAL_ONLY_TABLE_COLUMNS) & set(shim.LOCAL_ONLY_TABLE_CONTRACT), set())

    def test_contract_duckdb_columns_type_mapping(self):
        people = contract_duckdb_columns(load_search_contract("turbopuffer/people.namespace.json"))
        self.assertEqual(people["id"], "VARCHAR")
        self.assertEqual(people["word_tokens"], "VARCHAR[]")
        self.assertEqual(people["company_headcount"], "BIGINT")
        self.assertEqual(people["tenure_years"], "DOUBLE")
        self.assertEqual(people["is_current"], "BOOLEAN")
        self.assertEqual(people["vector"], "DOUBLE[]")
        education = contract_duckdb_columns(load_search_contract("turbopuffer/education.namespace.json"))
        self.assertIn("id", education)  # row key even when the contract omits it
        self.assertNotIn("vector", education)  # no vector metadata on education


class PositionPersonDedupGuardTest(unittest.TestCase):
    """Position-person dedup must never drop a column the search layer filters
    on the positions table.

    Local search resolves PERSON_PROFILE_FILTER_FIELDS through the
    local_person_profiles join, so only those fields may be deduplicated off
    local_people_positions.  Contract location attributes must always stay on
    positions: mixed location Or-clauses (city In [...] Or metro_areas
    ContainsAny [...]) are evaluated wholly on the positions table.
    """

    @classmethod
    def setUpClass(cls):
        cls.shim = _load_shim()
        cls.store = _load_local_store()
        cls.search_common = _load_search_common()
        contract = load_search_contract("turbopuffer/people.namespace.json")
        cls.contract_attributes = attribute_names(contract)
        cls.contract_filter_fields = {str(item["field"]) for item in contract.get("filters") or []}
        cls.dedup_columns = set(cls.shim.POSITION_PERSON_DUPLICATE_COLUMNS)
        cls.profile_columns = set(cls.shim.LOCAL_ONLY_TABLE_CONTRACT["local_person_profiles"])

    def test_dedup_columns_exist_on_person_profiles(self):
        missing = sorted(self.dedup_columns - self.profile_columns)
        self.assertEqual(missing, [], f"dedup would drop columns local_person_profiles does not carry: {missing}")

    def test_dedup_columns_resolve_through_profile_join(self):
        unresolved = sorted(self.dedup_columns - self.store.PERSON_PROFILE_FILTER_FIELDS)
        self.assertEqual(unresolved, [], f"dedup would drop columns local search does not resolve via the profile join: {unresolved}")

    def test_dedup_never_drops_position_resolved_contract_filter_fields(self):
        profile_join_fields = self.store.PERSON_PROFILE_FILTER_FIELDS & self.profile_columns
        position_resolved = self.contract_filter_fields - profile_join_fields
        overlap = sorted(self.dedup_columns & position_resolved)
        self.assertEqual(overlap, [], f"dedup would drop contract filter fields only resolvable on positions: {overlap}")

    def test_dedup_never_drops_location_filter_fields(self):
        location_fields = set(self.search_common.LOCATION_FIELDS)
        # Sanity: the shared location fields are real contract attributes/filters.
        self.assertEqual(sorted(location_fields - self.contract_attributes), [])
        self.assertEqual(sorted(location_fields - self.contract_filter_fields), [])
        overlap = sorted(self.dedup_columns & location_fields)
        self.assertEqual(overlap, [], f"dedup must never drop contract location fields from positions: {overlap}")

    def test_positions_table_contract_declares_all_location_columns(self):
        position_columns = set(self.shim.LOCAL_TABLE_CONTRACT["local_people_positions"])
        missing = sorted(set(self.search_common.LOCATION_FIELDS) - position_columns)
        self.assertEqual(missing, [], f"positions table contract missing location columns: {missing}")


class PositionPersonDedupLoadTest(unittest.TestCase):
    """End-to-end shim load: dedup keeps contract location data on positions."""

    POSITION = {
        "id": "11111111-1111-1111-1111-111111111111",
        "base_id": "p1",
        "person_id": "p1",
        "position_id": "11111111-1111-1111-1111-111111111111",
        "position_title": "Engineer",
        "city": "Palo Alto",
        "state": "California",
        "country": "United States",
        "macro_region": "North America",
        "metro_areas": ["San Francisco Bay Area"],
        "allowed_operator_ids": ["op1"],
        "x_twitter_followers": 5,
        "linkedin_followers": 10,
        "linkedin_connections": 7,
        "ig_followers": 2,
    }
    PROFILE = {
        "id": "p1",
        "person_id": "p1",
        "base_id": "p1",
        "full_name": "Pat Example",
        "city": "Palo Alto",
        "state": "California",
        "country": "United States",
        "allowed_operator_ids": ["op1"],
        "x_twitter_followers": 5,
        "linkedin_followers": 10,
        "linkedin_connections": 7,
        "ig_followers": 2,
    }

    @classmethod
    def setUpClass(cls):
        cls.shim = _load_shim()
        cls.store_module = _load_local_store()
        cls.tmp = tempfile.TemporaryDirectory()
        run_dir = Path(cls.tmp.name)
        records = run_dir / "records"
        records.mkdir(parents=True)
        (records / "people.records.jsonl").write_text(json.dumps(cls.POSITION) + "\n", encoding="utf-8")
        (records / "person_profiles.records.jsonl").write_text(json.dumps(cls.PROFILE) + "\n", encoding="utf-8")
        for name in ["summaries", "company_signals", "education", "schools", "companies"]:
            (records / f"{name}.records.jsonl").write_text("", encoding="utf-8")
        cls.db_path, cls.counts, _diffs = cls.shim.load_duckdb(run_dir, "op1", force=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _store(self):
        return self.store_module.LocalDuckDBSearchStore(str(self.db_path), read_only=True)

    def test_dedup_ran_and_dropped_only_profile_resolved_columns(self):
        self.assertEqual(self.counts.get("local_person_profile_position_overlap"), 1)
        self.assertEqual(self.counts.get("local_people_positions_person_columns_dropped"), 1)
        store = self._store()
        try:
            columns = set(store._table_columns("local_people_positions"))
        finally:
            store.conn.close()
        for column in ["city", "state", "country", "macro_region", "metro_areas"]:
            self.assertIn(column, columns, f"dedup dropped contract location column {column!r} from positions")
        for column in self.shim.POSITION_PERSON_DUPLICATE_COLUMNS:
            self.assertNotIn(column, columns, f"dedup left duplicate person column {column!r} on positions")

    def test_positions_keep_location_values(self):
        store = self._store()
        try:
            rows = store.filter_only_rows_for_namespace(
                "people",
                None,
                ["city", "state", "country", "macro_region", "metro_areas"],
            )
        finally:
            store.conn.close()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.get("city"), "Palo Alto")
        self.assertEqual(row.get("state"), "California")
        self.assertEqual(row.get("country"), "United States")
        self.assertEqual(row.get("macro_region"), "North America")
        self.assertEqual(row.get("metro_areas"), ["San Francisco Bay Area"])

    def test_mixed_city_metro_or_filter_matches_on_positions(self):
        # Prod-shape location filter: Or(city In [...], metro_areas ContainsAny [...]).
        # A Palo Alto person must match an SF Bay Area query through metro_areas.
        filters = ["Or", [["city", "In", ["San Francisco"]], ["metro_areas", "ContainsAny", ["San Francisco Bay Area"]]]]
        store = self._store()
        try:
            rows = store.filter_only_rows_for_namespace("people", filters, ["base_id"])
        finally:
            store.conn.close()
        self.assertEqual([row.get("base_id") for row in rows], ["p1"])

    def test_missing_filter_column_warns_on_stderr(self):
        filters = ["nonexistent_filter_column_for_test", "In", ["x"]]
        store = self._store()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                rows = store.filter_only_rows_for_namespace("people", filters, ["base_id"])
        finally:
            store.conn.close()
        self.assertEqual(rows, [])
        self.assertIn("nonexistent_filter_column_for_test", stderr.getvalue())
        self.assertIn("WARNING", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
