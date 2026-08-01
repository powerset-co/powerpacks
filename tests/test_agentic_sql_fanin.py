import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / "packs/search/primitives"
for _path in [PRIMITIVES / "lib", PRIMITIVES / "shared", PRIMITIVES / "local"]:
    sys.path.insert(0, str(_path))


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


if __name__ == "__main__":
    unittest.main()
