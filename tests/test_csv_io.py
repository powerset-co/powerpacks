"""Regression coverage for packs.shared.csv_io.CsvIO.

Proves the central reader lifts Python's default 131072-byte csv field limit so
rows carrying large embedded JSON (rapidapi_response, work_experiences, ...) no
longer raise ``_csv.Error: field larger than field limit``.
"""
import csv
import io
import tempfile
import unittest
from pathlib import Path

from packs.shared.csv_io import CsvIO


class CsvIOTests(unittest.TestCase):
    def tearDown(self) -> None:
        # Leave the process in the guarded state for any later tests.
        CsvIO._limit_raised = False
        CsvIO.ensure_field_limit()

    @staticmethod
    def _make_csv(big: str) -> str:
        return f"id,payload\r\n1,{big}\r\n"

    def test_raw_csv_fails_but_csvio_reads_large_dict_field(self) -> None:
        csv.field_size_limit(131072)  # stdlib default
        CsvIO._limit_raised = False
        big = "x" * 200_000
        data = self._make_csv(big)

        with self.assertRaises(csv.Error):
            list(csv.reader(io.StringIO(data)))

        rows = list(CsvIO.dict_reader(io.StringIO(data)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"], big)

    def test_csvio_reader_reads_large_field(self) -> None:
        csv.field_size_limit(131072)
        CsvIO._limit_raised = False
        big = "y" * 200_000
        rows = list(CsvIO.reader(io.StringIO(self._make_csv(big))))
        self.assertEqual(rows[1][1], big)

    def test_ensure_field_limit_raises_above_default(self) -> None:
        csv.field_size_limit(131072)
        CsvIO._limit_raised = False
        CsvIO.ensure_field_limit()
        self.assertGreater(csv.field_size_limit(), 131072)

    def test_write_dict_rows_strict_rejects_extra_keys_and_fills_missing_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nested" / "strict.csv"
            CsvIO.write_dict_rows_strict(path, ["id", "name"], [{"id": "1"}])
            self.assertEqual(path.read_bytes(), b"id,name\r\n1,\r\n")

            with self.assertRaises(ValueError):
                CsvIO.write_dict_rows_strict(
                    Path(td) / "extra.csv",
                    ["id"],
                    [{"id": "1", "unexpected": "value"}],
                )

    def test_upsert_dict_rows_merges_normalized_keys_and_preserves_keyless_rows(self) -> None:
        fieldnames = ["email", "name", "note", "added_at"]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "people.csv"
            CsvIO.write_dict_rows_strict(path, fieldnames, [
                {
                    "email": " Person@Example.com ",
                    "name": "Old Name",
                    "note": "keep me",
                    "added_at": "2026-01-01T00:00:00Z",
                },
                {"email": "", "name": "Existing keyless", "note": "", "added_at": ""},
            ])

            counts = CsvIO.upsert_dict_rows(
                path,
                fieldnames,
                [
                    {
                        "email": "person@example.com",
                        "name": "New Name",
                        "note": "",
                        "added_at": "2026-02-01T00:00:00Z",
                    },
                    {"email": "new@example.com", "name": "New Person", "note": "first", "added_at": ""},
                    {"email": " NEW@example.com ", "name": "", "note": "updated", "added_at": ""},
                    {"email": "", "name": "Incoming keyless", "note": "", "added_at": ""},
                ],
                ["email"],
            )

            self.assertEqual(counts, {
                "incoming": 4,
                "existing": 2,
                "written": 4,
                "preserved_existing": 0,
                "upserted": 2,
            })
            rows = CsvIO.read_dict_rows(path)
            self.assertEqual([row["email"].strip().lower() for row in rows[:2]], [
                "new@example.com",
                "person@example.com",
            ])
            self.assertEqual(rows[0]["note"], "updated")
            self.assertEqual(rows[1]["name"], "New Name")
            self.assertEqual(rows[1]["note"], "keep me")
            self.assertEqual(rows[1]["added_at"], "2026-01-01T00:00:00Z")
            self.assertEqual([row["name"] for row in rows[2:]], [
                "Existing keyless",
                "Incoming keyless",
            ])


if __name__ == "__main__":
    unittest.main()
