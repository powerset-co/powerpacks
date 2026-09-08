"""Source imports own their people files; Deep Context owns identity decisions."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.deep_context.common import load_people
from packs.ingestion.primitives.imports.gmail.importer import GmailImport
from packs.ingestion.primitives.imports.directory import DIRECTORY_COLUMNS
from packs.ingestion.schemas.message_contacts import CSV_HEADERS
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.shared.csv_io import CsvIO
from packs.ingestion.primitives.imports.linkedin.network_import import LinkedInImport
from packs.ingestion.primitives.imports.merge_people import PeopleMerge
from packs.ingestion.primitives.imports.messages.importer import MessagesImport
from packs.ingestion.primitives.pipeline.contract import StageManifest
from packs.ingestion.primitives.pipeline.graph import check_graph

IMPORT_STAGE = [GmailImport, LinkedInImport, MessagesImport, PeopleMerge]


class SourceImportContractTests(unittest.TestCase):
    def test_source_imports_do_not_consult_or_write_identity_directory(self) -> None:
        for node in (GmailImport, MessagesImport):
            with self.subTest(node=node.name):
                self.assertFalse(any("directory.csv" in item.path for item in (*node.inputs, *node.outputs)))
                self.assertEqual(len(node.outputs), 1)
                self.assertTrue(node.outputs[0].path.endswith("/people.csv"))
                self.assertEqual(node.outputs[0].writes, "full_rewrite")

    def test_import_graph_has_no_conflicts_mismatches_or_cycles(self) -> None:
        report = check_graph(IMPORT_STAGE)
        for finding in ("two_writer_conflicts", "schema_mismatches", "cycles"):
            self.assertEqual(report[finding], [])

    def test_only_linkedin_people_input_is_external(self) -> None:
        external = [item.path for item in PeopleMerge.inputs if item.external]
        self.assertEqual(external, [".powerpacks/network-import/import/linkedin/people.csv"])

    def test_imports_use_existing_fingerprinted_manifest_writer(self) -> None:
        for node in (GmailImport, MessagesImport):
            self.assertTrue(issubclass(node.payload, StageManifest))
            self.assertEqual(node.manifest, "")


class DeepContextHandoffTests(unittest.TestCase):
    def test_source_candidates_and_metadata_survive_merge_with_or_without_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_people = root / "account/people.csv"
            account_people.parent.mkdir()
            CsvIO.write_dict_rows(account_people, PEOPLE_SCHEMA_COLUMNS, [
                {"primary_email": "casey@example.com", "full_name": "Casey Bravo",
                 "source_channels": "gmail_msgvault", "interaction_counts": '{"email": 8}',
                 "last_interaction": "2026-09-02T00:00:00+00:00"},
                {"primary_email": "unnamed@example.com", "source_channels": "gmail_msgvault"},
            ])
            manifest = root / "gmail-discovery.json"
            manifest.write_text(json.dumps({"children": [{
                "account_email": "owner@example.com", "people_csv": str(account_people),
            }]}))
            contacts = root / "contacts.csv"
            CsvIO.write_dict_rows(contacts, CSV_HEADERS, [
                {"phone": "casey@example.com", "name": "Casey", "source": "imessage",
                 "imessage_message_count": "3", "imessage_last_message": "2026-09-03T00:00:00+00:00"},
                {"phone": "+15550100123", "name": "", "source": "whatsapp",
                 "whatsapp_message_count": "0", "is_in_group_chats": "true"},
            ])
            gmail = GmailImport(manifest_json=manifest, import_dir=root / "import")
            messages = MessagesImport(contacts_csv=contacts, import_dir=root / "import")
            gmail.run()
            messages.run()
            inputs = [gmail.people_csv, messages.people_csv]
            originals = {path: path.read_bytes() for path in [account_people, contacts, *inputs]}
            directory = root / "directory.csv"
            for known in (False, True):
                if known:
                    CsvIO.write_dict_rows(directory, DIRECTORY_COLUMNS, [{
                        "source": "deep_context_review", "source_key": "email:casey@example.com",
                        "email": "casey@example.com", "status": "found", "confidence": "1.0",
                        "public_identifier": "casey-bravo", "linkedin_url": "https://linkedin.com/in/casey-bravo",
                    }])
                merger = PeopleMerge(inputs=inputs, directory_csv=directory, output_dir=root / "merged")
                result = merger.run()
                self.assertEqual(result.stats.dropped_unkeyable, 0)
                self.assertEqual(result.stats.rows, 3)
                people = list(load_people(merger.people_csv))
                self.assertEqual(len(people), 3)
                self.assertEqual({email for person in people for email in person.emails},
                                 {"casey@example.com", "unnamed@example.com"})
                self.assertEqual({phone for person in people for phone in person.phones}, {"+15550100123"})
                rows = CsvIO.read_dict_rows(merger.people_csv)
                casey = next(row for row in rows if row["primary_email"] == "casey@example.com")
                self.assertEqual(json.loads(casey["interaction_counts"]), {"email": 8, "imessage": 3})
                self.assertEqual(casey["last_interaction"], "2026-09-03T00:00:00+00:00")
                self.assertEqual(set(casey["source_channels"].split(",")), {"gmail_msgvault", "imessage"})
                self.assertEqual(casey["public_identifier"], "casey-bravo" if known else "")
                first = merger.people_csv.read_bytes()
                merger.run()
                self.assertEqual(merger.people_csv.read_bytes(), first)
            self.assertEqual({path: path.read_bytes() for path in originals}, originals)


if __name__ == "__main__":
    unittest.main()
