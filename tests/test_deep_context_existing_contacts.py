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
    ArtifactRow,
    FactRow,
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
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import _imported_people, project_imported_people
from packs.ingestion.primitives.imports.merge_people import PeopleMerge
from packs.ingestion.primitives.pipeline.contract import PeopleRow
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


class PreservationTests(unittest.TestCase):
    """What the keep rule and the repair must leave alone (PR #722 review, Oct 6)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.jordan = PeopleRow(id="old-jordan", full_name="Jordan Bravo", primary_email="jordan@example.test",
                                primary_phone="+15550100101", source_channels="gmail_msgvault")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def project(self, rows: list[PeopleRow]) -> None:
        project_imported_people(self.db, _imported_people(tuple(rows)))

    def ensure(self, rows: list[PeopleRow]) -> None:
        inputs = []
        for index, row in enumerate(rows):
            path = self.root / f"source-{index}" / "people.csv"
            path.parent.mkdir()
            CsvIO.write_dict_rows(path, PEOPLE_SCHEMA_COLUMNS, [row.to_row()])
            inputs.append(path)
        merge = PeopleMerge(inputs=inputs, output_dir=self.root / "merged")
        merge.run()
        EnsureParents(db=self.db, people_csv=merge.people_csv).run()

    def test_two_people_sharing_a_phone_stay_two_parents(self) -> None:
        casey = self.jordan.model_copy(update={"id": "old-casey", "full_name": "Casey Delta",
                                               "primary_email": "casey@example.test"})
        self.project([self.jordan, casey])
        for person in queries.people(self.db):
            key = f"facts:{person.person_id}"
            self.db.project_rows((
                ArtifactRow(key, "facts", person.parent_id, str(self.root / key), "sha", "projected"),
                FactRow(person.parent_id, person.parent_id, key, machine_worth="yes",
                        facts_json=json.dumps({"canonical_name": person.display_name})),
            ))
        before = {row.person_id: row.parent_id for row in queries.people(self.db)}
        self.ensure([self.jordan, casey])
        self.assertEqual({row.person_id: row.parent_id for row in queries.people(self.db)}, before)
        self.assertEqual(len(queries.parents(self.db)), 2)

    def test_a_new_contact_sharing_a_phone_is_still_imported(self) -> None:
        casey = PeopleRow(id="candidate:phone:+15550100101", full_name="Casey Delta",
                          primary_phone="+15550100101", source_channels="imessage")
        self.project([self.jordan])
        self.ensure([self.jordan, casey])
        people = {row.person_id: row.parent_id for row in queries.people(self.db)}
        self.assertEqual(set(people), {"old-jordan", casey.id})
        self.assertNotEqual(people["old-jordan"], people[casey.id])
        self.assertEqual({row.normalized_value for row in queries.identifiers(self.db, person_id="old-jordan")},
                         {"jordan@example.test", "+15550100101"})

    def test_a_second_parent_holding_a_profile_and_a_verdict_is_merged_not_deleted(self) -> None:
        shell = self.jordan.model_copy(update={"id": "candidate:email:jordan@example.test", "primary_phone": "",
                                               "public_identifier": "jordan-bravo",
                                               "linkedin_url": "https://www.linkedin.com/in/jordan-bravo"})
        self.project([self.jordan, shell])
        parent = next(row.parent_id for row in queries.people(self.db) if row.person_id == shell.id)
        self.db.project_rows((
            LinkRow("jordan-bravo", parent, "jordan-bravo", "pub", shell.linkedin_url,
                    source="deep-context-reconcile", machine_action="verify", machine_approved="auto",
                    machine_judgment="confirmed", judgment_fingerprint="paid-fingerprint",
                    judgment_payload_json='{"verdict":"confirmed","confidence":0.99}', paid_profile=True),
            ArtifactRow("profile:paid", "profile", parent, str(self.root / "profile.json"), "saved", "projected",
                        candidate_key="jordan-bravo", payload_json='{"experience":[{"title":"Engineer"}]}'),
        ))
        self.assertTrue(queries.parent_has_paid_work(self.db, parent))
        self.ensure([self.jordan])
        self.assertIn("profile:paid", [row.artifact_key for row in queries.artifacts(self.db)])
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM links WHERE judgment_fingerprint='paid-fingerprint'")[0]["n"], 1)
        self.assertEqual(len(queries.parents(self.db)), 1)
        self.assertEqual({row.parent_id for row in queries.people(self.db)}, {queries.parents(self.db)[0].parent_id})


if __name__ == "__main__":
    unittest.main()
