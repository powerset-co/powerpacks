"""Index input is an exact export of the realized directory, including deletions."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context_v2.db import queries
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.run import realize
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS


class IndexDirectoryTests(unittest.TestCase):
    def test_raw_linkedin_rows_do_not_bypass_the_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            conn = open_store(store_path(root))
            source = dict.fromkeys(PEOPLE_SCHEMA_COLUMNS, "")
            source["linkedin_url"] = "https://www.linkedin.com/in/jordan-bravo"
            queries.upsert_candidates(conn, [("candidate:linkedin:42", "Jordan Bravo", 0,
                json.dumps(source), "now")])
            conn.execute("INSERT INTO candidate_sources VALUES ('candidate:linkedin:42', 'linkedin')")
            conn.execute("INSERT INTO candidate_parent (candidate_id, parent_id, reason, created_at) "
                         "VALUES ('candidate:linkedin:42', 'li:42', 'singleton', 'now')")
            conn.commit()
            raw = root / "network-import/import/linkedin/people.csv"
            raw.parent.mkdir(parents=True)
            raw.write_text("id,full_name,public_identifier\nraw,Missing ID,missing-id\n")
            indexed = realize(root)
            merged = root / "network-import/merged/people.csv"
            self.assertEqual(indexed.read_bytes(), merged.read_bytes())
            with indexed.open() as handle:
                self.assertEqual([r["id"] for r in csv.DictReader(handle)], ["li:42"])
            conn.execute("INSERT INTO deleted_parents VALUES ('li:42', 'now')")
            conn.commit()
            indexed = realize(root)
            self.assertEqual(indexed.read_bytes(), merged.read_bytes())
            with indexed.open() as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM families").fetchone()[0], 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
