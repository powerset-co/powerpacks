import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / "packs/search/primitives"
for _path in [PRIMITIVES / "lib", PRIMITIVES / "shared", PRIMITIVES / "local", PRIMITIVES / "turbopuffer"]:
    sys.path.insert(0, str(_path))

from search_result_merge import merge_agentic_sql_candidates  # noqa: E402


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


execute_role_search = load_module(
    "execute_role_search_fanin", PRIMITIVES / "execute_role_search" / "execute_role_search.py"
)
local_pipeline = load_module(
    "local_search_pipeline_fanin", PRIMITIVES / "local_search_pipeline" / "local_search_pipeline.py"
)


def write_positions_db(path: Path) -> None:
    import duckdb

    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE local_people_positions (id VARCHAR, person_id VARCHAR, base_id VARCHAR, "
        "position_title VARCHAR, seniority_band VARCHAR, is_current BOOLEAN)"
    )
    rows = [
        [f"p{i}-pos", f"p{i}", f"p{i}", title, band, True]
        for i, (title, band) in enumerate(
            [("Engineer", "senior"), ("Engineer", "senior"), ("PM", "manager"), ("Designer", "mid"), ("Analyst", "mid")]
        )
    ]
    rows.append(["p0-pos2", "p0", "p0", "Staff Engineer", "staff", False])
    conn.executemany("INSERT INTO local_people_positions VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.close()


class MergeAgenticSqlCandidatesTests(unittest.TestCase):
    def test_new_person_appended_with_tag_evidence_and_rank(self):
        existing = [{"person_id": "p-main", "vertical_sources": ["hybrid"]}]
        merged = merge_agentic_sql_candidates(
            existing,
            [{"person_id": "p-sql", "evidence": "overlapped at InfraDB 2019-2021", "company_name": "InfraDB"}],
            limit=0,
        )
        self.assertEqual([c["person_id"] for c in merged], ["p-main", "p-sql"])
        sql_candidate = merged[1]
        self.assertEqual(sql_candidate["vertical_sources"], ["agentic_sql"])
        self.assertEqual(sql_candidate["agentic_sql_evidence"], "overlapped at InfraDB 2019-2021")
        self.assertEqual(sql_candidate["agentic_sql_rank"], 1)
        self.assertEqual(sql_candidate["company_name"], "InfraDB")

    def test_existing_person_gains_tag_without_duplicate(self):
        existing = [{"person_id": "p-main", "vertical_sources": ["hybrid"]}]
        merged = merge_agentic_sql_candidates(
            existing,
            [{"person_id": "p-main", "evidence": "3 stints"}, {"person_id": "p-main"}],
            limit=0,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["vertical_sources"], ["hybrid", "agentic_sql"])
        self.assertEqual(merged[0]["agentic_sql_evidence"], "3 stints")
        self.assertEqual(merged[0]["agentic_sql_rank"], 1)

    def test_bare_id_list_and_empty_input(self):
        merged = merge_agentic_sql_candidates([], ["p-sql"], limit=0)
        self.assertEqual(merged[0]["person_id"], "p-sql")
        self.assertEqual(merged[0]["vertical_sources"], ["agentic_sql"])
        untouched = [{"person_id": "p-main"}]
        self.assertEqual(merge_agentic_sql_candidates(untouched, [], limit=0), untouched)

    def test_limit_respected(self):
        merged = merge_agentic_sql_candidates(
            [{"person_id": "p-main"}],
            [{"person_id": "p-a"}, {"person_id": "p-b"}],
            limit=2,
        )
        self.assertEqual([c["person_id"] for c in merged], ["p-main", "p-a"])


class FilteredPeopleCountTests(unittest.TestCase):
    def setUp(self):
        from local_duckdb_store import LocalDuckDBSearchStore
        from search_common import filters_from_role_payload

        self.filters_from_role_payload = filters_from_role_payload
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "pool.duckdb"
        write_positions_db(self.db_path)
        self.store = LocalDuckDBSearchStore(str(self.db_path))

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def test_counts_distinct_people_under_filters(self):
        filters = self.filters_from_role_payload({"seniority_bands": ["senior"]})
        counts = self.store.filtered_people_count(filters)
        self.assertEqual(counts["matched_people"], 2)
        self.assertEqual(counts["total_people"], 5)

    def test_no_filters_counts_whole_index(self):
        counts = self.store.filtered_people_count(self.filters_from_role_payload({}))
        self.assertEqual(counts["matched_people"], 5)
        self.assertEqual(counts["total_people"], 5)


class CompactPreviewPoolEstimateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "pool.duckdb"
        write_positions_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def preview(self, filters: dict) -> dict:
        payload = {"normalized_query": "q", "role_search_filters": filters}
        return local_pipeline.compact_preview(payload, Path(self.tmp.name) / "payload.json", self.db_path, [])

    def test_narrow_search_reports_pool_without_breadth_note(self):
        preview = self.preview({"seniority_bands": ["senior"]})
        self.assertEqual(preview["pool_estimate"]["matched_people"], 2)
        self.assertEqual(preview["pool_estimate"]["total_people"], 5)
        self.assertFalse(any("broad search" in note for note in preview["runtime_notes"]))

    def test_broad_search_adds_breadth_note(self):
        preview = self.preview({})
        self.assertEqual(preview["pool_estimate"]["matched_people"], 5)
        self.assertTrue(any("broad search" in note for note in preview["runtime_notes"]))

    def test_zero_match_adds_zero_note(self):
        preview = self.preview({"seniority_bands": ["nonexistent_band"]})
        self.assertEqual(preview["pool_estimate"]["matched_people"], 0)
        self.assertTrue(any("match 0 people" in note for note in preview["runtime_notes"]))

    def test_missing_db_skips_estimate(self):
        payload = {"normalized_query": "q", "role_search_filters": {}}
        preview = local_pipeline.compact_preview(payload, Path(self.tmp.name) / "p.json", Path(self.tmp.name) / "absent.duckdb", [])
        self.assertEqual(preview["pool_estimate"]["status"], "skipped_no_db")


class StubBackend:
    async def hybrid_role_rows(self, payload, filters, *, top_k, include_attributes):
        return [
            {
                "person_id": "p-main",
                "id": "p-main-pos",
                "position_id": "p-main-pos",
                "position_title": "Engineer",
                "retrieval_mode": "hybrid",
                "score": 1.0,
            }
        ]

    def namespace_name(self, logical_name):
        return logical_name


class ExecuteRoleSearchExtraCandidatesTests(unittest.TestCase):
    def setUp(self):
        self._orig = (
            execute_role_search.search_backend,
            execute_role_search.local_summary_rows,
            execute_role_search.local_company_signal_rows,
        )

        async def no_rows(payload, filters, *, top_k, include_attributes):
            return []

        execute_role_search.search_backend = lambda: StubBackend()
        execute_role_search.local_summary_rows = no_rows
        execute_role_search.local_company_signal_rows = no_rows

    def tearDown(self):
        (
            execute_role_search.search_backend,
            execute_role_search.local_summary_rows,
            execute_role_search.local_company_signal_rows,
        ) = self._orig

    def run_search(self, extra_candidates_json: str | None) -> dict:
        args = Namespace(
            state=None,
            payload_json=json.dumps({"role_ids": ["engineer"], "is_current_role": True}),
            env_file=None,
            write_state=False,
            write_artifact=False,
            limit=0,
            top_k=10,
            extra_candidates_json=extra_candidates_json,
        )
        return asyncio.run(execute_role_search.run(args))

    def test_extra_candidates_flow_into_candidate_ids_for_hydration(self):
        with tempfile.TemporaryDirectory() as tmp:
            extra_path = Path(tmp) / "agentic-sql-candidates.json"
            extra_path.write_text(json.dumps({
                "vertical": "agentic_sql",
                "people": [
                    {"person_id": "p-sql", "evidence": "2+ stints at seed companies"},
                    {"person_id": "p-main", "evidence": "also found by sql"},
                ],
            }))
            output = self.run_search(str(extra_path))

        self.assertEqual(output["agentic_sql_candidate_count"], 2)
        self.assertEqual(output["agentic_sql_tagged"], 2)
        self.assertEqual(set(output["candidate_ids"]), {"p-main", "p-sql"})
        by_person = {c["person_id"]: c for c in output["candidates"]}
        self.assertIn("agentic_sql", by_person["p-main"]["vertical_sources"])
        self.assertEqual(by_person["p-sql"]["vertical_sources"], ["agentic_sql"])
        self.assertEqual(by_person["p-sql"]["agentic_sql_evidence"], "2+ stints at seed companies")

    def test_without_flag_output_reports_zero_sql_candidates(self):
        output = self.run_search(None)
        self.assertEqual(output["agentic_sql_candidate_count"], 0)
        self.assertEqual(output["agentic_sql_tagged"], 0)
        self.assertEqual(output["candidate_ids"], ["p-main"])


if __name__ == "__main__":
    unittest.main()
