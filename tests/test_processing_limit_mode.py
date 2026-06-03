import csv
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from packs.indexing.primitives.build_processing_pipeline.build_processing_pipeline import select_people_for_run


class ProcessingLimitModeTest(unittest.TestCase):
    def test_missing_limit_selects_missing_people_after_baseline_filter(self) -> None:
        people = [
            {"id": "old-1", "linkedin_url": "https://www.linkedin.com/in/old-one"},
            {"id": "new-1", "linkedin_url": "https://www.linkedin.com/in/new-one"},
            {"id": "new-2", "public_identifier": "new-two"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "baseline.csv"
            with baseline.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["id", "linkedin_url", "public_identifier"])
                writer.writeheader()
                writer.writerow({"id": "old-1", "linkedin_url": "https://linkedin.com/in/old-one/", "public_identifier": "old-one"})

            selected, stats = select_people_for_run(
                people,
                Namespace(limit=1, limit_mode="missing", existing_people_csv=str(baseline), existing_duckdb=""),
            )

        self.assertEqual([row["id"] for row in selected], ["new-1"])
        self.assertEqual(stats["input_people"], 3)
        self.assertEqual(stats["missing_people"], 2)
        self.assertEqual(stats["reused_or_existing_people"], 1)
        self.assertEqual(stats["selected_people"], 1)

    def test_all_limit_preserves_existing_behavior(self) -> None:
        people = [{"id": "old-1"}, {"id": "new-1"}]
        selected, stats = select_people_for_run(people, Namespace(limit=1, limit_mode="all"))
        self.assertEqual([row["id"] for row in selected], ["old-1"])
        self.assertEqual(stats["mode"], "all")
        self.assertEqual(stats["selected_people"], 1)


if __name__ == "__main__":
    unittest.main()
