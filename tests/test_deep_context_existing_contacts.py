"""An existing contact keeps its person across a re-import; split contacts fold back.

Synthetic data only. The fan-in's Gmail row carries the pre-3.17 uuid id; the
source reader would re-key it to `candidate:email:<address>`.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.models import (
    LinkRow,
    ParentRow,
    PersonIdentifierRow,
    PersonIdentifiersProjection,
    PersonRow,
    PersonSourceRow,
    PersonSourcesProjection,
    RowKind,
    WriterSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
from packs.ingestion.primitives.imports.merge_people import PeopleMerge
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.shared.csv_io import CsvIO


UUID_ID = "0f3c2a1e-7b9d-5e4a-8c6f-1d2e3f4a5b6c"
EMAIL = "jordan@example.com"
SECOND_EMAIL = "jordan.b@example.org"
CANDIDATE_ID = f"candidate:email:{EMAIL}"


class ExistingContactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.gmail = self.root / "gmail" / "people.csv"
        self.gmail.parent.mkdir()
        self.merge = PeopleMerge(inputs=[self.gmail], output_dir=self.root / "merged")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def gmail_row(self) -> dict[str, str]:
        return {
            "id": UUID_ID, "full_name": "Jordan Bravo", "primary_email": EMAIL,
            "all_emails": json.dumps([EMAIL, SECOND_EMAIL]), "source_channels": "gmail_msgvault",
            "source_artifacts": '["gmail-account/people.csv"]',
        }

    def seed_uuid_contact(self) -> str:
        """Project the contact the pre-3.17 way: a legacy people.csv without a fan-in manifest."""
        legacy = self.root / "legacy" / "people.csv"
        legacy.parent.mkdir()
        CsvIO.write_dict_rows(legacy, PEOPLE_SCHEMA_COLUMNS, [self.gmail_row()])
        EnsureParents(db=self.db, people_csv=legacy).run()
        people = queries.people(self.db)
        self.assertEqual([row.person_id for row in people], [UUID_ID])
        return people[0].parent_id

    def fan_in(self) -> None:
        CsvIO.write_dict_rows(self.gmail, PEOPLE_SCHEMA_COLUMNS, [self.gmail_row()])
        self.merge.run()

    def identifiers(self, person_id: str) -> set[str]:
        return {row.normalized_value for row in queries.identifiers(self.db, person_id=person_id)}

    def test_re_import_keeps_the_existing_contact_and_its_parent(self) -> None:
        parent_id = self.seed_uuid_contact()
        self.fan_in()
        for _ in range(2):
            EnsureParents(db=self.db, people_csv=self.merge.people_csv).run()
            self.assertEqual({row.person_id: row.parent_id for row in queries.people(self.db)}, {UUID_ID: parent_id})
            self.assertEqual(len(queries.parents(self.db)), 1)
            self.assertEqual(self.identifiers(UUID_ID), {EMAIL, SECOND_EMAIL})
            self.assertEqual([row.id for row in queries.imported_people(self.db)], [UUID_ID])
            self.assertEqual(queries.people(self.db)[0].display_name, "Jordan Bravo")

    def split(self, *, paid: bool) -> str:
        """Reproduce a store an earlier run split: the address on a shell, the first person stripped."""
        parent_id = self.seed_uuid_contact()
        shell = "parent-shell"
        self.db.project_rows((
            ParentRow(shell, f"parent-worth:{shell}", "", "jordan-shell", source=WriterSource.PARENT_WORTH.value,
                      updated_at=now_iso()),
            PersonRow(CANDIDATE_ID, shell, "jordan-shell-child", "jordan-shell", "", False, False, None, None, now_iso()),
            PersonIdentifiersProjection(CANDIDATE_ID, (PersonIdentifierRow(CANDIDATE_ID, "email", EMAIL, EMAIL),)),
            PersonSourcesProjection(CANDIDATE_ID, (PersonSourceRow(CANDIDATE_ID, "gmail_msgvault"),)),
            LinkRow(CANDIDATE_ID, shell, "", RowKind.CANDIDATE_EMAIL.value, display_name="", candidate_origin=True,
                    raw_import=True, source=WriterSource.RECONCILE.value, updated_at=now_iso()),
            PersonIdentifiersProjection(UUID_ID, ()),
        ))
        if paid:
            with self.db.transaction() as conn:
                conn.execute("UPDATE parents SET human_worth='yes', human_worth_at=? WHERE parent_id=?",
                             (now_iso(), shell))
        self.assertEqual(len(queries.parents(self.db)), 2)
        self.assertEqual(self.identifiers(UUID_ID), set())
        return parent_id

    def test_empty_shell_is_deleted_and_the_contact_restored(self) -> None:
        parent_id = self.split(paid=False)
        self.fan_in()
        EnsureParents(db=self.db, people_csv=self.merge.people_csv).run()
        self.assertEqual({row.person_id: row.parent_id for row in queries.people(self.db)}, {UUID_ID: parent_id})
        self.assertEqual(len(queries.parents(self.db)), 1)
        self.assertEqual(self.identifiers(UUID_ID), {EMAIL, SECOND_EMAIL})
        self.assertEqual([row.id for row in queries.imported_people(self.db)], [UUID_ID])
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM links")[0]["n"], 0)

    def test_shell_with_paid_work_is_merged_into_the_contacts_parent(self) -> None:
        parent_id = self.split(paid=True)
        self.fan_in()
        EnsureParents(db=self.db, people_csv=self.merge.people_csv).run()
        self.assertEqual({row.parent_id for row in queries.people(self.db)}, {parent_id})
        self.assertEqual({row.person_id for row in queries.people(self.db)}, {UUID_ID, CANDIDATE_ID})
        self.assertEqual(len(queries.parents(self.db)), 1)
        self.assertEqual(queries.parents(self.db)[0].human_worth, "yes")
        self.assertEqual(self.identifiers(UUID_ID), {EMAIL, SECOND_EMAIL})


if __name__ == "__main__":
    unittest.main()
