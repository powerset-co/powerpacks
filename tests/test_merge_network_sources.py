import csv
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/merge_network_sources/merge_network_sources.py"
spec = importlib.util.spec_from_file_location("merge_network_sources", MODULE_PATH)
merge_network_sources = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = merge_network_sources
spec.loader.exec_module(merge_network_sources)


class MergeNetworkSourcesTests(unittest.TestCase):
    def invoke(self, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = merge_network_sources.main(argv)
        payload = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        return code, payload

    def write_people(self, path: Path, name: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = merge_network_sources.PEOPLE_SCHEMA_COLUMNS
        row = {col: "" for col in fields}
        row.update({
            "id": f"id-{name}",
            "public_identifier": "jane-example",
            "linkedin_url": "https://www.linkedin.com/in/jane-example",
            "full_name": name,
            "current_company": "Acme AI",
            "source_channels": path.parent.parent.name,
        })
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(row)

    def test_discovery_prefers_people_csv_and_writes_canonical_merge_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                run_dir = Path(".powerpacks/network-import/linkedin/run-1")
                self.write_people(run_dir / "people.csv", "Jane Canonical")
                self.write_people(run_dir / "people_harmonic_all.csv", "Jane Legacy")
                out_dir = Path(tmp) / "merged"
                code, payload = self.invoke(["run", "--base-dir", ".powerpacks", "--output-dir", str(out_dir)])
                self.assertEqual(code, 0)
                self.assertEqual(Path(payload["people_csv"]).name, "people.csv")
                self.assertTrue(Path(payload["people_csv"]).exists())
                self.assertTrue(Path(payload["legacy_output"]).exists())
                self.assertTrue(Path(payload["network_contacts_csv"]).exists())
                self.assertTrue(Path(payload["network_contact_sources_csv"]).exists())
                self.assertTrue(Path(payload["network_companies_csv"]).exists())
                with Path(payload["people_csv"]).open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["full_name"], "Jane Canonical")
                with Path(payload["network_contacts_csv"]).open(newline="", encoding="utf-8") as handle:
                    contacts = list(csv.DictReader(handle))
                self.assertEqual(contacts[0]["source_channels"], "linkedin")
                with Path(payload["network_contact_sources_csv"]).open(newline="", encoding="utf-8") as handle:
                    sources = list(csv.DictReader(handle))
                self.assertEqual(sources[0]["source_channel"], "linkedin")
                self.assertEqual(sources[0]["source_identifier"], "https://www.linkedin.com/in/jane-example")
                with Path(payload["network_companies_csv"]).open(newline="", encoding="utf-8") as handle:
                    companies = list(csv.DictReader(handle))
                self.assertEqual(companies[0]["company_name"], "Acme AI")
                self.assertEqual(companies[0]["contact_count"], "1")
            finally:
                os.chdir(old_cwd)

    def test_no_discover_ignores_filesystem_candidates_without_explicit_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                self.write_people(Path(".powerpacks/network-import/linkedin/old-run/people.csv"), "Jane Old")
                out_dir = Path(tmp) / "merged"
                code, payload = self.invoke(["run", "--no-discover", "--base-dir", ".powerpacks", "--output-dir", str(out_dir)])
                self.assertEqual(code, 0)
                self.assertEqual(payload["input_rows"], 0)
                self.assertEqual(payload["merged_rows"], 0)
                with Path(payload["people_csv"]).open(newline="", encoding="utf-8") as handle:
                    self.assertEqual(list(csv.DictReader(handle)), [])
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
