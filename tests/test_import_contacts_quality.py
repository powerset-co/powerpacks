import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS
from packs.ingestion.primitives.imports.gmail import importer as gmail_import
from packs.ingestion.primitives.discover.gmail.extract_gmail import write_msgvault_artifacts
from packs.ingestion.primitives.imports.common import write_manifest
from packs.shared.csv_io import CsvIO


class GmailSourceImportTests(unittest.TestCase):
    def test_refresh_replaces_enriched_account_rows_before_source_import(self) -> None:
        for limit, expected_emails in ((None, ["jordan@example.com", "taylor@example.com"]),
                                      (1, ["jordan@example.com"]), (0, [])):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                account_dir = root / "discover" / "owner-example.com"
                account_dir.mkdir(parents=True)
                account_people = account_dir / "people.csv"
                old_rows = [{
                    "id": "candidate:email:jordan@example.com", "primary_email": "jordan@example.com",
                    "full_name": "Avery Profile", "first_name": "Avery", "last_name": "Profile",
                    "public_identifier": "avery-profile", "linkedin_url": "https://www.linkedin.com/in/avery-profile",
                    "headline": "Old profile", "rapidapi_response": '{"name":"Avery Profile"}',
                    "all_emails": '["jordan@example.com","avery@example.com"]',
                    "primary_phone": "+15550100", "all_phones": '["+15550100"]',
                    "superseded_person_ids": '["old-profile"]', "enrichment_provider": "cached",
                    "source_channels": "gmail_msgvault,linkedin", "interaction_counts": '{"gmail":99}',
                }, {"primary_email": "removed@example.com", "full_name": "Removed Contact"}]
                CsvIO.write_dict_rows(account_people, PEOPLE_SCHEMA_COLUMNS, old_rows)
                archive = root / "original-enriched.csv"
                archive.write_bytes(account_people.read_bytes())
                original_bytes = archive.read_bytes()
                current = [
                    {"email": "jordan@example.com", "display_name": "", "total_sent": 1,
                     "total_received": 2, "total_messages": 3},
                    {"email": "taylor@example.com", "display_name": "Taylor Delta", "total_sent": 1,
                     "total_received": 1, "total_messages": 2},
                    {"email": "removed@example.com", "display_name": "Removed Contact", "total_sent": 0,
                     "total_received": 3, "total_messages": 3},
                ]
                refreshed = write_msgvault_artifacts(current, account_dir, "owner@example.com", limit=limit)
                source_rows = CsvIO.read_dict_rows(account_people)
                if limit != 0:
                    self.assertEqual(source_rows[0]["full_name"], "")
                    self.assertEqual(source_rows[0]["public_identifier"], "")
                self.assertEqual([row["primary_email"] for row in source_rows], expected_emails)
                self.assertEqual(refreshed["counts"]["contacts_final"], len(expected_emails))
                self.assertEqual(refreshed["counts"]["contacts_preserved_existing"], 0)
                for key, email_column in (("targeted_emails_csv", "primary_email"),
                                          ("gmail_contacts_aggregated_csv", "email"),
                                          ("gmail_threads_csv", "email"),
                                          ("linkedin_resolution_queue_csv", "handle")):
                    self.assertEqual([row[email_column] for row in CsvIO.read_dict_rows(Path(refreshed["artifacts"][key]))], expected_emails)
                manifest = root / "discover" / "manifest.json"
                manifest.write_text(json.dumps({"children": [{
                    "account_email": "owner@example.com", "people_csv": str(account_people),
                }]}))
                node = gmail_import.GmailImport(manifest_json=manifest, import_dir=root / "import")
                node.run()
                imported = CsvIO.read_dict_rows(node.people_csv)
                self.assertEqual([row["primary_email"] for row in imported], expected_emails)
                if imported:
                    person = imported[0]
                    self.assertEqual(person["id"], "candidate:email:jordan@example.com")
                    self.assertEqual(person["full_name"], "")
                    self.assertEqual(json.loads(person["all_emails"]), ["jordan@example.com"])
                    self.assertEqual(json.loads(person["interaction_counts"]), {"gmail": 3})
                    for field in ("public_identifier", "linkedin_url", "headline", "rapidapi_response",
                                  "primary_phone", "all_phones", "enrichment_provider"):
                        self.assertEqual(person[field], "", field)
                self.assertEqual(archive.read_bytes(), original_bytes)

    def test_import_projects_source_fields_even_when_account_id_is_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            account_people = root / "account.csv"
            CsvIO.write_dict_rows(account_people, PEOPLE_SCHEMA_COLUMNS, [{
                "id": "candidate:email:jordan@example.com", "primary_email": "JORDAN@example.com",
                "full_name": "Jordan Bravo", "first_name": "Jordan", "last_name": "Bravo",
                "all_emails": '["avery@example.com"]', "primary_phone": "+15550100",
                "all_phones": '["+15550100"]', "public_identifier": "avery-profile",
                "linkedin_url": "https://www.linkedin.com/in/avery-profile", "headline": "Old profile",
                "rapidapi_response": '{"name":"Avery Profile"}', "superseded_person_ids": '["old-profile"]',
                "enrichment_provider": "cached", "enrichment_status": "enriched",
                "interaction_counts": '{"gmail":4}', "source_channels": "gmail_msgvault,linkedin",
            }])
            original_bytes = account_people.read_bytes()
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"children": [{
                "account_email": "owner@example.com", "people_csv": str(account_people),
            }]}))
            node = gmail_import.GmailImport(manifest_json=manifest, import_dir=root / "import")
            node.run()
            person = CsvIO.read_dict_rows(node.people_csv)[0]
            self.assertEqual(person["id"], "candidate:email:jordan@example.com")
            self.assertEqual(person["full_name"], "Jordan Bravo")
            self.assertEqual(person["source_channels"], "gmail_msgvault")
            self.assertEqual(json.loads(person["all_emails"]), ["jordan@example.com"])
            for field in ("primary_phone", "all_phones", "public_identifier", "linkedin_url", "headline",
                          "rapidapi_response", "superseded_person_ids", "enrichment_provider", "enrichment_status"):
                self.assertEqual(person[field], "", field)
            self.assertEqual(account_people.read_bytes(), original_bytes)

    def test_import_rebuilds_previous_source_contract_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            account_people = root / "account.csv"
            CsvIO.write_dict_rows(account_people, PEOPLE_SCHEMA_COLUMNS, [{
                "primary_email": "jordan@example.com", "full_name": "Jordan Bravo",
                "interaction_counts": '{"gmail":4}',
            }])
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"children": [{
                "account_email": "owner@example.com", "people_csv": str(account_people),
            }]}))
            import_root = root / "import"
            output = import_root / "gmail" / "people.csv"
            output.parent.mkdir(parents=True)
            CsvIO.write_dict_rows(output, PEOPLE_SCHEMA_COLUMNS, [{
                "id": "candidate:email:jordan@example.com", "primary_email": "jordan@example.com",
                "public_identifier": "avery-profile",
            }])
            write_manifest("gmail", {
                "status": "completed", "input": {"pipeline_contract": "gmail-source-only-v1",
                    "discovery_manifest": str(manifest),
                    "accounts": [{"account_email": "owner@example.com", "people_csv": str(account_people)}]},
                "outputs": {"people_csv": str(output)}, "stats": {"people": 1, "candidates": 1},
            }, import_dir=import_root)
            node = gmail_import.GmailImport(manifest_json=manifest, import_dir=import_root)
            node.run()
            self.assertFalse(node.written.get("noop", False))
            self.assertEqual(CsvIO.read_dict_rows(output)[0]["public_identifier"], "")

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
            CsvIO.write_dict_rows(directory, ["source", "source_key", "status", "email", "linkedin_url", "confidence"], [{
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
