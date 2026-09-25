"""Synthetic Messages import contracts: the floor, then source-only mapping."""

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from packs.ingestion.primitives.discover.common import read_csv_rows, write_csv_rows
from packs.ingestion.primitives.imports.messages import importer, util
from packs.ingestion.schemas.message_contacts import CSV_HEADERS, MessageContact
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS


class MessageContactBoundaryTests(unittest.TestCase):
    def test_csv_cells_parse_once_without_changing_channel_order(self):
        contact = MessageContact.from_csv_row({
            "phone": " +15550100123 ", "name": " Jordan Bravo ",
            "source": " WhatsApp / iMessage / whatsapp ",
            "imessage_message_count": "3.0", "whatsapp_message_count": "9",
            "skip": "yes", "match_status": "matched", "matched_person_id": "person-1",
        })
        self.assertEqual(contact.phone, "+15550100123")
        self.assertEqual(contact.name, "Jordan Bravo")
        self.assertFalse(hasattr(contact, "skip"))
        self.assertFalse(hasattr(contact, "match_status"))
        self.assertFalse(hasattr(contact, "matched_person_id"))
        self.assertEqual(util.messages_source_channels(contact), ["whatsapp", "imessage"])
        self.assertEqual(util.contact_interaction_counts(contact), {"imessage": 3, "whatsapp": 9})
        with self.assertRaises(FrozenInstanceError):
            contact.imessage_message_count = 7

    def test_empty_and_invalid_optional_cells_keep_neutral_values(self):
        contact = MessageContact.from_csv_row({
            "phone": "+15550100123", "name": "", "imessage_message_count": "-2",
            "whatsapp_message_count": "nan",
        })
        self.assertEqual(contact.whatsapp_message_count, 0)
        self.assertEqual(util.contact_interaction_counts(contact), {})
        self.assertEqual(util.messages_source_channels(contact), ["messages"])
        self.assertEqual(util.contact_last_interaction(contact), "")


