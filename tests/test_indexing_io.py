import tempfile
import unittest
from pathlib import Path

from packs.indexing.lib.io import iter_csv_rows, read_jsonl, write_jsonl


class IndexingIOTests(unittest.TestCase):
    def test_iter_csv_rows_handles_large_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "people.csv"
            large = "x" * (1024 * 1024)
            path.write_text(f"id,summary\n1,{large}\n", encoding="utf-8")
            rows = list(iter_csv_rows(path))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["summary"], large)

    def test_write_jsonl_accepts_generator_without_materializing_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rows.jsonl"
            consumed = []

            def records():
                for idx in range(3):
                    consumed.append(idx)
                    yield {"id": idx, "value": f"row-{idx}"}

            write_jsonl(path, records())
            self.assertEqual(consumed, [0, 1, 2])
            self.assertEqual(read_jsonl(path), [{"id": 0, "value": "row-0"}, {"id": 1, "value": "row-1"}, {"id": 2, "value": "row-2"}])


if __name__ == "__main__":
    unittest.main()
