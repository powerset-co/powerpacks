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


class GmailCandidatesTests(unittest.TestCase):
    QUEUE_FIELDS = [
        "handle", "id", "account_emails", "source_ids", "display_name",
        "full_name", "primary_email", "company_guess", "primary_email_type",
        "total_messages", "thread_count", "last_interaction", "source",
        "source_channels",
    ]

    def write_queue(self, path: Path, rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.QUEUE_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in self.QUEUE_FIELDS})

    def test_directory_only_is_the_only_mode(self) -> None:
        args = gmail_import.build_parser().parse_args(["run"])
        self.assertNotIn("resolve_legacy", vars(args))
        self.assertNotIn("approve_parallel_spend", vars(args))
        with self.assertRaises(SystemExit):
            gmail_import.build_parser().parse_args(["run", "--resolve-legacy"])

    def test_queue_row_to_candidate_maps_and_skips(self) -> None:
        row = {
            "handle": "jane@corp.com",
            "primary_email": "Jane@Corp.com",
            "full_name": "Jane Doe",
            "company_guess": "Corp",
            "total_messages": "42",
            "thread_count": "7",
            "last_interaction": "2026-05-01T10:00:00+00:00",
            "account_emails": "me@gmail.com",
        }
        candidate = gmail_import.queue_row_to_candidate(row, cached_negative=True)
        self.assertEqual(candidate["candidate_key"], "email:jane@corp.com")
        self.assertEqual(candidate["source"], "gmail")
        self.assertEqual(json.loads(candidate["interaction_counts"]), {"gmail": 42})
        self.assertTrue(json.loads(candidate["evidence"])["cached_negative"])
        self.assertIsNone(gmail_import.queue_row_to_candidate({"primary_email": ""}, cached_negative=False))

    def test_write_gmail_candidates_unions_and_dedups_queues(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            unresolved = tmp / "unresolved.csv"
            negative = tmp / "negative.csv"
            self.write_queue(unresolved, [
                {"handle": "a@x.com", "primary_email": "a@x.com", "full_name": "Alice Adams", "total_messages": "3"},
                {"handle": "b@x.com", "primary_email": "b@x.com", "full_name": "Bob Brown", "total_messages": "5"},
            ])
            self.write_queue(negative, [
                {"handle": "b@x.com", "primary_email": "b@x.com", "full_name": "Bob Brown", "total_messages": "5"},
                {"handle": "c@x.com", "primary_email": "c@x.com", "full_name": "Cara Cole", "total_messages": "1"},
            ])
            artifacts = {
                "gmail_unresolved_linkedin_resolution_queue_csvs": [
                    {"queue_csv": str(unresolved), "account_email": "me@gmail.com"},
                ],
                "gmail_cached_negative_linkedin_resolution_queue_csvs": [
                    {"queue_csv": str(negative), "account_email": "me@gmail.com"},
                ],
            }
            import_dir = tmp / "import" / "gmail"
            import_dir.mkdir(parents=True)
            result = gmail_import.write_gmail_candidates(artifacts, import_dir)
            self.assertEqual(result["candidates"], 3)
            self.assertEqual(result["skipped"], {"no_email": 0, "duplicate_email": 1})
            with (import_dir / "candidates.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            by_key = {row["candidate_key"]: row for row in rows}
            self.assertEqual(
                sorted(by_key),
                ["email:a@x.com", "email:b@x.com", "email:c@x.com"],
            )
            # First-seen (unresolved) wins the dedup: b@x.com is not cached-negative.
            self.assertFalse(json.loads(by_key["email:b@x.com"]["evidence"])["cached_negative"])
            self.assertTrue(json.loads(by_key["email:c@x.com"]["evidence"])["cached_negative"])


class ImportContactsQualityTests(unittest.TestCase):
    def test_gmail_import_uses_per_account_people_records_from_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            base = tmp / ".powerpacks/network-import"
            discover_gmail = base / "discover/gmail"
            discover_gmail.mkdir(parents=True)
            contacts = discover_gmail / "contacts.csv"
            queue = discover_gmail / "linkedin_resolution_queue.csv"
            account_dir = discover_gmail / "operator-example-com"
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
                    "account_email": "operator@example.com",
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
                "account_email": "operator@example.com",
                "queue_csv": str(account_queue),
                "people_csv": str(account_people),
                "slug": "operator-example.com",
            }])
            self.assertEqual(artifacts["gmail_people_records"], [{
                "account_email": "operator@example.com",
                "people_csv": str(account_people),
                "slug": "operator-example.com",
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
                    "account_email": "operator@example.com",
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

    def test_gmail_account_people_merge_unions_resolved_emails(self) -> None:
        # Multiple work emails that resolve to the SAME LinkedIn person must
        # union into one row carrying every address, not drop all but the first.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            one = tmp / "one.csv"
            two = tmp / "two.csv"
            three = tmp / "three.csv"
            out = tmp / "people.gmail.csv"
            base = {column: "" for column in PEOPLE_SCHEMA_COLUMNS}
            for path, email in [
                (one, "jordan@acme.com"),
                (two, "jordan@acme.vc"),
                (three, "jordan@acme.ai"),
            ]:
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=PEOPLE_SCHEMA_COLUMNS)
                    writer.writeheader()
                    writer.writerow({
                        **base,
                        "id": f"gmail:{path.stem}:{email}",
                        "linkedin_url": "https://www.linkedin.com/in/jordan-acme",
                        "public_identifier": "jordan-acme",
                        "full_name": "Jordan Reyes",
                        "primary_email": email,
                        "all_emails": json.dumps([email]),
                        "source_channels": "gmail_msgvault",
                    })

            legacy = gmail_import.load_legacy_discover_module()
            result = legacy.materialize_gmail_merged_people_csv([str(one), str(two), str(three)], out)

            self.assertEqual(result["status"], "completed")
            with out.open(newline="", encoding="utf-8") as handle:
                rows = list(CsvIO.dict_reader(handle))
            self.assertEqual(len(rows), 1)
            all_emails = json.loads(rows[0]["all_emails"])
            self.assertEqual(
                sorted(all_emails),
                ["jordan@acme.ai", "jordan@acme.com", "jordan@acme.vc"],
            )
            # primary_email stays one of the resolved addresses (first-seen).
            self.assertEqual(rows[0]["primary_email"], "jordan@acme.com")

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
