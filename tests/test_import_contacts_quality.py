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
from packs.ingestion.primitives.import_contacts_pipeline.common import (
    directory_source_account_quality,
    normalize_directory_source_accounts,
)
from packs.shared.csv_io import CsvIO


def write_directory(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DIRECTORY_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in DIRECTORY_COLUMNS})


class ImportContactsQualityTests(unittest.TestCase):
    def test_gmail_import_uses_per_account_people_records_from_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            base = tmp / ".powerpacks/network-import"
            discover_gmail = base / "discover/gmail"
            discover_gmail.mkdir(parents=True)
            contacts = discover_gmail / "contacts.csv"
            queue = discover_gmail / "linkedin_resolution_queue.csv"
            account_dir = discover_gmail / "arthur-powerset.co"
            account_people = account_dir / "people.csv"
            account_queue = account_dir / "linkedin_resolution_queue.csv"
            contacts.write_text("primary_email,full_name\njane@example.com,Jane\n", encoding="utf-8")
            queue.write_text("primary_email,full_name\njane@example.com,Jane\n", encoding="utf-8")
            account_dir.mkdir(parents=True)
            account_queue.write_text("handle,primary_email,total_messages\njane@example.com,jane@example.com,2\n", encoding="utf-8")
            with account_people.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=PEOPLE_SCHEMA_COLUMNS)
                writer.writeheader()
                writer.writerow({
                    **{column: "" for column in PEOPLE_SCHEMA_COLUMNS},
                    "primary_email": "jane@example.com",
                    "interaction_counts": json.dumps({"gmail": 2}),
                    "last_interaction": "2026-01-02T00:00:00Z",
                })
            (discover_gmail / "manifest.json").write_text(json.dumps({
                "contacts_csv": str(contacts),
                "linkedin_resolution_queue_csv": str(queue),
                "children": [{
                    "account_email": "arthur@powerset.co",
                    "artifacts": {
                        "linkedin_resolution_queue_csv": str(account_queue),
                        "people_csv": str(account_people),
                    },
                }],
            }), encoding="utf-8")

            with mock.patch.object(gmail_import, "DEFAULT_BASE_DIR", base):
                artifacts = gmail_import.gmail_artifacts_from_discovery()

            self.assertEqual(artifacts["gmail_linkedin_resolution_queue_csv"], str(queue))
            self.assertEqual(artifacts["gmail_linkedin_resolution_queue_csvs"], [{
                "account_email": "arthur@powerset.co",
                "queue_csv": str(account_queue),
                "people_csv": str(account_people),
                "slug": "arthur-powerset.co",
            }])
            self.assertEqual(artifacts["gmail_people_records"], [{
                "account_email": "arthur@powerset.co",
                "people_csv": str(account_people),
                "slug": "arthur-powerset.co",
            }])

    def test_gmail_import_rejects_stale_child_people_without_counts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            base = tmp / ".powerpacks/network-import"
            discover_gmail = base / "discover/gmail"
            discover_gmail.mkdir(parents=True)
            contacts = discover_gmail / "contacts.csv"
            queue = discover_gmail / "linkedin_resolution_queue.csv"
            stale_people = tmp / "stale-people.csv"
            contacts.write_text("primary_email,full_name\njane@example.com,Jane\n", encoding="utf-8")
            queue.write_text("primary_email,full_name\njane@example.com,Jane\n", encoding="utf-8")
            stale_people.write_text("primary_email,full_name\njane@example.com,Jane\n", encoding="utf-8")
            (discover_gmail / "manifest.json").write_text(json.dumps({
                "contacts_csv": str(contacts),
                "linkedin_resolution_queue_csv": str(queue),
                "children": [{
                    "account_email": "arthur@powerset.co",
                    "artifacts": {
                        "linkedin_resolution_queue_csv": str(queue),
                        "people_csv": str(stale_people),
                    },
                }],
            }), encoding="utf-8")

            with mock.patch.object(gmail_import, "DEFAULT_BASE_DIR", base):
                artifacts = gmail_import.gmail_artifacts_from_discovery()

            self.assertEqual(artifacts["gmail_linkedin_resolution_queue_csv"], str(queue))
            self.assertNotIn("gmail_linkedin_resolution_queue_csvs", artifacts)
            self.assertEqual(artifacts["gmail_invalid_discovery_records"][0]["reason"], "missing_people_schema_or_interaction_counts")

    def test_gmail_account_people_merge_preserves_interaction_counts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            one = tmp / "one.csv"
            two = tmp / "two.csv"
            out = tmp / "people.gmail.csv"
            base = {
                column: ""
                for column in PEOPLE_SCHEMA_COLUMNS
            }
            for path, count, last_interaction in [
                (one, 2, "2026-01-02T00:00:00Z"),
                (two, 5, "2026-01-03T00:00:00Z"),
            ]:
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=PEOPLE_SCHEMA_COLUMNS)
                    writer.writeheader()
                    writer.writerow({
                        **base,
                        "id": f"gmail:{path.stem}:jane@example.com",
                        "linkedin_url": "https://www.linkedin.com/in/jane-example",
                        "public_identifier": "jane-example",
                        "full_name": "Jane Example",
                        "primary_email": "jane@example.com",
                        "source_channels": "gmail_msgvault",
                        "interaction_counts": json.dumps({"gmail": count}),
                        "last_interaction": last_interaction,
                    })

            legacy = gmail_import.load_legacy_discover_module()
            result = legacy.materialize_gmail_merged_people_csv([str(one), str(two)], out)

            self.assertEqual(result["status"], "completed")
            with out.open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0]["interaction_counts"]), {"gmail": 5})
            self.assertEqual(rows[0]["last_interaction"], "2026-01-03T00:00:00Z")

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
                rows = list(CsvIO.dict_reader(handle))
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
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source"], "linkedin_csv")
            self.assertEqual(rows[0]["source_account"], "arthur")
            self.assertEqual(rows[0]["linkedin_url"], "https://www.linkedin.com/in/ada")


if __name__ == "__main__":
    unittest.main()
