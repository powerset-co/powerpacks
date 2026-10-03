from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.db import queries
from packs.ingestion.primitives.deep_context.db.context_queries import collection_sources
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.ensure_parents.ensure_parents import EnsureParents
from packs.ingestion.primitives.deep_context.ensure_parents.imported_people import project_imported_people, read_imported_people
from packs.ingestion.primitives.deep_context.realize.export_people import ExportPeople
from packs.ingestion.primitives.imports.merge_people import PeopleMerge
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS, generate_person_id
from packs.shared.csv_io import CsvIO


class SourceContactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.gmail = self.root / "gmail.csv"
        self.messages = self.root / "messages.csv"
        self.directory = self.root / "directory.csv"
        self.first = "candidate:email:jordan@example.com"
        self.second = "candidate:phone:+15550100123"
        self.aggregate = generate_person_id("jordan-bravo")
        CsvIO.write_dict_rows(self.directory, ["status", "email", "phone", "public_identifier", "confidence"], [
            {"status": "found", "email": "jordan@example.com", "public_identifier": "jordan-bravo", "confidence": "1"},
            {"status": "found", "phone": "+15550100123", "public_identifier": "jordan-bravo", "confidence": "1"},
        ])
        self.merge = PeopleMerge(inputs=[self.gmail, self.messages], output_dir=self.root / "merged",
                                 directory_csv=self.directory, profile_cache_dir=self.root / "cache")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_sources(self, *, phone: str = "") -> None:
        CsvIO.write_dict_rows(self.gmail, PEOPLE_SCHEMA_COLUMNS, [{
            "id": self.first, "full_name": "Jordan Bravo", "primary_email": "jordan@example.com",
            "primary_phone": phone, "source_channels": "gmail_msgvault",
            "superseded_person_ids": '["gmail:original-hash"]',
            "source_artifacts": '["gmail-account/people.csv"]',
        }])
        CsvIO.write_dict_rows(self.messages, PEOPLE_SCHEMA_COLUMNS, [{
            "id": self.second, "full_name": "Casey Delta", "primary_phone": "+15550100123",
            "source_channels": "imessage", "source_artifacts": '["messages/contacts.csv"]',
        }])

    def project(self) -> None:
        self.merge.run()
        EnsureParents(db=self.db, people_csv=self.merge.people_csv).run()

    def identifiers(self) -> dict[str, set[tuple[str, str]]]:
        result: dict[str, set[tuple[str, str]]] = {}
        for row in queries.identifiers(self.db):
            result.setdefault(row.person_id, set()).add((row.kind, row.normalized_value))
        return result

    def test_cold_linkedin_collapse_preserves_each_actual_source_contact(self) -> None:
        self.write_sources()
        before = self.gmail.read_bytes(), self.messages.read_bytes()
        self.project()
        people = queries.people(self.db)
        self.assertEqual({row.person_id for row in people}, {self.first, self.second, self.aggregate})
        self.assertEqual(len({row.parent_id for row in people}), 1)
        self.assertEqual(self.identifiers(), {
            self.first: {("email", "jordan@example.com")},
            self.second: {("phone", "+15550100123")},
        })
        self.assertEqual({row.person_id for row in collection_sources(self.db)}, {self.first, self.second})
        roster = queries.imported_people(self.db)
        self.assertEqual(len(roster), 1)
        self.assertEqual((roster[0].primary_email, roster[0].primary_phone), ("jordan@example.com", "+15550100123"))
        self.assertEqual(before, (self.gmail.read_bytes(), self.messages.read_bytes()))

    def test_warm_source_refresh_updates_original_contact_identifiers(self) -> None:
        self.write_sources()
        self.project()
        parent = queries.people(self.db)[0].parent_id
        self.write_sources(phone="+15550100456")
        self.project()
        self.assertEqual(self.identifiers(), {
            self.first: {("email", "jordan@example.com"), ("phone", "+15550100456")},
            self.second: {("phone", "+15550100123")},
        })
        self.assertEqual({row.parent_id for row in queries.people(self.db)}, {parent})

    def test_same_contact_in_two_sources_remains_one_original_contact(self) -> None:
        self.write_sources()
        CsvIO.write_dict_rows(self.messages, PEOPLE_SCHEMA_COLUMNS, [{
            "id": self.first, "full_name": "Jordan Bravo", "primary_email": "jordan@example.com",
            "source_channels": "imessage", "source_artifacts": '["messages/contacts.csv"]',
        }])
        self.project()
        self.assertEqual({row.person_id for row in queries.people(self.db)}, {self.first, self.aggregate})
        self.assertEqual({row.source for row in queries.sources(self.db, person_id=self.first)}, {"gmail_msgvault", "imessage"})
        self.assertEqual([row.person_id for row in collection_sources(self.db)], [self.first])

    def test_retargeted_legacy_aggregate_does_not_collect_copied_contacts(self) -> None:
        self.write_sources()
        self.merge.run()
        project_imported_people(self.db, read_imported_people(self.merge.people_csv))
        self.db.decide_identity("jordan-bravo", "retarget", approved="yes",
                                replacement_url="https://www.linkedin.com/in/casey-delta",
                                replacement_public_identifier="casey-delta")
        ExportPeople(db=self.db, out_dir=self.root / "merged").run()
        self.project()
        self.assertEqual({row.person_id for row in collection_sources(self.db)}, {self.first, self.second})

    def test_manifest_source_missing_after_fan_in_fails_visibly(self) -> None:
        self.write_sources()
        self.merge.run()
        self.gmail.rename(self.gmail.with_suffix(".csv.bkup"))
        with self.assertRaisesRegex(FileNotFoundError, "gmail.csv"):
            EnsureParents(db=self.db, people_csv=self.merge.people_csv).run()
        self.assertEqual(queries.people(self.db), ())

    def test_optional_source_absent_during_fan_in_does_not_block(self) -> None:
        self.write_sources()
        self.messages.rename(self.messages.with_suffix(".csv.bkup"))
        self.project()
        self.assertEqual({row.person_id for row in queries.people(self.db)}, {self.first, self.aggregate})

    def test_legacy_csv_without_manifest_keeps_existing_projection(self) -> None:
        people = self.root / "legacy.csv"
        CsvIO.write_dict_rows(people, PEOPLE_SCHEMA_COLUMNS, [{
            "id": self.aggregate, "full_name": "Jordan Bravo", "primary_email": "jordan@example.com",
            "superseded_person_ids": f'["{self.first}"]',
        }])
        EnsureParents(db=self.db, people_csv=people).run()
        self.assertEqual({row.person_id for row in queries.people(self.db)}, {self.aggregate})


if __name__ == "__main__":
    unittest.main()
