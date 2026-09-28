import csv
import tempfile
import unittest
from pathlib import Path

from packs.search.primitives.persist_search_results import results_io


class ResultsLastInteractionTests(unittest.TestCase):
    def test_exports_preserve_existing_interaction_date_and_unknowns(self):
        profiles = [
            {"person_id": "dated", "last_interaction": "2026-09-02T12:00:00+00:00"},
            {"person_id": "missing"},
            {"person_id": "null", "last_interaction": None},
        ]
        state = {"steps": [{"id": "hydrate_people", "output": {"profiles": profiles}}]}
        with tempfile.TemporaryDirectory() as tmp:
            rows = results_io.result_rows(state)
            jsonl_path = Path(tmp) / "results.jsonl"
            csv_path = Path(tmp) / "results.csv"
            results_io.write_jsonl(jsonl_path, rows)
            results_io.write_csv(csv_path, rows)
            jsonl_rows = results_io.read_jsonl(jsonl_path)
            with csv_path.open(newline="") as handle:
                csv_rows = list(csv.DictReader(handle))

        self.assertEqual(
            [row["last_interaction"] for row in jsonl_rows],
            ["2026-09-02T12:00:00+00:00", None, None],
        )
        self.assertEqual(
            [row["last_interaction"] for row in csv_rows],
            ["2026-09-02T12:00:00+00:00", "", ""],
        )
