import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.primitives.imports.directory import (
    DIRECTORY_COLUMNS,
)
from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.ingestion.primitives.imports.gmail import importer as gmail_import
from packs.shared.csv_io import CsvIO


def write_directory(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DIRECTORY_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in DIRECTORY_COLUMNS})


class GmailSourceImportTests(unittest.TestCase):
    def test_import_retains_all_source_contacts_without_queue_or_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            discovery = root / ".powerpacks/network-import/discover/gmail"
            account_people = discovery / "owner-example.com/people.csv"
            account_people.parent.mkdir(parents=True)
            source_rows = [{
                "id": f"gmail:{name}", "primary_email": email, "full_name": name,
                "first_name": name.split()[0] if name else "",
                "source_channels": "gmail_msgvault",
                "source_artifacts": json.dumps(["synthetic/targeted_emails.csv"]),
                "interaction_counts": json.dumps({"gmail": count}),
            } for email, name, count in (
                ("jordan@example.com", "Jordan Bravo", 4),
                ("casey@example.com", "", 1),
                ("noreply@example.com", "Service Updates", 1),
            )]
            CsvIO.write_dict_rows(account_people, PEOPLE_SCHEMA_COLUMNS, source_rows)
            source_bytes = account_people.read_bytes()
            (discovery / "manifest.json").write_text(json.dumps({"children": [{
                "account_email": "owner@example.com", "people_csv": str(account_people),
                "linkedin_resolution_queue_csv": str(account_people.parent / "absent.csv"),
                "code": 0, "contacts": 3, "sync": {"status": "skipped"},
                "artifacts": {"people_csv": str(account_people)},
            }]}))
            command = [sys.executable, str(Path(gmail_import.__file__)), "run"]
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=True)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["stats"], {"people": 3, "candidates": 3})
            output = root / ".powerpacks/network-import/import/gmail/people.csv"
            rows = CsvIO.read_dict_rows(output)
            self.assertEqual([row["id"] for row in rows], [
                "candidate:email:casey@example.com", "candidate:email:jordan@example.com",
                "candidate:email:noreply@example.com",
            ])
            self.assertEqual(rows[0]["full_name"], "")
            self.assertEqual(rows[1]["first_name"], "Jordan")
            self.assertTrue(all(row["enriched_at"] == row["enrichment_provider"] == "" for row in rows))
            self.assertTrue(all(row["public_identifier"] == row["linkedin_url"] == "" for row in rows))
            self.assertEqual(account_people.read_bytes(), source_bytes)
            self.assertFalse((root / ".powerpacks/network-import/directory.csv").exists())
            again = subprocess.run(command, cwd=root, capture_output=True, text=True, check=True)
            self.assertTrue(json.loads(again.stdout)["noop"])

    def test_import_merges_account_metadata_and_leaves_stored_decisions_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            discovery = root / "discover"
            discovery.mkdir()
            accounts = []
            for owner, count, name in (("one", 4, "Jordan Bravo"), ("two", 7, "")):
                people_csv = discovery / f"{owner}.csv"
                CsvIO.write_dict_rows(people_csv, PEOPLE_SCHEMA_COLUMNS, [{
                    "id": f"gmail:{owner}", "primary_email": "jordan@example.com",
                    "full_name": name, "first_name": "Jordan" if name else "",
                    "source_channels": "gmail_msgvault", "source_artifacts": json.dumps([f"{owner}/targeted.csv"]),
                    "interaction_counts": json.dumps({"gmail": count}),
                    "last_interaction": f"2026-01-0{count}",
                }])
                accounts.append({"account_email": f"{owner}@example.com", "people_csv": str(people_csv)})
            manifest = discovery / "manifest.json"
            manifest.write_text(json.dumps({"children": accounts}))
            directory = root / "directory.csv"
            write_directory(directory, [{
                "source": "deep_context_review", "source_key": "email:jordan@example.com",
                "email": "jordan@example.com", "status": "found", "confidence": "1",
                "linkedin_url": "https://www.linkedin.com/in/jordan-bravo",
            }])
            import_root = root / "import"
            stored = import_root / "gmail" / "gmail-combined-resolutions-one" / "linkedin_resolutions.csv"
            stored.parent.mkdir(parents=True)
            stored.write_text("saved paid resolution artifact")
            preserved = {path: path.read_bytes() for path in (directory, stored)}
            node = gmail_import.GmailImport(manifest_json=manifest, import_dir=import_root)
            node.run()
            people = CsvIO.read_dict_rows(node.people_csv)
            self.assertEqual(len(people), 1)
            person = people[0]
            self.assertEqual(person["id"], "candidate:email:jordan@example.com")
            self.assertEqual(person["linkedin_url"], "")
            self.assertEqual(person["first_name"], "Jordan")
            self.assertEqual(json.loads(person["interaction_counts"]), {"gmail": 7})
            self.assertEqual(json.loads(person["all_emails"]), ["jordan@example.com"])
            self.assertTrue({"one/targeted.csv", "two/targeted.csv"}.issubset(json.loads(person["source_artifacts"])))
            self.assertEqual({path: path.read_bytes() for path in preserved}, preserved)
            self.assertNotIn(str(directory), node.written["fingerprints"]["input_artifacts"])
            changed_rows = CsvIO.read_dict_rows(Path(accounts[0]["people_csv"]))
            changed_rows.append({"primary_email": "casey@example.com", "source_channels": "gmail_msgvault"})
            CsvIO.write_dict_rows(Path(accounts[0]["people_csv"]), PEOPLE_SCHEMA_COLUMNS, changed_rows)
            refreshed = gmail_import.GmailImport(manifest_json=manifest, import_dir=import_root)
            refreshed.run()
            self.assertFalse(refreshed.written.get("noop", False))
            self.assertEqual(refreshed.written["stats"], {"people": 2, "candidates": 2})

    def test_import_cli_has_no_matching_or_operator_options(self) -> None:
        args = gmail_import.build_parser().parse_args(["run"])
        self.assertEqual(vars(args), {"command": "run", "force": False})


if __name__ == "__main__":
    unittest.main()
