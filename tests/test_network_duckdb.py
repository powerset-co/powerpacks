import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packs/ingestion/primitives/build_network_duckdb/build_network_duckdb.py"


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class NetworkDuckDBTests(unittest.TestCase):
    def test_loads_network_contact_csvs_into_duckdb(self) -> None:
        try:
            import duckdb  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("duckdb is not installed")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            network = tmp / "network"
            write_csv(network / "people.csv", ["id", "full_name", "source_channels"], [
                {"id": "person-1", "full_name": "Jane Example", "source_channels": "linkedin,gmail_msgvault"},
            ])
            write_csv(network / "network_contacts.csv", ["contact_id", "merge_key", "display_name", "linkedin_url", "public_identifier", "primary_email", "primary_phone", "source_channels", "source_count", "needs_review"], [
                {"contact_id": "person-1", "merge_key": "linkedin:jane-example", "display_name": "Jane Example", "linkedin_url": "https://www.linkedin.com/in/jane-example", "public_identifier": "jane-example", "primary_email": "jane@example.com", "primary_phone": "", "source_channels": "linkedin,gmail_msgvault", "source_count": "2", "needs_review": "false"},
            ])
            write_csv(network / "network_contact_sources.csv", ["contact_id", "merge_key", "source_channel", "source_identifier", "source_artifact", "display_name", "linkedin_url", "public_identifier", "primary_email", "primary_phone"], [
                {"contact_id": "person-1", "merge_key": "linkedin:jane-example", "source_channel": "linkedin", "source_identifier": "https://www.linkedin.com/in/jane-example", "source_artifact": "linkedin/people.csv", "display_name": "Jane Example", "linkedin_url": "https://www.linkedin.com/in/jane-example", "public_identifier": "jane-example", "primary_email": "", "primary_phone": ""},
                {"contact_id": "person-1", "merge_key": "linkedin:jane-example", "source_channel": "gmail_msgvault", "source_identifier": "jane@example.com", "source_artifact": "gmail/people.csv", "display_name": "Jane Example", "linkedin_url": "", "public_identifier": "", "primary_email": "jane@example.com", "primary_phone": ""},
            ])
            write_csv(network / "network_companies.csv", ["company_id", "company_key", "company_name", "company_urn", "source_channels", "contact_count", "contact_ids", "contact_names"], [
                {"company_id": "company-1", "company_key": "name:acme-ai", "company_name": "Acme AI", "company_urn": "", "source_channels": "linkedin", "contact_count": "1", "contact_ids": "[\"person-1\"]", "contact_names": "[\"Jane Example\"]"},
            ])

            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--network-dir", str(network), "--output-dir", str(tmp / "duckdb"), "--force"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["tables"]["local_network_contacts"], 1)
            self.assertEqual(payload["tables"]["local_network_contact_sources"], 2)
            self.assertEqual(payload["tables"]["local_network_companies"], 1)

            import duckdb
            con = duckdb.connect(payload["duckdb"], read_only=True)
            try:
                count = con.execute("SELECT COUNT(*) FROM network_contact_sources WHERE source_channel = 'gmail_msgvault'").fetchone()[0]
                self.assertEqual(count, 1)
                sources = con.execute("SELECT source_channels FROM network_contacts WHERE contact_id = 'person-1'").fetchone()[0]
                self.assertEqual(sources, "linkedin,gmail_msgvault")
                company = con.execute("SELECT company_name FROM network_companies WHERE company_key = 'name:acme-ai'").fetchone()[0]
                self.assertEqual(company, "Acme AI")
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
