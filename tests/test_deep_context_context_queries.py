"""Evidence reads must not scale their bound variables with the install size.

Created: 2026-09-25
Changelog:
- 2026-09-25: created for the cluster survey on a 27k-parent install, which
  bound every parent id twice and hit SQLite's variable limit.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.context_queries import dossier_evidence_rows
from packs.ingestion.primitives.deep_context.db.models import ParentRow, PersonRow
from packs.ingestion.primitives.deep_context.db.store import Db

# Above SQLITE_MAX_VARIABLE_NUMBER (32,766) once each id is bound twice.
PARENTS = 17_000


class ContextQueriesTests(unittest.TestCase):
    def test_evidence_rows_for_every_parent_stay_under_the_variable_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Db(Path(directory) / "deep-context.sqlite")
            ids = [f"parent-{index:012x}" for index in range(PARENTS)]
            db.project_rows(tuple(ParentRow(parent_id, parent_id) for parent_id in ids))
            db.project_rows(tuple(PersonRow(f"person-{parent_id}", parent_id) for parent_id in ids))

            rows = dossier_evidence_rows(db, tuple(ids))

            self.assertEqual(len(rows.parents), PARENTS)
            self.assertEqual(len(rows.people), PARENTS)
            # Case-insensitive lookup and the empty request keep their contracts.
            self.assertEqual(len(dossier_evidence_rows(db, (ids[0].upper(),)).parents), 1)
            self.assertEqual(dossier_evidence_rows(db, ()).parents, ())


if __name__ == "__main__":
    unittest.main()
