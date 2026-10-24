"""The index's people file: realize's families, then the LinkedIn connections no family holds."""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.realize.realize import (INDEX_PEOPLE_RELATIVE_PATH,
                                                                        LINKEDIN_IMPORT_RELATIVE_PATH,
                                                                        PEOPLE_CSV_RELATIVE_PATH, with_connections)


class WithConnectionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.write(PEOPLE_CSV_RELATIVE_PATH, "id,full_name,public_identifier,worth\nf1,Jordan Bravo,jordan-bravo,yes\nf2,No Handle,,maybe\n")

    def write(self, relative: Path, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def rows(self, path: Path) -> list[dict]:
        with path.open(encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_families_first_then_the_connections_no_family_holds(self) -> None:
        self.write(LINKEDIN_IMPORT_RELATIVE_PATH, "id,full_name,public_identifier,headline\nl1,Jordan Bravo,jordan-bravo,CTO\nl2,Sam Li,sam-li,Founder\nl3,,,\n")
        indexed, families, added = with_connections(self.root)
        self.assertEqual((indexed, families, added), (self.root / INDEX_PEOPLE_RELATIVE_PATH, 2, 1))
        rows = self.rows(indexed)
        self.assertEqual([row["public_identifier"] for row in rows], ["jordan-bravo", "", "sam-li"])
        # The family's row wins; the connection's columns are added, blank where a family has none.
        self.assertEqual((rows[0]["worth"], rows[0]["headline"]), ("yes", ""))
        self.assertEqual((rows[2]["worth"], rows[2]["headline"]), ("", "Founder"))

    def test_without_a_linkedin_import_the_file_is_the_families(self) -> None:
        indexed, families, added = with_connections(self.root)
        self.assertEqual((families, added), (2, 0))
        self.assertEqual(len(self.rows(indexed)), 2)


if __name__ == "__main__":
    unittest.main()
