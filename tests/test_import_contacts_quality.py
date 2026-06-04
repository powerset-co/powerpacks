import csv
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.discover_contacts_pipeline.directory import DIRECTORY_COLUMNS
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


if __name__ == "__main__":
    unittest.main()
