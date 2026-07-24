"""Tests for the shared newline-delimited JSON pair in common/jsonio.py.

The message extractors route their JSONL through `write_jsonl`/`read_jsonl`, so
this locks the contract they depend on: LF-terminated lines, key-sorted and
ASCII-escaped by default, blank lines skipped on read, and a missing file
reading as empty rather than raising.
"""

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import read_jsonl, write_jsonl


ROWS = [
    {"phone": "+15550100", "name": "Jordan Bravo", "count": 2},
    {"phone": "+15550101", "name": "Casey Delgado", "email": "casey@example.com", "count": None},
]


class JsonlRoundTripTest(unittest.TestCase):
    def test_round_trip_preserves_rows_and_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "contacts.jsonl"
            self.assertEqual(write_jsonl(path, ROWS), 2)
            self.assertEqual(read_jsonl(path), ROWS)

    def test_creates_missing_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "deep" / "nested" / "contacts.jsonl"
            write_jsonl(path, ROWS)
            self.assertTrue(path.exists())
            self.assertEqual(read_jsonl(path), ROWS)

    def test_accepts_a_generator_and_returns_the_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "contacts.jsonl"
            self.assertEqual(write_jsonl(path, (row for row in ROWS)), 2)
            self.assertEqual(len(read_jsonl(path)), 2)

    def test_lines_are_lf_terminated_and_key_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "contacts.jsonl"
            write_jsonl(path, [{"z": 1, "a": 2}])
            self.assertEqual(path.read_bytes(), b'{"a": 2, "z": 1}\n')

    def test_sort_keys_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "contacts.jsonl"
            write_jsonl(path, [{"z": 1, "a": 2}], sort_keys=False)
            self.assertEqual(path.read_bytes(), b'{"z": 1, "a": 2}\n')

    def test_overwrites_rather_than_appends(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "contacts.jsonl"
            write_jsonl(path, ROWS)
            self.assertEqual(write_jsonl(path, ROWS[:1]), 1)
            self.assertEqual(read_jsonl(path), ROWS[:1])


class JsonlEmptyAndBlankLineTest(unittest.TestCase):
    def test_writing_no_rows_leaves_an_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "contacts.jsonl"
            self.assertEqual(write_jsonl(path, []), 0)
            self.assertEqual(path.read_bytes(), b"")
            self.assertEqual(read_jsonl(path), [])

    def test_missing_file_reads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read_jsonl(Path(td) / "never-written.jsonl"), [])

    def test_trailing_newline_does_not_produce_a_phantom_row(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "contacts.jsonl"
            path.write_text('{"a": 1}\n', encoding="utf-8")
            self.assertEqual(read_jsonl(path), [{"a": 1}])

    def test_reads_a_final_line_with_no_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "contacts.jsonl"
            path.write_text('{"a": 1}\n{"b": 2}', encoding="utf-8")
            self.assertEqual(read_jsonl(path), [{"a": 1}, {"b": 2}])

    def test_blank_and_whitespace_lines_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "contacts.jsonl"
            path.write_text('\n{"a": 1}\n   \n\n{"b": 2}\n\n', encoding="utf-8")
            self.assertEqual(read_jsonl(path), [{"a": 1}, {"b": 2}])

    def test_malformed_line_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "contacts.jsonl"
            path.write_text('{"a": 1}\nnot json\n', encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                read_jsonl(path)


class JsonlNonAsciiTest(unittest.TestCase):
    ROW = {"name": "Renée Müller-Solé", "note": "café ☕ 東京"}

    def test_non_ascii_is_escaped_by_default_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "contacts.jsonl"
            write_jsonl(path, [self.ROW])
            raw = path.read_bytes()
            self.assertNotIn("é".encode(), raw)
            self.assertIn(b"\\u00e9", raw)
            self.assertEqual(read_jsonl(path), [self.ROW])

    def test_ensure_ascii_false_writes_literal_utf8_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "contacts.jsonl"
            write_jsonl(path, [self.ROW], ensure_ascii=False)
            raw = path.read_bytes()
            self.assertIn("Renée Müller-Solé".encode(), raw)
            self.assertNotIn(b"\\u00e9", raw)
            self.assertEqual(read_jsonl(path), [self.ROW])

    def test_file_is_read_as_utf8_regardless_of_locale(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "contacts.jsonl"
            path.write_bytes(json.dumps(self.ROW, ensure_ascii=False).encode("utf-8") + b"\n")
            self.assertEqual(read_jsonl(path), [self.ROW])


if __name__ == "__main__":
    unittest.main()
