import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

ROOT = Path(__file__).resolve().parents[1]
PRIMITIVES = ROOT / "packs/search/primitives"
for _path in [PRIMITIVES / "lib", PRIMITIVES / "shared", PRIMITIVES / "local", PRIMITIVES / "turbopuffer"]:
    sys.path.insert(0, str(_path))

import local_search_backend as backend  # noqa: E402
from search_common import filters_from_role_payload  # noqa: E402


def build_db(path: Path, *, people: int = 200) -> None:
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE local_people_positions (id VARCHAR, person_id VARCHAR, base_id VARCHAR, "
        "position_title VARCHAR, company_id VARCHAR, is_current BOOLEAN, seniority_band VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO local_people_positions VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            [f"p{i}-pos", f"p{i}", f"p{i}", "Engineer", f"c{i % 40}", True, "senior"]
            for i in range(people)
        ],
    )
    conn.close()


class LocalStoreForkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "concurrency.duckdb"
        build_db(self.db_path)
        backend._local_store_for_path.cache_clear()

    def tearDown(self):
        backend._local_store_for_path.cache_clear()
        self.tmp.cleanup()

    def test_local_store_returns_per_call_forks_of_one_root(self):
        root = backend._local_store_for_path(str(self.db_path))
        with mock.patch.object(root, "fork", wraps=root.fork) as fork:
            first = backend.local_store(self.db_path)
            second = backend.local_store(self.db_path)
        try:
            self.assertEqual(fork.call_count, 2)
            self.assertIsNot(first, second)
            self.assertEqual(first.db_path, second.db_path)
            self.assertEqual(first.namespace_row_count("people"), 200)
            self.assertEqual(second.namespace_row_count("people"), 200)
        finally:
            first.close()
            second.close()

    def test_concurrent_company_chunk_filters_do_not_corrupt_catalog_reads(self):
        """Regression: chunked company prefilters fan out via asyncio.to_thread.

        Before the cursor-fork fix every call executed on one shared cached
        connection, which under concurrency made PRAGMA table_info come back
        empty and _table_exists report existing tables as missing
        (LocalDuckDBError: namespace 'people' requires missing table ...).
        """

        async def one_chunk(chunk_index: int):
            payload = {
                "company_ids": [f"c{(chunk_index * 7 + offset) % 40}" for offset in range(5)],
                "is_current_role": True,
            }
            filters = filters_from_role_payload(payload)
            return await backend.filter_only_rows_for_namespace(
                self.db_path, "people", filters, ["base_id", "position_title", "company_id"], max_results=0
            )

        async def fan_out():
            return await asyncio.gather(*(one_chunk(i) for i in range(64)))

        chunk_rows = asyncio.run(fan_out())
        self.assertEqual(len(chunk_rows), 64)
        for rows in chunk_rows:
            self.assertGreater(len(rows), 0, "chunk unexpectedly matched no rows — catalog read corrupted")
            for row in rows:
                self.assertTrue(row.get("base_id"))


if __name__ == "__main__":
    unittest.main()
