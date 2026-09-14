"""One-time compatibility for pre-SQLite synthetic CSV rows."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.enrich.parallel_research.result import ResearchResult
from packs.ingestion.primitives.deep_context.migration.legacy import _Graph, _synthetic


class LegacySyntheticMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "synthetic-people.csv"
        self.parent_id = "parent-a1b2c3d4e5f6"
        self.graph = _Graph(
            review={},
            aliases={},
            parents={
                self.parent_id: ParentRow(
                    self.parent_id,
                    f"parent-worth:{self.parent_id}",
                    "Jordan Bravo",
                    "jordan-bravo",
                ),
            },
            people={"person-a": PersonRow("person-a", self.parent_id)},
            slug_parent={"jordan-bravo": self.parent_id},
            identifiers={},
            sources={},
            facts=[],
            indexed_people={"person-a"},
            person_parent={"person-a": self.parent_id},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_row(self, *, approved: str = "") -> None:
        row = {
            "id": "person-a",
            "public_identifier": "synth-email-1af7a7e7773a",
            "full_name": "Jordan Bravo",
            "headline": "Founder",
            "summary": "Founder",
            "city": "Oakland",
            "country": "US",
            "work_experiences": json.dumps([
                {"title": "Founder", "company_name": "Example Labs", "is_current": True},
            ]),
            "education": "[]",
            "source_parent_slug": "jordan-bravo",
            "source_person_ids": json.dumps(["person-a"]),
            "approved": approved,
        }
        with self.path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)

    def test_import_rekeys_and_converts_to_native_result(self) -> None:
        self.write_row()

        _synthetic(self.graph, self.path)

        self.assertEqual(set(self.graph.links), {self.parent_id})
        self.assertEqual(self.graph.synthetics[0].candidate_key, self.parent_id)
        result = ResearchResult.from_json(self.graph.synthetics[0].profile_json)
        self.assertIsNotNone(result)
        self.assertEqual(result.person.full_name, "Jordan Bravo")
        self.assertEqual(result.positions[0].company_name, "Example Labs")

    def test_import_preserves_human_decision_under_parent_key(self) -> None:
        self.write_row(approved="yes")

        _synthetic(self.graph, self.path)

        action, approved, *_ = self.graph.human_links[self.parent_id]
        self.assertEqual((action, approved), ("verify", "yes"))


if __name__ == "__main__":
    unittest.main()
