import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "packs/messages/primitives/sync_contact_datalake/sync_contact_datalake.py"
spec = importlib.util.spec_from_file_location("sync_contact_datalake", MODULE)
sync_contact_datalake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_contact_datalake)


class SyncContactDatalakeTests(unittest.TestCase):
    def test_sync_response_summary_hides_backend_table_details(self):
        summary = sync_contact_datalake.summarize_sync_response({
            "received": 22,
            "datalake_inserted": 20,
            "datalake_updated": 2,
            "operator_contacts_inserted": 99,
            "linkedin_candidates_inserted": 88,
            "materialized": False,
            "skipped": 1,
            "errors": 0,
        })
        self.assertEqual(summary, {
            "uploaded_contacts": 22,
            "message": "Uploaded 22 contacts",
            "errors": 0,
        })
        self.assertNotIn("materialized", summary)
        self.assertNotIn("operator_contacts_inserted", summary)

    def test_records_include_aleph_synthetic_profile_shape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            csv_path = root / "research_review.csv"
            research_dir = root / "research"
            handle = "phone-test"
            (research_dir / handle).mkdir(parents=True)
            profile = {
                "research_id": "r1",
                "person": {"full_name": "Jane Doe", "first_name": "Jane", "last_name": "Doe"},
                "social": {"linkedin_url": "https://www.linkedin.com/in/Jane-Doe/", "primary_phone": "+15551234567"},
                "headline": {"text": "Founder"},
                "summary": {"text": "Builds things."},
                "location": {"city": "San Francisco", "state": "CA", "country": "United States", "raw": "SF"},
                "positions": [{"title": "Founder", "company_name": "Acme", "start_date": "2024", "is_current": True, "confidence": 0.9}],
                "education": [{"school_name": "Stanford", "degree": "BS", "confidence": 0.8}],
                "metadata": {"estimated_completeness": 0.8, "total_sources_consulted": 3, "research_date": "2026-05-06"},
            }
            (research_dir / handle / "01_research_parallel.json").write_text(json.dumps(profile))
            with csv_path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "handle", "phone_e164", "full_name", "total_messages",
                    "imessage_message_count", "whatsapp_message_count", "last_message",
                    "imessage_last_message", "whatsapp_last_message", "bucket",
                ])
                writer.writeheader()
                writer.writerow({
                    "handle": handle,
                    "phone_e164": "+15551234567",
                    "full_name": "Jane Doe",
                    "total_messages": "7",
                    "imessage_message_count": "2",
                    "whatsapp_message_count": "5",
                    "last_message": "2026-05-02T00:00:00Z",
                    "imessage_last_message": "2026-05-01T00:00:00Z",
                    "whatsapp_last_message": "2026-05-02T00:00:00Z",
                    "bucket": "yes",
                })

            records = sync_contact_datalake.load_records(csv_path, research_dir)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["phone_e164"], "+15551234567")
        self.assertEqual(record["phone"], "+15551234567")
        self.assertEqual(record["full_name"], "Jane Doe")
        self.assertEqual(record["name"], "Jane Doe")
        self.assertEqual(record["linkedin_url"], "https://www.linkedin.com/in/jane-doe")
        self.assertEqual(record["public_identifier"], "jane-doe")
        self.assertEqual(record["processing_status"], "staged")
        self.assertEqual(record["message_count"], 7)
        self.assertEqual(record["imessage_message_count"], 2)
        self.assertEqual(record["whatsapp_message_count"], 5)
        self.assertEqual(record["last_message"], "2026-05-02T00:00:00Z")
        self.assertEqual(record["imessage_last_message"], "2026-05-01T00:00:00Z")
        self.assertEqual(record["whatsapp_last_message"], "2026-05-02T00:00:00Z")
        self.assertEqual(record["research_profile"]["research_id"], "r1")
        synthetic = record["synthetic_profile"]
        self.assertEqual(synthetic["public_identifier"], "jane-doe")
        self.assertEqual(synthetic["linkedin_url"], "https://www.linkedin.com/in/jane-doe")
        self.assertEqual(synthetic["enrichment_provider"], "synthetic")
        self.assertEqual(synthetic["work_experiences"][0]["company_name"], "Acme")
        self.assertIn("person_id", synthetic)
        self.assertTrue(synthetic["synthetic_metadata"]["draft"])

    def test_load_records_syncs_only_approved_contacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            csv_path = root / "research_review.csv"
            research_dir = root / "research"
            for handle in ("phone-yes", "phone-default-yes", "phone-maybe", "phone-network", "phone-no"):
                (research_dir / handle).mkdir(parents=True)
                (research_dir / handle / "01_research_parallel.json").write_text(
                    json.dumps({"person": {"full_name": handle}, "social": {}}),
                    encoding="utf-8",
                )
            with csv_path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["bucket", "handle", "phone_e164", "full_name", "exclude", "in_network"])
                writer.writeheader()
                writer.writerow({"bucket": "medium", "handle": "phone-yes", "phone_e164": "+15550000001", "full_name": "Yes", "exclude": "no", "in_network": ""})
                writer.writerow({"bucket": "yes", "handle": "phone-default-yes", "phone_e164": "+15550000005", "full_name": "Default Yes", "exclude": "", "in_network": "false"})
                writer.writerow({"bucket": "medium", "handle": "phone-maybe", "phone_e164": "+15550000002", "full_name": "Maybe", "exclude": "", "in_network": ""})
                writer.writerow({"bucket": "medium", "handle": "phone-network", "phone_e164": "+15550000004", "full_name": "Network", "exclude": "", "in_network": "true"})
                writer.writerow({"bucket": "confident", "handle": "phone-no", "phone_e164": "+15550000003", "full_name": "No", "exclude": "yes", "in_network": ""})

            records = sync_contact_datalake.load_records(csv_path, research_dir)

        self.assertEqual([record["handle"] for record in records], ["phone-yes", "phone-default-yes", "phone-network"])
        self.assertTrue(records[0]["approved"])
        self.assertNotIn("include", records[0])
        self.assertNotIn("upload_decision", records[0])


if __name__ == "__main__":
    unittest.main()
