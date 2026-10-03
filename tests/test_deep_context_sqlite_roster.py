from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.queries import imported_people, parents
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow, CandidatePeopleProjection, CandidatePersonRow, FactRow, LinkRow,
)
from packs.ingestion.primitives.deep_context.realize.export_people import ExportPeople
from packs.ingestion.primitives.deep_context.db.identity_views import enrichment_queue
from packs.ingestion.primitives.deep_context.db.queries import people as stored_people
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import project_imported_people, read_imported_people
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.shared.csv_io import CsvIO


class SQLiteRosterTest(unittest.TestCase):
    def test_new_linkedin_row_does_not_absorb_old_contact_via_superseded_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            csv = root / "people.csv"
            old_id = "candidate:email:jordan@example.test"
            CsvIO.write_dict_rows(csv, PEOPLE_SCHEMA_COLUMNS, [
                {"id": old_id, "full_name": "Jordan Bravo", "primary_email": "jordan@example.test",
                 "primary_phone": "+15550100", "interaction_counts": '{"gmail":4}'},
            ])
            project_imported_people(db, read_imported_people(csv))
            original_parent = stored_people(db)[0].parent_id
            db.decide_worth(original_parent, "yes")
            CsvIO.write_dict_rows(csv, PEOPLE_SCHEMA_COLUMNS, [
                {"id": "linkedin-person-1", "full_name": "Jordan Bravo",
                 "public_identifier": "jordan-bravo",
                 "superseded_person_ids": f'["{old_id}"]'},
            ])
            project_imported_people(db, read_imported_people(csv))
            roster = imported_people(db)
            self.assertEqual(len(roster), 2)
            by_id = {row.id: row for row in roster}
            self.assertEqual(by_id[old_id].primary_phone, "+15550100")
            self.assertIn("gmail", by_id[old_id].interaction_counts)
            self.assertEqual(by_id["linkedin-person-1"].primary_phone, "")
            self.assertNotEqual(next(row.parent_id for row in stored_people(db)
                                     if row.person_id == "linkedin-person-1"), original_parent)
            self.assertEqual(next(row.human_worth for row in parents(db)
                                  if row.parent_id == original_parent), "yes")

    def test_refresh_of_superseded_candidate_updates_realized_person(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            csv = root / "people.csv"
            old_id = "candidate:email:jordan@example.test"
            CsvIO.write_dict_rows(csv, PEOPLE_SCHEMA_COLUMNS, [
                {"id": old_id, "full_name": "Jordan Bravo", "primary_email": "jordan@example.test"},
            ])
            project_imported_people(db, read_imported_people(csv))
            parent_id = stored_people(db)[0].parent_id
            db.project_rows((LinkRow("research:one", parent_id, "", "research",
                                     paid_profile=True, source="deep-context-reconcile"),))
            db.project_rows((CandidatePeopleProjection("research:one", (CandidatePersonRow("research:one", old_id, parent_id),)),))
            db.decide_identity("research:one", "retarget", approved="yes",
                               replacement_url="https://www.linkedin.com/in/jordan-bravo",
                               replacement_public_identifier="jordan-bravo")
            ExportPeople(db=db, out_dir=root / "merged").run()
            realized = imported_people(db)[0]
            self.assertEqual(realized.id, old_id)
            CsvIO.write_dict_rows(csv, PEOPLE_SCHEMA_COLUMNS, [
                {"id": old_id, "full_name": "Jordan Bravo", "primary_email": "jordan@example.test",
                 "last_interaction": "2026-09-28T00:00:00Z"},
            ])
            project_imported_people(db, read_imported_people(csv))
            roster = imported_people(db)
            self.assertEqual(len(roster), 1)
            self.assertEqual((roster[0].id, roster[0].public_identifier),
                             (realized.id, "jordan-bravo"))
            self.assertEqual(roster[0].last_interaction, "2026-09-28T00:00:00+00:00")
            self.assertEqual(db.query("SELECT decision_approved FROM links WHERE row_key='research:one'")[0][0], "yes")

    def test_new_unlinked_candidate_enters_existing_enrichment_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            csv = root / "people.csv"
            CsvIO.write_dict_rows(csv, PEOPLE_SCHEMA_COLUMNS, [
                {"id": "candidate:email:jordan@example.test", "full_name": "Jordan Bravo",
                 "primary_email": "jordan@example.test", "source_channels": "gmail_msgvault"},
            ])
            project_imported_people(db, read_imported_people(csv))
            parent_id = stored_people(db)[0].parent_id
            db.decide_worth(parent_id, "yes")
            db.project_rows((
                ArtifactRow("facts:one", "facts", parent_id, "/facts/one", "sha", "projected"),
                FactRow(parent_id, parent_id, "facts:one", machine_worth="yes", facts_json="{}"),
            ))
            self.assertEqual([row.row_key for row in enrichment_queue(db)],
                             ["candidate:email:jordan@example.test"])

    def test_incremental_import_preserves_omitted_rows_and_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            csv = root / "people.csv"
            CsvIO.write_dict_rows(csv, PEOPLE_SCHEMA_COLUMNS, [
                {"id": "person-1", "full_name": "Jordan Bravo", "primary_email": "jordan@example.test",
                 "public_identifier": "jordan-bravo", "headline": "Founder", "current_company": "Acme"},
                {"id": "person-2", "full_name": "Casey Delta", "primary_email": "casey@example.test"},
            ])
            project_imported_people(db, read_imported_people(csv))
            parent_ids = {row.parent_id for row in parents(db)}
            CsvIO.write_dict_rows(csv, PEOPLE_SCHEMA_COLUMNS, [
                {"id": "person-1", "full_name": "Jordan Bravo", "primary_email": "jordan@example.test",
                 "public_identifier": "jordan-bravo", "current_title": "CEO"},
                {"id": "person-3", "full_name": "Morgan Echo", "primary_email": "morgan@example.test"},
            ])
            project_imported_people(db, read_imported_people(csv))
            roster = {row.id: row for row in imported_people(db)}
            self.assertEqual(set(roster), {"person-1", "person-2", "person-3"})
            self.assertEqual((roster["person-1"].headline, roster["person-1"].current_company,
                              roster["person-1"].current_title), ("Founder", "Acme", "CEO"))
            self.assertTrue(parent_ids.issubset({row.parent_id for row in parents(db)}))
            self.assertEqual(db.query("PRAGMA foreign_key_check"), [])

    def test_changed_slug_drops_old_profile_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Db(root / "deep-context.sqlite")
            csv = root / "people.csv"
            CsvIO.write_dict_rows(csv, PEOPLE_SCHEMA_COLUMNS, [
                {"id": "person-1", "full_name": "Jordan Bravo", "primary_email": "jordan@example.test",
                 "public_identifier": "old-slug", "headline": "Old profile", "current_company": "Old Co",
                 "rapidapi_response": '{"person":"old"}'},
            ])
            project_imported_people(db, read_imported_people(csv))
            CsvIO.write_dict_rows(csv, PEOPLE_SCHEMA_COLUMNS, [
                {"id": "person-1", "full_name": "Jordan Bravo", "primary_email": "jordan@example.test",
                 "public_identifier": "new-slug", "headline": "New profile",
                 "current_company": "New Co", "rapidapi_response": '{"person":"new"}'},
            ])
            project_imported_people(db, read_imported_people(csv))
            row = imported_people(db)[0]
            self.assertEqual(row.public_identifier, "new-slug")
            self.assertEqual((row.headline, row.current_company, row.rapidapi_response),
                             ("New profile", "New Co", '{"person":"new"}'))