class ContactFloorTests(unittest.TestCase):
    def test_fresh_import_keeps_only_contacts_clearing_the_floor(self):
        rows = [
            {"phone": "+15550100123", "name": "Jordan Bravo", "source": "imessage",
             "message_count": "4", "imessage_message_count": "4",
             "imessage_last_message": "2026-09-01T12:00:00+00:00"},
            {"phone": "+15550100124", "name": "", "source": "imessage",
             "message_count": "6", "imessage_message_count": "6"},
            {"phone": "+15550100125", "name": "5550100125", "source": "imessage",
             "message_count": "6", "imessage_message_count": "6"},
            {"phone": "+15550100126", "name": "Casey Bravo", "source": "whatsapp",
             "message_count": "0", "whatsapp_message_count": "0"},
            {"phone": "+15550100127", "name": "Riley Bravo", "source": "imessage",
             "message_count": "3", "imessage_message_count": "3", "is_in_group_chats": "true"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contacts = root / "messages/contacts.csv"
            write_csv_rows(contacts, CSV_HEADERS, rows)
            node = importer.MessagesImport(contacts_csv=contacts, import_dir=root / "import")
            node.run()
            manifest = node.written
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["stats"], {"people": 1, "candidates": 1})
            self.assertEqual(manifest["skipped"], {
                "no_name": 1, "name_is_phone": 1, "below_min_messages": 1, "group_only_low_signal": 1,
            })
            _headers, people = read_csv_rows(root / "import/messages/people.csv")
            self.assertEqual([row["id"] for row in people], ["candidate:phone:+15550100123"])


class SourceOnlyMessagesCliTests(unittest.TestCase):
    def test_fresh_import_maps_source_metadata_and_counts_floor_skips(self):
        rows = [
            {"phone": "+15550100123", "name": "Jordan Bravo 🚲", "source": "imessage",
             "match_status": "matched", "matched_person_id": "wrong-person",
             "matched_name": "Wrong Person", "matched_linkedin_url": "https://linkedin.com/in/wrong-person",
             "match_method": "name_exact", "match_confidence": "1", "match_reason": "old guess",
             "skip": "true", "message_count": "4", "imessage_message_count": "4",
             "imessage_last_message": "2026-09-01T12:00:00+00:00"},
            {"phone": "+15550100124", "name": "", "source": "whatsapp", "message_count": "0",
             "whatsapp_message_count": "0", "is_in_group_chats": "true"},
            {"phone": "Casey@Example.com", "name": "Casey Bravo", "source": "imessage/whatsapp",
             "message_count": "3", "imessage_message_count": "1", "whatsapp_message_count": "2",
             "imessage_last_message": "2026-09-01T12:00:00+00:00",
             "whatsapp_last_message": "2026-09-02T12:00:00+00:00",
             "match_status": "suggested", "matched_person_id": "wrong-email-person"},
            {"phone": "+15550100125", "name": "K", "source": "imessage", "message_count": "1",
             "imessage_message_count": "1", "is_in_group_chats": "true"},
            {"phone": "911", "name": "Service Desk", "source": "imessage", "message_count": "0"},
            {"phone": "", "name": "No Identifier"},
            {"phone": "not-an-identifier", "name": "Invalid Identifier"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contacts = root / ".powerpacks/messages/contacts.csv"
            write_csv_rows(contacts, CSV_HEADERS + ["skip", "match_status", "matched_person_id", "matched_name", "matched_linkedin_url", "match_method", "match_confidence", "match_reason"], rows)
            source_bytes = contacts.read_bytes()
            result = subprocess.run(
                [sys.executable, str(Path(importer.__file__).resolve()), "run"],
                cwd=root, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = json.loads(result.stdout)
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["stats"], {"people": 2, "candidates": 2})
            self.assertEqual(manifest["skipped"], {
                "no_name": 1, "bad_name": 1, "short_code_or_invalid_phone": 3,
            })
            out_dir = root / ".powerpacks/network-import/import/messages"
            headers, people = read_csv_rows(out_dir / "people.csv")
            self.assertEqual(headers, PEOPLE_SCHEMA_COLUMNS)
            by_id = {row["id"]: row for row in people}
            self.assertEqual(set(by_id), {
                "candidate:phone:+15550100123", "candidate:email:casey@example.com",
            })
            named = by_id["candidate:phone:+15550100123"]
            self.assertEqual(named["full_name"], "Jordan Bravo 🚲")
            self.assertEqual(named["primary_phone"], "+15550100123")
            self.assertEqual(json.loads(named["all_phones"]), ["+15550100123"])
            self.assertEqual(json.loads(named["interaction_counts"]), {"imessage": 4})
            self.assertEqual(named["last_interaction"], "2026-09-01T12:00:00+00:00")
            email = by_id["candidate:email:casey@example.com"]
            self.assertEqual(email["primary_email"], "Casey@Example.com")
            self.assertEqual(json.loads(email["all_emails"]), ["Casey@Example.com"])
            self.assertEqual(email["primary_phone"], "")
            self.assertEqual(email["all_phones"], "")
            self.assertEqual(email["source_channels"], "imessage,whatsapp")
            self.assertEqual(json.loads(email["interaction_counts"]), {"imessage": 1, "whatsapp": 2})
            self.assertEqual(email["last_interaction"], "2026-09-02T12:00:00+00:00")
            for person in people:
                self.assertEqual(person["linkedin_url"], "")
                self.assertEqual(person["public_identifier"], "")
                self.assertEqual(person["superseded_person_ids"], "")
                self.assertEqual(person["source_artifacts"], ".powerpacks/messages/contacts.csv")
            self.assertNotIn("wrong-person", (out_dir / "people.csv").read_text())
            self.assertEqual(contacts.read_bytes(), source_bytes)
            self.assertEqual(sorted(path.name for path in out_dir.iterdir()), ["manifest.json", "people.csv"])
            self.assertFalse((root / ".powerpacks/network-import/directory.csv").exists())
            self.assertFalse(list(root.rglob("research_review.csv")))
            self.assertFalse(list(root.rglob("*.match.manifest.json")))


if __name__ == "__main__":
    unittest.main()
