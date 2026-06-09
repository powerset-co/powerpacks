import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.ingestion.primitives.discover_contacts_pipeline.directory import DIRECTORY_COLUMNS
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.ingestion.primitives.import_contacts_pipeline import gmail as gmail_import
from packs.ingestion.primitives.import_contacts_pipeline import linkedin as linkedin_import
from packs.ingestion.primitives.import_contacts_pipeline import common as import_common
from packs.ingestion.primitives.import_contacts_pipeline.common import (
    directory_source_account_quality,
    normalize_directory_source_accounts,
)


def write_directory(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DIRECTORY_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in DIRECTORY_COLUMNS})


class ImportContactsQualityTests(unittest.TestCase):
    def test_gmail_import_uses_stable_discovery_queue_when_child_queue_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            base = tmp / ".powerpacks/network-import"
            discover_gmail = base / "discover/gmail"
            discover_gmail.mkdir(parents=True)
            contacts = discover_gmail / "contacts.csv"
            queue = discover_gmail / "linkedin_resolution_queue.csv"
            contacts.write_text("primary_email,full_name\njane@example.com,Jane\n", encoding="utf-8")
            queue.write_text("primary_email,full_name\njane@example.com,Jane\n", encoding="utf-8")
            (discover_gmail / "manifest.json").write_text(json.dumps({
                "contacts_csv": str(contacts),
                "linkedin_resolution_queue_csv": str(queue),
                "children": [{
                    "account_email": "arthur@powerset.co",
                    "payload": {
                        "artifacts": {
                            "linkedin_resolution_queue_csv": str(tmp / "missing-temp-queue.csv"),
                            "people_csv": str(tmp / "missing-people.csv"),
                        }
                    },
                }],
            }), encoding="utf-8")

            with mock.patch.object(gmail_import, "DEFAULT_BASE_DIR", base):
                artifacts = gmail_import.gmail_artifacts_from_discovery()

            self.assertEqual(artifacts["gmail_linkedin_resolution_queue_csv"], str(queue))
            self.assertEqual(artifacts["gmail_linkedin_resolution_queue_csvs"], [{
                "account_email": "",
                "queue_csv": str(queue),
                "people_csv": str(contacts),
                "slug": "all",
            }])

    def test_gmail_import_manifest_carries_timing_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            base = tmp / ".powerpacks/network-import"
            discover_gmail = base / "discover/gmail"
            discover_gmail.mkdir(parents=True)
            import_dir = base / "import"
            contacts = discover_gmail / "contacts.csv"
            queue = discover_gmail / "linkedin_resolution_queue.csv"
            contacts.write_text("primary_email,full_name\njane@example.com,Jane\n", encoding="utf-8")
            queue.write_text("primary_email,full_name\njane@example.com,Jane\n", encoding="utf-8")
            (discover_gmail / "manifest.json").write_text(json.dumps({
                "contacts_csv": str(contacts),
                "linkedin_resolution_queue_csv": str(queue),
                "children": [],
            }), encoding="utf-8")

            accounts = tmp / "accounts.json"
            accounts.write_text(json.dumps({
                "accounts": {
                    "gmail": {"config": {"selected_accounts": ["jane@example.com"]}}
                }
            }), encoding="utf-8")

            people_csv = import_dir / "gmail" / "people.csv"
            people_csv.parent.mkdir(parents=True, exist_ok=True)
            people_csv.write_text("id,full_name\nperson-jane,Jane\n", encoding="utf-8")

            legacy = SimpleNamespace(
                run_gmail_directory=lambda *_a, **_k: True,
                run_gmail_linkedin_resolution=lambda *_a, **_k: True,
                run_gmail_apply_and_enrich=lambda *_a, **_k: True,
                save_ledger=lambda *_a, **_k: None,
            )

            with mock.patch.object(gmail_import, "DEFAULT_BASE_DIR", base), \
                mock.patch.object(gmail_import, "DEFAULT_IMPORT_DIR", import_dir), \
                mock.patch.object(import_common, "DEFAULT_IMPORT_DIR", import_dir), \
                mock.patch.object(gmail_import, "load_legacy_discover_module", return_value=legacy), \
                mock.patch.object(gmail_import, "copy_people_csv", return_value=str(people_csv)), \
                mock.patch.object(gmail_import, "normalize_directory_source_accounts", return_value={"updated_rows": 0}), \
                mock.patch.object(gmail_import, "directory_source_account_quality", return_value={"status": "ok"}):
                payload = gmail_import.run(SimpleNamespace(
                    accounts=accounts,
                    operator_id="operator-1",
                    approve_parallel_spend=True,
                ))

            self.assertEqual(payload["status"], "completed")
            manifest = json.loads((import_dir / "gmail" / "manifest.json").read_text(encoding="utf-8"))
            for written in (payload, manifest):
                self.assertIn("started_at", written)
                self.assertIsInstance(written["duration_seconds"], float)
                self.assertGreaterEqual(written["duration_seconds"], 0.0)
                self.assertIsInstance(written["parallel_enrichment_seconds"], float)
                self.assertGreaterEqual(written["parallel_enrichment_seconds"], 0.0)
                self.assertEqual(written["checkpoint_every"], 25)

    def test_gmail_directory_rows_require_source_account(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "directory.csv"
            write_directory(directory, [
                {
                    "source": "gmail_msgvault",
                    "source_key": "gmail:me@example.com:email:jane@example.com",
                    "source_account": "me@example.com",
                    "source_channels": "gmail_msgvault",
                },
                {
                    "source": "gmail_msgvault",
                    "source_key": "gmail::email:missing@example.com",
                    "source_channels": "gmail_msgvault",
                },
            ])

            quality = directory_source_account_quality("gmail", directory)

            self.assertEqual(quality["status"], "failed")
            self.assertEqual(quality["checked_rows"], 2)
            self.assertEqual(quality["missing_source_account"], 1)

    def test_messages_directory_rows_require_source_account_and_channel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "directory.csv"
            write_directory(directory, [
                {
                    "source": "messages",
                    "source_key": "messages:phone:+15555550100",
                    "source_account": "imessage,whatsapp",
                    "source_channels": "imessage,whatsapp",
                },
                {
                    "source": "messages",
                    "source_key": "messages:phone:+15555550101",
                    "source_account": "messages",
                    "source_channels": "messages",
                },
            ])

            quality = directory_source_account_quality("messages", directory)

            self.assertEqual(quality["status"], "failed")
            self.assertEqual(quality["checked_rows"], 2)
            self.assertEqual(quality["missing_source_account"], 0)
            self.assertEqual(quality["invalid_source_channels"], 1)

    def test_messages_directory_source_account_self_heals_from_channels(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "directory.csv"
            write_directory(directory, [
                {
                    "source": "messages",
                    "source_key": "messages:phone:+15555550100",
                    "source_channels": "imessage,whatsapp",
                },
            ])

            repair = normalize_directory_source_accounts("messages", directory)
            quality = directory_source_account_quality("messages", directory)

            self.assertEqual(repair["updated_rows"], 1)
            self.assertEqual(quality["status"], "ok")
            with directory.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["source_account"], "imessage,whatsapp")

    def test_linkedin_direct_import_commits_people_to_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            accounts = tmp / "accounts.json"
            source_csv = tmp / "Connections.csv"
            source_csv.write_text("First Name,Last Name,URL\nAda,Lovelace,https://www.linkedin.com/in/ada\n", encoding="utf-8")
            accounts.write_text(json.dumps({
                "accounts": {
                    "linkedin_csv": {
                        "config": {
                            "csv_path": str(source_csv),
                            "source_label": "arthur",
                        }
                    }
                }
            }), encoding="utf-8")
            child_people = tmp / "child_people.csv"
            with child_people.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=PEOPLE_SCHEMA_COLUMNS)
                writer.writeheader()
                writer.writerow({
                    "id": "person-ada",
                    "public_identifier": "ada",
                    "linkedin_url": "https://www.linkedin.com/in/ada",
                    "full_name": "Ada Lovelace",
                    "source_channels": "linkedin_csv",
                })
            directory = tmp / "directory.csv"
            import_dir = tmp / "import"

            with mock.patch.object(linkedin_import, "DEFAULT_DIRECTORY_CSV", directory), \
                mock.patch.object(linkedin_import, "DEFAULT_IMPORT_DIR", import_dir), \
                mock.patch.object(linkedin_import, "run_cmd", return_value=(0, {"status": "completed", "artifacts": {"people_csv": str(child_people)}}, "")):
                payload = linkedin_import.run(SimpleNamespace(accounts=accounts, operator_id="operator-1"))

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["directory_checkpoint"]["confirmed_rows"], 1)
            with directory.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source"], "linkedin_csv")
            self.assertEqual(rows[0]["source_account"], "arthur")
            self.assertEqual(rows[0]["linkedin_url"], "https://www.linkedin.com/in/ada")


if __name__ == "__main__":
    unittest.main()
