"""Fan-in preserves source identity regardless of old profile associations."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.imports.merge_people import PeopleMerge
from packs.ingestion.primitives.pipeline.contract import PeopleRow
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.shared.csv_io import CsvIO


class SourceIdentityTests(unittest.TestCase):
    def test_wrong_directory_email_and_phone_matches_do_not_change_contacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.csv"
            contacts = [
                {"id": "candidate:email:casey@example.com", "full_name": "Casey Bravo",
                 "primary_email": "casey@example.com", "source_channels": "gmail_msgvault"},
                {"id": "candidate:phone:+15550100123", "full_name": "Jordan Delta",
                 "primary_phone": "+15550100123", "source_channels": "imessage"},
            ]
            CsvIO.write_dict_rows(source, PEOPLE_SCHEMA_COLUMNS, [PeopleRow.model_validate(row).to_row() for row in contacts])
            directory = root / ".powerpacks/network-import/directory.csv"
            directory.parent.mkdir(parents=True)
            directory.write_text("email,phone,status,confidence,linkedin_url\n"
                                 "casey@example.com,,found,1,https://linkedin.com/in/wrong-profile\n"
                                 ",+15550100123,found,1,https://linkedin.com/in/wrong-profile\n")
            before = source.read_bytes()
            previous = Path.cwd()
            try:
                os.chdir(root)
                merger = PeopleMerge(inputs=[source], output_dir=root / "merged")
                merger.run()
            finally:
                os.chdir(previous)
            rows = {row["id"]: row for row in CsvIO.read_dict_rows(merger.people_csv)}
            self.assertEqual(set(rows), {contact["id"] for contact in contacts})
            for contact in contacts:
                row = rows[contact["id"]]
                self.assertEqual(row["full_name"], contact["full_name"])
                self.assertEqual(row["primary_email"], contact.get("primary_email", ""))
                self.assertEqual(row["primary_phone"], contact.get("primary_phone", ""))
                self.assertEqual((row["public_identifier"], row["linkedin_url"]), ("", ""))
            self.assertEqual(source.read_bytes(), before)

    def test_shared_profile_and_cache_do_not_join_or_enrich_distinct_source_contacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.csv"
            contacts = [
                {"id": "source-casey", "full_name": "Casey Bravo", "primary_email": "casey@example.com"},
                {"id": "source-jordan", "full_name": "Jordan Delta", "primary_email": "jordan@example.com"},
            ]
            for row in contacts:
                row.update(public_identifier="wrong-profile", linkedin_url="https://linkedin.com/in/wrong-profile")
            CsvIO.write_dict_rows(source, PEOPLE_SCHEMA_COLUMNS, [PeopleRow.model_validate(row).to_row() for row in contacts])
            cache = root / ".powerpacks/network-import/profile_cache_v2"
            cache.mkdir(parents=True)
            (cache / "wrong-profile.json").write_text(json.dumps({
                "normalized_profile": {"success": True},
                "raw_response": {"full_name": "Riley Echo", "headline": "Founder",
                                 "experiences": [{"title": "Founder", "company": "Example Labs"}]},
            }))
            previous = Path.cwd()
            try:
                os.chdir(root)
                merger = PeopleMerge(inputs=[source], output_dir=root / "merged")
                merger.run()
                before = merger.people_csv.read_bytes()
                merger.run()
                self.assertEqual(merger.people_csv.read_bytes(), before)
            finally:
                os.chdir(previous)
            rows = {row["id"]: row for row in CsvIO.read_dict_rows(merger.people_csv)}
            self.assertEqual(set(rows), {contact["id"] for contact in contacts})
            for contact in contacts:
                row = rows[contact["id"]]
                self.assertEqual(json.loads(row["all_emails"]), [contact["primary_email"]])
                self.assertEqual(row["full_name"], contact["full_name"])
                self.assertEqual((row["headline"], row["work_experiences"]), ("", ""))
