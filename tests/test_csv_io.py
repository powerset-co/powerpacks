"""Regression coverage for packs.shared.csv_io.CsvIO.

Proves the central reader lifts Python's default 131072-byte csv field limit so
rows carrying large embedded JSON (rapidapi_response, work_experiences, ...) no
longer raise ``_csv.Error: field larger than field limit``.
"""
import csv
import io
import unittest

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


if __name__ == "__main__":
    unittest.main()
