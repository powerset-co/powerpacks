from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.deep_context import collect_person_context
from packs.ingestion.primitives.deep_context.check_readiness import CheckReadiness
from packs.ingestion.primitives.deep_context.collect_person_context import CollectPersonContext
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.imported_people import (
    project_imported_people,
    read_imported_people,
)
from packs.shared.csv_io import CsvIO


FIELDS = [
    "id",
    "full_name",
    "primary_email",
    "all_emails",
    "primary_phone",
    "all_phones",
    "source_channels",
    "superseded_person_ids",
]


class ImportedPeopleBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.csv = self.root / "people.csv"
        self.db = Db(self.root / "deep-context.sqlite")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, rows: list[dict[str, str]]) -> None:
        CsvIO.write_dict_rows(self.csv, FIELDS, rows)

    def test_parser_normalizes_jsonish_channels_and_deduplicates_rows(self) -> None:
        self.write([
            {
                "id": "PERSON-1",
                "full_name": "Jordan Bravo",
                "primary_email": "Jordan@Example.test",
                "source_channels": '["gmail_msgvault", "imessage"]',
            },
            {
                "id": "person-1",
                "all_emails": '["other@example.test"]',
                "source_channels": "whatsapp",
            },
        ])

        rows = read_imported_people(self.csv)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].person_id, "person-1")
        self.assertEqual(rows[0].emails, ("jordan@example.test", "other@example.test"))
        self.assertEqual(
            rows[0].source_channels,
            ("gmail_msgvault", "imessage", "whatsapp"),
        )

    def test_projection_gets_one_stable_parent_and_preserves_newer_evidence(self) -> None:
        self.write([{
            "id": "person-1",
            "full_name": "Jordan Bravo",
            "primary_email": "first@example.test",
            "source_channels": "gmail_msgvault",
        }])
        project_imported_people(self.db, read_imported_people(self.csv))
        first = canonical_snapshot(self.db)
        parent_id = first.people[0].parent_id

        self.write([{
            "id": "person-1",
            "full_name": "",
            "primary_phone": "+15550100",
            "source_channels": "imessage",
        }])
        project_imported_people(self.db, read_imported_people(self.csv))
        current = canonical_snapshot(self.db)

        self.assertEqual(current.people[0].parent_id, parent_id)
        self.assertEqual(current.people[0].display_name, "Jordan Bravo")
        self.assertEqual(
            {(row.kind, row.normalized_value) for row in current.identifiers},
            {("email", "first@example.test"), ("phone", "+15550100")},
        )
        self.assertEqual(
            {row.source for row in current.sources},
            {"gmail_msgvault", "imessage"},
        )

    def test_superseded_identity_absorbs_into_existing_parent(self) -> None:
        self.write([{
            "id": "candidate:email:jordan@example.test",
            "full_name": "Jordan Bravo",
            "primary_email": "jordan@example.test",
            "source_channels": "gmail_msgvault",
        }])
        project_imported_people(self.db, read_imported_people(self.csv))
        prior = canonical_snapshot(self.db)
        parent_id = prior.people[0].parent_id

        self.write([{
            "id": "linkedin-person-1",
            "full_name": "Jordan Bravo",
            "primary_email": "jordan@example.test",
            "source_channels": "linkedin_csv,gmail_msgvault",
            "superseded_person_ids": '["candidate:email:jordan@example.test"]',
        }])
        project_imported_people(self.db, read_imported_people(self.csv))
        current = canonical_snapshot(self.db)

        self.assertEqual(len(current.parents), 1)
        self.assertEqual(
            {row.person_id: row.parent_id for row in current.people},
            {
                "candidate:email:jordan@example.test": parent_id,
                "linkedin-person-1": parent_id,
            },
        )

    def test_collection_projects_explicit_people_input_before_selection(self) -> None:
        self.write([{
            "id": "person-1",
            "full_name": "Jordan Bravo",
            "primary_phone": "+15550100",
            "source_channels": "imessage",
        }])
        with (
            mock.patch.object(
                collect_person_context.sources,
                "probe_chat_db",
                return_value={"exists": False, "readable": False, "messages": 0, "error": None},
            ),
            mock.patch.object(
                collect_person_context.sources, "collect_person", return_value=([], 0),
            ),
            mock.patch.object(
                collect_person_context.sources, "read_imessage_groups", return_value=[],
            ),
        ):
            result = CollectPersonContext(
                db=self.db,
                people_csv=self.csv,
                out_dir=self.root / "raw",
                msgvault_db=self.root / "missing-msgvault.db",
                chat_db=self.root / "missing-chat.db",
                wacli_db=self.root / "missing-wacli.db",
            ).execute()

        self.assertEqual(result.people_total, 1)
        self.assertEqual(canonical_snapshot(self.db).people[0].person_id, "person-1")

    def test_readiness_probes_people_input_before_sqlite_exists(self) -> None:
        self.write([{
            "id": "candidate:email:jordan@example.test",
            "full_name": "Jordan Bravo",
            "primary_email": "jordan@example.test",
            "source_channels": "gmail_msgvault",
        }])
        wacli = self.root / "wacli.db"
        wacli.touch()
        missing_db = self.root / "fresh" / "deep-context.sqlite"
        with (
            mock.patch(
                "packs.ingestion.primitives.deep_context.check_readiness.sources.probe_chat_db",
                return_value={"exists": False, "readable": False, "messages": 0, "error": None},
            ),
            mock.patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-key"}),
        ):
            result = CheckReadiness(
                db_path=missing_db,
                people_csv=self.csv,
                msgvault_db=self.root / "missing-msgvault.db",
                chat_db=self.root / "missing-chat.db",
                wacli_db=wacli,
            ).run()

        self.assertTrue(result["ready"])
        self.assertEqual(result["message_people"], 1)
        self.assertEqual(result["candidates"]["total"], 1)
        self.assertFalse(missing_db.exists())


if __name__ == "__main__":
    unittest.main()
